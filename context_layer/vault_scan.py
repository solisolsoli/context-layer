#!/usr/bin/env python3
"""Scan a Markdown vault and propose a starting routing config.

What this does
--------------
Walks a vault, looks at top-level folder structure, filename tokens, Markdown
headings and file sizes, and writes a `routes.json` the router can actually
load. It also pre-fills the exclude prefixes for the obvious noise (Obsidian
app data, trash, node_modules, attachment folders, anything that looks
generated or archived).

What this is NOT
----------------
It is not a semantic understanding of your vault. It reads filenames, folder
names, headings and file sizes. It never decides what a document *means*. Every
route it proposes is a guess that you are expected to correct. The generated
file says so in its own `_generated` keys, and `print_report` says so on stdout.

Python 3.10+; standard library only; no network access.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat as stat_module

from . import __version__

# The loader that will read the generated config, and the shared text rules.
try:
    from router import source_policy as _policy, textfold as _textfold  # checkout
except ImportError:
    from .router import source_policy as _policy, textfold as _textfold  # installed package

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Mirrors build_index.py's default extension set, so a route can only ever
# point at something the indexer would have indexed.
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".jsonl", ".csv"}

# Files that are plainly not prose. Used to recognise an attachment folder.
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg", ".ico",
    ".pdf", ".mp3", ".m4a", ".wav", ".flac", ".mp4", ".mov", ".avi", ".mkv",
    ".zip", ".gz", ".tar", ".7z", ".dmg", ".exe", ".bin",
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".key", ".pages",
    ".ttf", ".otf", ".woff", ".woff2", ".canvas", ".excalidraw",
}

# Tool/app directories. Always excluded, never a route.
TOOLING_DIRS = {
    ".obsidian", ".trash", ".git", ".svn", ".hg", ".idea", ".vscode",
    "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".DS_Store", ".context", ".context-runs",
    ".smart-env", ".space", ".stfolder",
}

# Folders whose usual job is to hold binaries pasted into notes.
ATTACHMENT_DIR_NAMES = {
    "attachments", "attachment", "_attachments", "assets", "_assets",
    "media", "_media", "images", "image", "img", "files", "_files",
    "resources", "_resources", "excalidraw", "clippings-media",
}

# Words that mark a folder as generated output or a frozen copy. Matched as whole
# name tokens (plus a plural or past-tense ending), never as substrings: "dist"
# must not exclude "Distributed Systems", nor "temp" a "Temperature Logs" folder.
GENERATED_MARKERS = (
    "generated", "archive", "archiv", "snapshot", "backup", "bak",
    "build", "dist", "output", "export", "cache", "tmp", "temp",
    "trash", "deprecated", "obsolete",
)

# Filenames that usually act as the entry point of a folder.
INDEX_STEMS = {"index", "readme", "home", "moc", "map", "overview", "start",
               "_index", "00-index", "00_index", "dashboard"}

# Filename markers for a document that has probably been replaced. The scanner
# cannot know what replaced it, so it refuses to pin such a file as a CURRENT
# canonical source rather than guessing `superseded_by`.
STALE_NAME_MARKERS = ("legacy", "deprecated", "superseded", "obsolete",
                      "-old", "old-", "_old", "previous", "outdated")

# English default stopwords: a trigger term that is a stopword is worthless as
# a route trigger. A vault in another language adds its own words through the
# optional `stopwords` list in .context/routes.json; `init` reads that list from
# an existing config, applies it on top of these defaults, and writes it back
# into the regenerated config so the router uses the same words.
STOPWORDS = frozenset({
    "a", "about", "after", "all", "an", "and", "any", "are", "as", "at", "be",
    "been", "before", "but", "by", "can", "do", "does", "each", "for", "from",
    "get", "had", "has", "have", "how", "i", "if", "in", "into", "is", "it",
    "its", "just", "like", "make", "many", "may", "me", "more", "most", "my",
    "new", "no", "not", "now", "of", "on", "one", "only", "or", "other", "our",
    "out", "over", "per", "see", "should", "so", "some", "such", "than",
    "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "to", "two", "up", "use", "used", "using", "very", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your", "note", "notes", "page", "pages", "file",
    "files", "doc", "docs", "document", "documents", "untitled", "copy",
    "draft", "final", "version", "md", "txt", "part",
    # Calendar/template words. A daily-note template repeats these in every
    # file, so they look "frequent" while carrying no topic at all.
    "today", "tomorrow", "yesterday", "week", "weekly", "monthly", "morning",
    "evening", "night", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "january", "february", "march", "april",
    "june", "july", "august", "september", "october", "november", "december",
})

# The set tokens_of() consults; scan_vault() sets it for the vault it scans.
_active_stopwords = set(STOPWORDS)

# Continuation terms are generic (not vault-specific), so a generated config
# can ship them safely. Copied from routes.example.json.
CONTINUATION_TERMS = [
    "continue", "go on", "ok", "okay", "yes", "no", "fine", "good", "sure",
    "again", "shorter", "longer", "same", "thanks", "this", "that",
    "keep going", "carry on", "approved", "agreed", "nope", "make it shorter",
    "make it longer", "shorten", "expand", "once more", "redo", "nice", "i like it",
    "not quite", "maybe", "i think", "please", "totally", "a bit", "understood",
    "got it", "did you get it",
]

DATE_STEM = re.compile(r"^\d{4}[-_.]?\d{2}[-_.]?\d{2}")
WEEK_STEM = re.compile(r"^\d{4}[-_.]?[Ww]\d{1,2}$")
MONTH_STEM = re.compile(r"^\d{4}[-_.]\d{2}$")
TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)

MAX_CANONICAL_BYTES = 200_000      # a canonical source is read whole by the router
HEADING_READ_BYTES = 8_000         # only the head of a file is read for headings


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@dataclass
class ProposedRoute:
    name: str
    triggers: list[str]
    canonical_sources: list[str]
    path_hints: list[str]
    file_count: int
    why: list[str] = field(default_factory=list)
    canonical_why: dict[str, str] = field(default_factory=dict)
    canonical_note: str | None = None
    low_confidence: str | None = None


@dataclass
class ScanResult:
    vault: Path
    text_files: list[Path]
    excluded: list[tuple[str, str]]          # (prefix, reason)
    mirror_prefixes: list[str]
    routes: list[ProposedRoute]
    skipped_groups: list[tuple[str, str]]    # (group, reason)
    shape: str                               # "foldered" | "flat" | "mixed"
    notes: list[str] = field(default_factory=list)
    stopwords: list[str] = field(default_factory=list)   # extra, beyond the defaults
    # Detected noise folders whose name routes.json would refuse: (prefix, why).
    omitted_exclusions: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def normalize(text: str) -> str:
    """The shared comparison form (router/textfold.py fold): the router folds prompts
    and triggers the same way."""
    return _textfold.fold(text)


def tokens_of(text: str) -> list[str]:
    return [t for t in TOKEN.findall(normalize(text))
            if len(t) >= 4 and t not in _active_stopwords]


def configured_stopwords(vault: Path) -> list[str]:
    """The optional `stopwords` list of an existing .context/routes.json.

    Missing or unreadable config means none: `init` must still work on a fresh
    vault. A present list that is not a list of strings is ignored as well; the
    router, which reads the same key at query time, reports it as an error.
    """
    config_path = vault / ".context" / "routes.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    extra = config.get("stopwords") if isinstance(config, dict) else None
    if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
        return []
    return sorted({normalize(item).strip() for item in extra if item.strip()})


def ranked(counter: Counter) -> "list[tuple[str, int]]":
    """Counter.most_common is tie-unstable across runs; a config generator must
    not produce a different file from the same vault. Break ties on the token."""
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def is_date_like(stem: str) -> bool:
    return bool(DATE_STEM.match(stem) or WEEK_STEM.match(stem) or MONTH_STEM.match(stem))


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize(text).encode("ascii", "ignore").decode()).strip("-")
    return slug or "route"


def human_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


GENERATED_ENDINGS = ("", "s", "es", "d", "ed")


def name_tokens(name: str) -> list[str]:
    """Split a folder name into lower-case word tokens: on anything that is not a
    letter or digit, at lower-to-upper case changes and at letter/digit edges."""
    spaced = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", name)
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", spaced)
    return [token for token in re.split(r"[\W_]+", normalize(spaced)) if token]


def looks_generated(name: str) -> str | None:
    tokens = set(name_tokens(name))
    for marker in GENERATED_MARKERS:
        if any(marker + ending in tokens for ending in GENERATED_ENDINGS):
            return marker
    return None


# --------------------------------------------------------------------------
# Walk + exclusion
# --------------------------------------------------------------------------

def _classify_top_level(vault: Path, directory: Path) -> tuple[str, str] | None:
    """Return (kind, reason) if this top-level directory should be excluded."""
    name = directory.name
    if directory.is_symlink():
        return ("tooling", "symlink directory is not followed")
    if name in TOOLING_DIRS or name.startswith("."):
        return ("tooling", f"tool/app directory ({name})")
    text = binary = other = 0
    for path in directory.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in TEXT_EXTENSIONS:
            text += 1
        elif suffix in BINARY_EXTENSIONS:
            binary += 1
        else:
            other += 1
    # A conventional attachment name ("Resources", "files") excludes the folder
    # only when it does not mostly hold notes: a notes-only "Resources/" stays in.
    if normalize(name) in ATTACHMENT_DIR_NAMES and binary + other >= text:
        return ("attachments", "name is a conventional attachment folder and most of "
                               "its files are not notes")
    total = text + binary
    if total >= 3 and binary / total >= 0.8:
        return ("attachments", f"{binary}/{total} files are binary attachments")

    marker = looks_generated(name)
    if marker:
        return ("generated", f"folder name has the word '{marker}'")
    return None


def scan_exclusions(vault: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (exclude_prefixes with reasons, mirror_prefixes), unvalidated; see
    writable_exclusions()."""
    excluded: list[tuple[str, str]] = []
    mirrors: list[str] = []
    for entry in sorted(vault.iterdir()):
        if not entry.is_dir():
            continue
        verdict = _classify_top_level(vault, entry)
        if verdict:
            kind, reason = verdict
            excluded.append((entry.name + "/", reason))
            if kind == "generated" and looks_generated(entry.name) in (
                "snapshot", "archive", "archiv"):
                mirrors.append(entry.name + "/")
        else:
            # One level down: a live folder can still hold an attachment or
            # generated subfolder.
            for child in sorted(p for p in entry.iterdir() if p.is_dir()):
                sub = _classify_top_level(vault, child)
                if sub:
                    prefix = f"{entry.name}/{child.name}/"
                    excluded.append((prefix, sub[1]))
    # Always-on floor, whether or not these directories exist today.
    for name, reason in ((".obsidian/", "Obsidian app data"),
                         (".trash/", "Obsidian trash"),
                         ("node_modules/", "dependency directory"),
                         (".context-runs/", "this tool's own run artifacts")):
        if not any(prefix == name for prefix, _ in excluded):
            excluded.append((name, reason + " (pre-filled by default)"))
    excluded.sort()
    return excluded, mirrors


