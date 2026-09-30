#!/usr/bin/env python3
"""Create a small fictional vault for trying Context Layer Brain View.

    python3 make-demo-vault.py DEMO_DIR [--notes 400] [--seed 7] [--no-trace]
                                        [--advisor none|shadow|on]

Writes Markdown notes with wikilinks and tags into DEMO_DIR (all names are
generated), a sample `.context/activation.json` stamped with the current time
so the overlay shows immediately, and installs nothing. The sample trace has
the fields context-layer writes (status PARTIAL, mode superset); with
--advisor it also carries a sample advisor block (enums and counters only),
so the advisor overlay can be tried without any advisor installed. Open
DEMO_DIR as a vault in Obsidian, copy the plugin files into
DEMO_DIR/.obsidian/plugins/context-layer-brain/, and enable the plugin.

The target directory must be empty or a vault this script created before
(it contains `.demo-vault`); anything else is refused.
"""

import argparse
import json
import random
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

AREAS = ["astronomy", "botany", "cartography", "ceramics", "glaciers", "harbors", "orchards", "robotics"]
WORDS = ["amber", "basalt", "cinder", "delta", "ember", "fjord", "garnet", "heron", "iris", "juniper",
         "kestrel", "lichen", "meadow", "nimbus", "onyx", "prairie", "quartz", "reef", "sable", "tundra"]
MARKER = ".demo-vault"


def title(rng, i):
    return f"{rng.choice(WORDS).title()} {rng.choice(WORDS)} {i}"


def build(target, count, seed, with_trace, advisor="none"):
    rng = random.Random(seed)
    notes = []
    for i in range(count):
        area = AREAS[i % len(AREAS)] if i % 13 else ""
        notes.append({"area": area, "title": title(rng, i), "links": set(), "tag": rng.choice(WORDS)})
    linked = int(count * 0.7)
    for i in range(1, linked):
        j = rng.randrange(min(i, 10)) if rng.random() < 0.3 else rng.randrange(i)
        notes[i]["links"].add(j)
    for _ in range(int(linked * 0.5)):
        a, b = rng.randrange(linked), rng.randrange(linked)
        if a != b:
            notes[a]["links"].add(b)

    for note in notes:
        folder = target / note["area"] if note["area"] else target
        folder.mkdir(parents=True, exist_ok=True)
        body = [f"---\ntags: [{note['tag']}]\n---", f"# {note['title']}", "",
                f"A fictional note about {note['tag']} for the demo vault.", ""]
        body += [f"- Related: [[{notes[j]['title']}]]" for j in sorted(note["links"])]
        (folder / f"{note['title']}.md").write_text("\n".join(body) + "\n", encoding="utf-8")

    (target / MARKER).write_text("Created by make-demo-vault.py. Safe to delete.\n", encoding="utf-8")
    if not with_trace:
        return
    path_of = lambda n: (n["area"] + "/" if n["area"] else "") + n["title"] + ".md"
    seed_index = max(range(linked), key=lambda k: (len(notes[k]["links"]), -k))
    hops = sorted(notes[seed_index]["links"])[:4]
    second = sorted({j for h in hops for j in notes[h]["links"]} - set(hops) - {seed_index})[:4]
    nodes = [{"path": path_of(notes[seed_index]), "activation": 1.0, "hop": 0, "role": "seed", "selected": True}]
    nodes += [{"path": path_of(notes[h]), "activation": round(0.7 - 0.1 * k, 2), "hop": 1, "role": "hop", "selected": k < 2}
              for k, h in enumerate(hops)]
    nodes += [{"path": path_of(notes[s]), "activation": round(0.3 - 0.05 * k, 2), "hop": 2, "role": "hop", "selected": False}
              for k, s in enumerate(second)]
    edges = [{"from": path_of(notes[seed_index]), "to": path_of(notes[h]), "kind": "wikilink", "weight": 0.6,
              "anchor": {"path": path_of(notes[seed_index]), "line": 6}} for h in hops]
    for s in second:
        parent = next(h for h in hops if s in notes[h]["links"])
        edges.append({"from": path_of(notes[parent]), "to": path_of(notes[s]), "kind": "wikilink", "weight": 0.25,
                      "anchor": {"path": path_of(notes[parent]), "line": 6}})
    trace = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": secrets.token_hex(16),
        "query": None,
        "method": "synaptic",
        "mode": "superset",
        "budget_tokens": 600,
        "nodes": nodes,
        "edges": edges,
        "packet": {"passages": 1 + min(2, len(hops)), "est_tokens": 640, "status": "PARTIAL"},
    }
    if advisor != "none" and len(nodes) > 3:
        # A sample verdict per note: one rescued, one judged off-topic, the rest on topic.
        applied = advisor == "on"
        for k, node in enumerate(nodes):
            node["jev"] = "rescued" if k == 3 else ("off_topic" if k == 2 else "on_topic")
        if applied:
            nodes[3]["selected"] = True
        trace["jev"] = {"mode": advisor, "applied": applied, "superset": True, "provider_kind": "recorded",
                        "gate_passed": True, "kept": len(nodes) - 2, "flagged": 1, "rescued": 1, "degraded": False}
    (target / ".context").mkdir(exist_ok=True)
    (target / ".context" / "activation.json").write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path)
    parser.add_argument("--notes", type=int, default=400)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--no-trace", action="store_true", help="do not write .context/activation.json")
    parser.add_argument("--advisor", choices=["none", "shadow", "on"], default="none",
                        help="add a sample advisor block to the trace (default: none)")
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    if target.exists() and any(target.iterdir()) and not (target / MARKER).exists():
        sys.exit(f"refusing to write into non-empty directory {target} (no {MARKER} marker)")
    if not 10 <= args.notes <= 20000:
        sys.exit("--notes must be between 10 and 20000")
    target.mkdir(parents=True, exist_ok=True)
    build(target, args.notes, args.seed, not args.no_trace, args.advisor)
    print(f"demo vault written to {target} ({args.notes} notes)")


if __name__ == "__main__":
    main()
