#!/usr/bin/env python3
"""Rewrite a built sdist so that two builds of one commit give the same bytes.

    export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
    python3 -m build
    python3 scripts/normalize_sdist.py dist/context_layer-*.tar.gz

setuptools writes each file's own modification time, the build time into the
gzip header, and the builder's local user and group names into every tar
header, so a locally built sdist differs from build to build and names the
account that built it. This script rewrites the archive in place:

- members in name order;
- owner and group `root` (uid and gid 0);
- every modification time set to SOURCE_DATE_EPOCH (or --epoch);
- permissions 0644, or 0755 for directories and for files with an execute bit;
- no extra PAX records (access times, float times);
- a gzip header carrying that same time and no file name.

File contents are not changed. It prints `sha256  name` for each archive.
The wheel needs none of this: with SOURCE_DATE_EPOCH set, setuptools already
writes it reproducibly.

Python 3.10+; standard library only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile


def normalize(path: Path, epoch: int) -> str:
    with tarfile.open(path, "r:gz", encoding="utf-8") as source:
        members = []
        for info in source.getmembers():
            data = source.extractfile(info).read() if info.isfile() else None
            members.append((info, data))
    members.sort(key=lambda pair: pair[0].name)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8") as target:
        for info, data in members:
            clean = tarfile.TarInfo(info.name)
            clean.type = info.type
            clean.linkname = info.linkname
            clean.size = len(data) if data is not None else 0
            executable = info.isdir() or bool(info.mode & 0o111)
            clean.mode = 0o755 if executable else 0o644
            clean.uid = clean.gid = 0
            clean.uname = clean.gname = "root"
            clean.mtime = epoch
            target.addfile(clean, io.BytesIO(data) if data is not None else None)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=epoch) as packed:
            packed.write(raw.getvalue())
    os.replace(tmp, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sdist", nargs="+", type=Path, help="Built .tar.gz files, rewritten in place.")
    parser.add_argument("--epoch", type=int, default=None,
                        help="Timestamp for every member (default: SOURCE_DATE_EPOCH).")
    args = parser.parse_args(argv)
    epoch = args.epoch
    if epoch is None:
        value = os.environ.get("SOURCE_DATE_EPOCH", "")
        if not value.isdigit():
            parser.error("set SOURCE_DATE_EPOCH (for example to `git log -1 --format=%ct`) or pass --epoch")
        epoch = int(value)
    for path in args.sdist:
        if not path.name.endswith(".tar.gz") or not path.is_file():
            print(f"normalize_sdist: not a .tar.gz file: {path}", file=sys.stderr)
            return 2
        print(f"{normalize(path, epoch)}  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
