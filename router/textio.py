"""Stable UTF-8 command output without platform newline translation."""
from __future__ import annotations

import sys


def configure_stdout(stream=None) -> None:
    """Preserve verbatim CRLF passages and use LF for generated framing.

    Only command entry points call this. Importing a library must not change
    its caller's streams; redirected in-memory test streams need no setup.
    """
    reconfigure = getattr(sys.stdout if stream is None else stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", newline="\n")