def writable_exclusions(excluded: list[tuple[str, str]]) -> tuple[
        list[tuple[str, str]], list[tuple[str, str]]]:
    """Split detected exclusions into (written, omitted). An entry the strict loader
    would refuse is never written, because the whole config would then be refused;
    each omitted one carries its reason and what happens to its files instead."""
    written: list[tuple[str, str]] = []
    omitted: list[tuple[str, str]] = []
    for prefix, reason in excluded:
        problem = _policy.exclusion_entry_problem(prefix, fix=False)
        if problem is None:
            written.append((prefix, reason))
            continue
        try:
            _policy.relative_name(prefix)
            outcome = "its notes are indexed unless you rename the folder"
        except ValueError:
            outcome = "`index` skips its files as unsupported names"
        omitted.append((prefix, f"{reason}; not written, because routes.json refuses the "
                                f"entry (it {problem}); {outcome}"))
    return written, omitted


def collect_text_files(vault: Path, exclude_prefixes: list[str]) -> list[Path]:
    """Non-empty text files outside tooling, dot and excluded folders, sorted, relative
    to the vault. Symlinks are never followed or listed. A folder that no file below
    could survive (tooling, dot, or covered by an exclusion prefix) is not walked."""
    prefixes = tuple(exclude_prefixes)
    found = []
    for current, directories, names in os.walk(vault, followlinks=False):
        here = os.path.relpath(current, vault)
        prefix = "" if here == "." else here.replace(os.sep, "/") + "/"
        directories[:] = [d for d in directories
                          if d not in TOOLING_DIRS and not d.startswith(".")
                          and not (prefix + d + "/").startswith(prefixes)]
        for name in names:
            if os.path.splitext(name)[1].lower() not in TEXT_EXTENSIONS or name in TOOLING_DIRS:
                continue
            rel_str = prefix + name
            if rel_str.startswith(prefixes):
                continue
            try:
                info = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if not stat_module.S_ISREG(info.st_mode) or info.st_size == 0:
                continue          # a symlink, a pipe or an empty file
            found.append(Path(rel_str))
    return sorted(found)


