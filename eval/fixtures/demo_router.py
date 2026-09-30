#!/usr/bin/env python3
"""A deliberately small, fake context router.

It exists so that `evaluate.py`, `score_packet.py`, PROMOTION_GATE.md and
ABLATION.md can be demonstrated end to end without anyone's private vault. It
is NOT a good retrieval system and is not meant to be one. It reproduces, in
about 150 lines, the three layers that the ablation method is designed to
attribute capacity loss to:

  1. a fast path  -- a matched "answer card" short-circuits retrieval entirely
                     (disable with --no-fast-path)
  2. a tiered budget -- short prompts get a small packet (force the large tier
                     with --full)
  3. a canonical floor -- rule documents that may be shortened but never
                     dropped (disable with --no-canonical-floor)

Usage:
    python3 demo_router.py [flags] "the user prompt"

The prompt is the last positional argument, which is the contract evaluate.py
expects. Output goes to stdout and nothing is written anywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "router"))
from textio import configure_stdout  # noqa: E402

HERE = Path(__file__).resolve().parent
DOCS = HERE / "docs"

# route keyword -> documents this route may not answer without
ROUTES = {
    "release": (("release", "deploy", "ship", "changelog", "rollback"),
                ["release-checklist.md", "api-versioning.md"]),
    "authoring": (("write", "page", "summary", "style", "voice", "screenshot"),
                  ["style-guide.md"]),
    "onboarding": (("onboard", "access", "writer", "joins", "new"),
                   ["onboarding.md"]),
    "incident": (("incident", "outage", "on-call", "oncall", "down"),
                 ["incident-runbook.md"]),
    "retention": (("delete", "deleted", "retention", "recover", "analytics"),
                  ["data-retention.md"]),
}

# fast-path answer cards: claim + the single source it was verified against
CARDS = {
    "summary-length": {
        "triggers": ("summary", "characters", "length"),
        "claim": "Page summaries are 80 to 100 characters of visible text.",
        "source": "style-guide.md",
    },
    "version-scheme": {
        "triggers": ("version", "versioning", "calver", "semver"),
        "claim": "The current versioning scheme is CalVer (YYYY.MM.PATCH).",
        "source": "api-versioning.md",
    },
}

TIERS = {"brief": 2, "standard": 3, "full": 6}


def tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def pick_tier(prompt: str, force_full: bool) -> str:
    if force_full:
        return "full"
    words = len(prompt.split())
    if words < 5:
        return "brief"
    if words <= 15:
        return "standard"
    return "full"


def matched_routes(prompt_tokens: set[str]) -> list[str]:
    hits = []
    for name, (keywords, _docs) in ROUTES.items():
        if prompt_tokens & set(keywords):
            hits.append(name)
    return hits


def matched_card(prompt_tokens: set[str]) -> tuple[str, dict] | None:
    for name, card in CARDS.items():
        if prompt_tokens & set(card["triggers"]):
            return name, card
    return None


def render_card_packet(prompt: str, name: str, card: dict) -> str:
    path = DOCS / card["source"]
    return (
        f"# Context packet (fast activation)\n\n"
        f"## Exact user prompt\n```text\n{prompt}\n```\n\n"
        f"Evidence status: **SUPPORTED**\n\n"
        f"### {name} — {card['claim']}\n"
        f"- Evidence grade: `verified`\n"
        f"- Source: `docs/{card['source']}`\n"
        f"- Source SHA-256: `{sha(path)}`\n"
    )


def render_packet(prompt: str, chosen: list[Path]) -> str:
    out = [
        "# Context packet",
        "",
        "## Exact user prompt",
        "```text",
        prompt,
        "```",
        "",
        "Evidence status: **SUPPORTED**",
        "",
    ]
    for i, path in enumerate(chosen, 1):
        body = path.read_text(encoding="utf-8").strip()
        out += [
            f"### E{i:03d} — `docs/{path.name}`",
            f"- Locator: `docs/{path.name}`",
            "- Timestamp: `2026-09-15`",
            "- Authority class: `canonical`",
            "- Source hash state: `verified`",
            "",
            body,
            "",
        ]
    return "\n".join(out)


def main() -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", default=str(DOCS), help="document directory")
    ap.add_argument("--full", action="store_true", help="force the largest budget tier")
    ap.add_argument("--no-fast-path", action="store_true", help="never short-circuit on an answer card")
    ap.add_argument("--no-canonical-floor", action="store_true", help="let the budget evict rule documents")
    ap.add_argument("prompt", help="the user prompt (last positional argument)")
    args = ap.parse_args()

    docs_dir = Path(args.docs)
    prompt_tokens = tokens(args.prompt)

    if not args.no_fast_path and not args.full:
        card = matched_card(prompt_tokens)
        if card:
            print(render_card_packet(args.prompt, *card))
            return 0

    tier = pick_tier(args.prompt, args.full)
    limit = TIERS[tier]

    # lexical candidates, best overlap first
    scored = []
    for path in sorted(docs_dir.glob("*.md")):
        overlap = len(prompt_tokens & tokens(path.read_text(encoding="utf-8")))
        if overlap:
            scored.append((overlap, path.name))
    scored.sort(key=lambda p: (-p[0], p[1]))
    lexical = [name for _s, name in scored]

    canonical: list[str] = []
    for route in matched_routes(prompt_tokens):
        for name in ROUTES[route][1]:
            if name not in canonical:
                canonical.append(name)

    # This is the layer the ablation flag turns off. With the floor on, at least
    # CANONICAL_FLOOR rule documents survive any budget; with it off the quota
    # collapses to zero exactly when the budget is tightest.
    floor = 0 if args.no_canonical_floor else min(2, len(canonical))
    quota = max(floor, limit - min(limit, len(lexical)))

    chosen: list[str] = canonical[:quota]
    for name in lexical:
        if len(chosen) >= limit:
            break
        if name not in chosen:
            chosen.append(name)

    print(render_packet(args.prompt, [docs_dir / n for n in chosen]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