# --------------------------------------------------------------------------
# Evidence per group
# --------------------------------------------------------------------------

def heading_tokens(vault: Path, relatives: list[Path], limit: int = 60) -> Counter:
    counter: Counter = Counter()
    for rel in relatives[:limit]:
        if rel.suffix.lower() != ".md":
            continue
        try:
            with (vault / rel).open("r", encoding="utf-8", errors="replace") as handle:
                head = handle.read(HEADING_READ_BYTES)
        except OSError:
            continue
        for _offset, level, title in _textfold.headings(head):
            if level <= 3:
                counter.update(set(tokens_of(title)))
    return counter


def filename_tokens(relatives: list[Path]) -> Counter:
    counter: Counter = Counter()
    for rel in relatives:
        stem = rel.stem
        if is_date_like(stem):
            continue
        counter.update(set(tokens_of(stem.replace("-", " ").replace("_", " "))))
    return counter


def pick_canonical(vault: Path, relatives: list[Path], group_name: str,
                   want: int) -> tuple[list[str], dict[str, str], list[str]]:
    """Choose the files a route should always open, with the reason for each."""
    scored: list[tuple[float, str, Path]] = []
    stale: list[str] = []
    group_tokens = set(tokens_of(group_name.replace("-", " ").replace("_", " ")))
    for rel in relatives:
        try:
            size = (vault / rel).stat().st_size
        except OSError:
            continue
        if size > MAX_CANONICAL_BYTES:
            continue
        stem = normalize(rel.stem)
        if any(marker in stem for marker in STALE_NAME_MARKERS):
            stale.append(str(rel).replace("\\", "/"))
            continue
        score = 0.0
        reason = f"largest remaining file in the group ({human_bytes(size)})"
        if stem in INDEX_STEMS or stem.lstrip("0_- ") in INDEX_STEMS:
            score += 100
            reason = "index-style filename"
        elif group_tokens and group_tokens & set(tokens_of(stem)):
            score += 50
            reason = f"filename repeats the group name ({human_bytes(size)})"
        if is_date_like(rel.stem):
            score -= 40
            reason = "date-named file (weak canonical candidate)"
        score += min(size / 1000.0, 40.0)
        scored.append((score, reason, rel))
    scored.sort(key=lambda item: (-item[0], str(item[2])))
    paths: list[str] = []
    why: dict[str, str] = {}
    for _score, reason, rel in scored[:want]:
        key = str(rel).replace("\\", "/")
        paths.append(key)
        why[key] = reason
    return paths, why, stale


# --------------------------------------------------------------------------
# Route proposal
# --------------------------------------------------------------------------

def _build_route(vault: Path, name: str, relatives: list[Path], path_hint: str | None,
                 why: list[str], name_is_synthetic: bool = False) -> ProposedRoute:
    single = len(relatives) == 1
    fname = filename_tokens(relatives)
    heads = heading_tokens(vault, relatives)

    triggers: list[str] = []
    trigger_why: list[str] = []
    # A synthetic group name ("vault-root") describes the scan, not the content,
    # so it must never become a trigger term.
    if not name_is_synthetic:
        phrase = normalize(name.replace("-", " ").strip())
        if len(phrase) >= 4 and " " in phrase:
            triggers.append(phrase)
        for token in tokens_of(phrase):
            if token not in triggers:
                triggers.append(token)
        if triggers:
            trigger_why.append(f"group name ({', '.join(triggers)})")

    min_name_count = 1 if single else 2
    repeated = [t for t, n in ranked(fname)[:12]
                if n >= min_name_count and t not in triggers][:5]
    if repeated:
        triggers.extend(repeated)
        trigger_why.append(
            f"filename tokens in >={min_name_count} file(s) (" + ", ".join(repeated) + ")")

    min_head_count = 1 if single else max(2, len(relatives) // 8)
    head_terms = [t for t, n in ranked(heads)[:14]
                  if n >= min_head_count and t not in triggers][:4]
    if head_terms:
        triggers.extend(head_terms)
        trigger_why.append(
            ("headings" if single else "repeated headings") + " (" + ", ".join(head_terms) + ")")

    want = 3 if len(relatives) >= 8 else 2
    canonical, canonical_why, stale = pick_canonical(vault, relatives, name, want)
    canonical_note = None
    if stale:
        trigger_why.append(
            "held back from canonical_sources because the filename looks superseded: "
            + ", ".join(stale))
    # A group of date-named files (a journal, a meeting log) has no canonical
    # document. Pinning arbitrary days into every packet would be worse than
    # pinning nothing, so such a route is left lexical-only on purpose.
    if canonical and all(is_date_like(Path(c).stem) for c in canonical):
        canonical, canonical_why = [], {}
        canonical_note = (
            "left empty on purpose: every candidate in this group is date-named, so no "
            "one entry is the authority. If you keep a standing note for this area "
            "(a rules page, a project index), name it here."
        )

    route = ProposedRoute(
        name=slugify(name),
        triggers=triggers[:10],
        canonical_sources=canonical,
        path_hints=[path_hint] if path_hint else [],
        file_count=len(relatives),
        why=why + trigger_why,
        canonical_why=canonical_why,
        canonical_note=canonical_note,
    )
    if len(route.triggers) < 2:
        route.low_confidence = "fewer than 2 trigger terms could be inferred"
    elif not canonical and not canonical_note:
        route.low_confidence = "no canonical source small enough to open whole"
    return route


def propose_routes(vault: Path, relatives: list[Path], max_routes: int) -> tuple[
        list[ProposedRoute], list[tuple[str, str]], str]:
    by_top: dict[str, list[Path]] = defaultdict(list)
    for rel in relatives:
        by_top["" if len(rel.parts) == 1 else rel.parts[0]].append(rel)

    root_files = by_top.get("", [])
    folders = {k: v for k, v in by_top.items() if k}
    if not folders:
        shape = "flat"
    elif len(root_files) > sum(len(v) for v in folders.values()):
        shape = "mixed"
    else:
        shape = "foldered"

    routes: list[ProposedRoute] = []
    skipped: list[tuple[str, str]] = []

    for folder, files in sorted(folders.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(files) < 2:
            skipped.append((folder + "/", f"only {len(files)} indexable file(s)"))
            continue
        routes.append(_build_route(
            vault, folder, files, folder + "/",
            [f"top-level folder {folder}/ with {len(files)} indexable files"],
        ))

    # Root-level files: cluster by a shared filename token rather than dumping
    # every loose file into one route. A date-named pile becomes one route,
    # because a date is useless as a trigger term.
    if root_files:
        dated = [r for r in root_files if is_date_like(r.stem)]
        undated = [r for r in root_files if not is_date_like(r.stem)]
        if len(dated) >= 3 and len(dated) >= 0.3 * len(root_files):
            route = _build_route(
                vault, "daily-notes", dated, None,
                [f"{len(dated)} of {len(root_files)} root files have date-shaped names"],
            )
            route.path_hints = []
            if len(route.triggers) < 2:
                route.low_confidence = (
                    "date filenames carry no vocabulary; triggers came from headings only"
                )
            routes.append(route)
        if undated:
            counter = filename_tokens(undated)
            used: set[Path] = set()
            for token, count in ranked(counter)[:6]:
                if count < 2:
                    break
                members = [r for r in undated
                           if token in tokens_of(r.stem.replace("-", " ").replace("_", " "))]
                members = [m for m in members if m not in used]
                if len(members) < 2:
                    continue
                used.update(members)
                routes.append(_build_route(
                    vault, token, members, None,
                    [f"{len(members)} root files share the filename token '{token}'"],
                ))
            leftover = [r for r in undated if r not in used]
            # A flat vault of topically distinct files has no grouping to find:
            # each file IS the topic. One route per file, named after the file,
            # is a far more useful guess than one "everything" route.
            budget = max_routes - len(routes)
            if leftover and budget > 0:
                leftover.sort(key=lambda r: -(vault / r).stat().st_size)
                for rel in leftover[:budget]:
                    routes.append(_build_route(
                        vault, rel.stem, [rel], None,
                        ["single loose root file, no folder and no shared filename "
                         "token to group it with"],
                    ))
                for rel in leftover[budget:]:
                    skipped.append((str(rel), "no group found and the route budget was full"))
            elif leftover:
                skipped.append(("<vault root>",
                                f"{len(leftover)} loose file(s) share no filename token "
                                "and the route budget was full"))

    routes.sort(key=lambda r: (r.low_confidence is not None, -r.file_count, r.name))
    for extra in routes[max_routes:]:
        skipped.append((extra.name, f"over the --max-routes limit of {max_routes}"))
    routes = routes[:max_routes]

    # A trigger that fires two routes is worse than no trigger: keep each term
    # on the route where its group is largest, drop it elsewhere.
    seen: dict[str, ProposedRoute] = {}
    for route in sorted(routes, key=lambda r: -r.file_count):
        kept = []
        for trigger in route.triggers:
            owner = seen.get(trigger)
            if owner is None:
                seen[trigger] = route
                kept.append(trigger)
        route.triggers = kept
        if len(route.triggers) < 2 and not route.low_confidence:
            route.low_confidence = "trigger terms collided with a larger route"
    return routes, skipped, shape


def scan_vault(vault: Path, max_routes: int = 8,
               stopwords: "list[str] | None" = None) -> ScanResult:
    """Scan a vault. `stopwords` extends the English defaults; None reads the
    optional `stopwords` list from the vault's existing .context/routes.json."""
    global _active_stopwords
    vault = vault.resolve()
    extra = configured_stopwords(vault) if stopwords is None else \
        sorted({normalize(item).strip() for item in stopwords if item.strip()})
    _active_stopwords = set(STOPWORDS) | set(extra)
    detected, mirrors = scan_exclusions(vault)
    excluded, omitted = writable_exclusions(detected)
    written = {prefix for prefix, _ in excluded}
    mirrors = [prefix for prefix in mirrors if prefix in written]
    # Routes are proposed from the files outside every detected noise folder,
    # written or not.
    relatives = collect_text_files(vault, [p for p, _ in detected])
    routes, skipped, shape = propose_routes(vault, relatives, max_routes)
    notes = []
    if not relatives:
        notes.append("No indexable files found. Check the path, and check whether "
                     "everything was excluded as noise.")
    if not routes:
        notes.append("No route could be inferred. Write routes by hand; the shape is "
                     "documented in router/README.md.")
    return ScanResult(vault=vault, text_files=relatives, excluded=excluded,
                      mirror_prefixes=mirrors, routes=routes, skipped_groups=skipped,
                      shape=shape, notes=notes, stopwords=extra, omitted_exclusions=omitted)


# --------------------------------------------------------------------------
# Config rendering
# --------------------------------------------------------------------------

def render_config(result: ScanResult) -> dict:
    routes: dict[str, dict] = {}
    for position, route in enumerate(result.routes):
        entry: dict = {
            "_inferred_from": "; ".join(route.why),
            "priority": 2 if position == 0 else 1,
            "triggers": route.triggers,
            "canonical_sources": [
                {"path": path, "rule_state": "current"} for path in route.canonical_sources
            ],
        }
        if route.path_hints:
            entry["path_hints"] = route.path_hints
        if route.canonical_note:
            entry["_canonical_sources_empty"] = route.canonical_note
        if route.low_confidence:
            entry["_low_confidence"] = route.low_confidence
        routes[route.name] = entry

    exclude = [prefix for prefix, _ in result.excluded]
    config = {
        "schema_version": 1,
        "engine_version": f"{__version__}-generated",
        "_generated": {
            "by": "context-layer init",
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "vault_shape": result.shape,
            "indexable_files_seen": len(result.text_files),
            "warning": (
                "EVERY ROUTE BELOW IS A GUESS. It was inferred from folder names, "
                "filename tokens, Markdown headings and file sizes only. Nothing in "
                "this vault was read for meaning. Triggers that do not match how you "
                "actually phrase prompts will simply never fire, and a canonical "
                "source chosen by size may be the wrong document. Correct this file "
                "before you trust a packet built from it."
            ),
            "how_to_correct": [
                "1. Fix the trigger terms: write the phrases you really type, "
                "including multi-word ones.",
                "2. Fix canonical_sources: these must be the documents that MUST be "
                "opened whenever the route fires, not merely the biggest files.",
                "3. Label anything superseded with rule_state/superseded_by so an old "
                "rule cannot outrank the one in force.",
            ],
            "reference": "router/README.md documents every key this file may carry.",
        },
        "max_routes": 4,
        "lexical_reserve": 6,
        "canonical_source_floor": 3,
        "routes": routes,
        "_exclusions": (
            "Pre-filled from the scan. Prefixes here are skipped at index time AND "
            "dropped after retrieval, so generated or archived text can never be "
            "quoted back as vault evidence."
        ),
        "exclude_prefixes": exclude,
        "retrieval_exclude_prefixes": sorted(set(exclude + [".context"])),
        **({"_exclusions_not_written": [f"{prefix} -- {why}"
                                        for prefix, why in result.omitted_exclusions]}
           if result.omitted_exclusions else {}),
        "continuation_terms": CONTINUATION_TERMS,
        "_stopwords": (
            "Extra stopwords for this vault's language, on top of the built-in "
            "English list. Used by `init` when it picks trigger terms and by the "
            "router when it tokenizes a prompt."
        ),
        "stopwords": list(result.stopwords),
        "record_type_allowlist": ["verbatim_text_file"],
    }
    if result.mirror_prefixes:
        config["_mirrors"] = (
            "A snapshot/archive folder was detected. mirror_prefixes collapses an "
            "archived revision onto the live file instead of spending a second "
            "source slot. Remove this if those copies are not mirrors."
        )
        config["mirror_prefixes"] = result.mirror_prefixes
    return config


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def shown_path(path: Path, vault: Path) -> str:
    """Vault-relative POSIX name inside the vault, else the bare file name: the report
    never carries an absolute path."""
    try:
        return Path(path).resolve().relative_to(Path(vault).resolve()).as_posix() or "."
    except ValueError:
        return Path(path).name


def print_report(result: ScanResult, out_path: Path, written: bool,
                 emit=print) -> None:
    rel_out = shown_path(out_path, result.vault)
    emit("Scanned .")
    emit(f"  vault shape:      {result.shape}")
    emit(f"  indexable files:  {len(result.text_files)}")
    emit("")
    emit("Excluded as noise (pre-filled into exclude_prefixes):")
    for prefix, reason in result.excluded:
        emit(f"  {prefix:<28} WHY: {reason}")
    if result.omitted_exclusions:
        emit("")
        emit("Detected as noise but not written (routes.json would refuse the name):")
        for prefix, reason in result.omitted_exclusions:
            emit(f"  {prefix:<28} WHY: {reason}")
    if result.mirror_prefixes:
        emit("")
        emit("Detected as mirror/snapshot copies (mirror_prefixes):")
        for prefix in result.mirror_prefixes:
            emit(f"  {prefix}")
    emit("")

    if result.routes:
        emit(f"Inferred {len(result.routes)} route(s). ALL OF THEM ARE GUESSES:")
        for route in result.routes:
            emit("")
            emit(f"  route \"{route.name}\"  ({route.file_count} files)")
            for reason in route.why:
                emit(f"    WHY: {reason}")
            emit(f"    triggers:  {', '.join(route.triggers) if route.triggers else '(none)'}")
            if route.canonical_sources:
                emit("    canonical sources:")
                for path in route.canonical_sources:
                    emit(f"      {path}")
                    emit(f"        WHY: {route.canonical_why.get(path, 'n/a')}")
            elif route.canonical_note:
                emit(f"    canonical sources: none -- {route.canonical_note}")
            else:
                emit("    canonical sources: none found; this route will retrieve "
                     "lexically only")
            if route.low_confidence:
                emit(f"    LOW CONFIDENCE: {route.low_confidence}")
    else:
        emit("Inferred 0 routes.")

    if result.skipped_groups:
        emit("")
        emit("Not turned into routes:")
        for group, reason in result.skipped_groups:
            emit(f"  {group:<28} WHY: {reason}")

    for note in result.notes:
        emit("")
        emit(f"NOTE: {note}")

    emit("")
    if written:
        emit(f"Wrote {rel_out}")
    else:
        emit(f"Nothing written (--print-only). Target would be {rel_out}")
    emit("")
    emit("This is a starting guess, not a working config. The scan read folder names,")
    emit("filenames, headings and file sizes. It did not read anything for meaning.")
    emit("")
    emit("Edit these three things by hand before you trust a packet:")
    emit("  1. TRIGGERS  - replace the inferred tokens with the phrases you actually")
    emit("                 type. Multi-word phrases are what make routing precise.")
    emit("  2. CANONICAL_SOURCES - each one must be a document that MUST be opened when")
    emit("                 the route fires. Size picked these; you pick authority.")
    emit("  3. SUPERSEDED RULES - any file replaced by a newer one needs")
    emit("                 rule_state: \"superseded\" + superseded_by, or the old rule")
    emit("                 can win on lexical rank.")
