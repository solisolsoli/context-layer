"""Development set for the note-name, alias and heading index fields.

A DEV set, written while developing the opt-in `--name-fields` index option; it is
kept separate from every sealed benchmark and was not derived from one. Everything
is invented. `build(root)` writes a fictional vault and returns labelled cases; the
generator is deterministic.

Families:
- name:    the question is (or contains) the note's file name; the note's text never
           uses those words ("Kestrel Yard.md" says "Restores the tram shelters").
- alias:   the question is an alias from the note's frontmatter; the text never uses it.
- heading: the question is a heading of the note; the heading's words are absent from
           the text below it.
- both:    a content question that the plain index already answers, whose note also has
           a name and headings (a control: the option must not lose these).

Distractor notes ("misc/Decoy N.md") repeat one word of some names in their text, so a
question that is only a note's name also matches notes that merely say one of its words.
- absent:  a question whose words occur nowhere (both indexes must return nothing).

The generator checks its own labels: no question word of a name/alias/heading case
occurs in the required note's text, and every required span occurs verbatim.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

STOP = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "is", "are", "was",
        "what", "which", "who", "where", "when", "how", "does", "do", "did", "can", "be", "by",
        "with", "from", "that", "this", "tell", "me", "about", "show", "find", "note", "notes"}

NAMES = ["Kestrel Yard", "Ironbark Lane", "Solace Pier", "Thimble Works", "Greywater Cut",
         "Nightjar Row", "Copperfield Hall", "Larkspur Gate", "Marrow Bridge", "Fennel Dock",
         "Quillon Hill", "Sable Court", "Tamarind Stairs", "Umber Mill", "Vesper Green",
         "Wren Alley"]
NAME_TEXTS = ["Restores the tram shelters. Opened in {year}.",
              "Catalogues the seed bank and lends packets each spring.",
              "Repaints the harbour buoys before the autumn fog.",
              "Repairs the market clocks; the workshop opens at eight.",
              "Reroutes the storm drains after every heavy season.",
              "Surveys the hedgerows and reports to the parish.",
              "Keeps the ledger of lantern oil and wicks.",
              "Trains volunteers for the ferry rope crews.",
              "Sorts donated boots and sends them inland.",
              "Measures the tide with a copper gauge, daily.",
              "Runs the winter kitchen for the night shifts.",
              "Grinds flour for the three nearest bakeries.",
              "Mends fishing nets on Thursdays.",
              "Teaches chart reading to the school groups.",
              "Weighs the produce for the Saturday stalls.",
              "Stores the spare bell ropes and pulleys."]
ALIASES = ["Dockhands", "Buoy Painters", "Seedkeepers", "Clockmenders", "Drain Wardens",
           "Hedge Scouts", "Wickkeepers", "Rope Crew", "Boot Sorters", "Gauge Readers",
           "Soup Line", "Flour Grinders", "Net Menders", "Chart Tutors", "Stall Weighers",
           "Bell Keepers"]
HEADINGS = [("Fog Horn Schedule", "Sounded at dawn and dusk from October."),
            ("Rope Splicing Course", "Six evenings, beginners welcome."),
            ("Lamp Oil Ordering", "Ordered in bulk every second month."),
            ("Weir Inspection Rota", "Two people walk the length together."),
            ("Kiln Firing Calendar", "Fired on the first Monday of each month."),
            ("Orchard Pruning Guide", "Prune after the first hard frost."),
            ("Ferry Crossing Fares", "Paid in coin at the landing."),
            ("Signal Flag Meanings", "A chart hangs beside the door.")]
FILLER = ["The team meets on Tuesdays.", "Coffee is provided.", "Minutes are kept in the shared folder.",
          "Volunteers are welcome throughout the year.", "Requests go through the front desk."]
CONTENT_FACTS = [("boiler", "The boiler room key hangs behind the reception desk."),
                 ("archive", "The archive closes at four on Fridays."),
                 ("garden", "The rooftop garden needs watering every second day."),
                 ("lockers", "Locker numbers are handed out by the porter."),
                 ("bicycles", "Bicycles are stored under the east stairwell."),
                 ("kettle", "The kettle in the annex is descaled monthly.")]
DECOY_WORDS = ["yard", "pier", "lane", "hall", "gate", "dock"]


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP}


def build(root: Path, seed: int = 23) -> list[dict]:
    """Write the dev vault under `root`; return the labelled cases."""
    rng = random.Random(seed)
    root = Path(root)
    notes: dict[str, str] = {}
    cases: list[dict] = []
    for index, name in enumerate(NAMES):
        text = NAME_TEXTS[index].format(year=2010 + index)
        extra = rng.choice(FILLER)
        alias = ALIASES[index]
        heading = HEADINGS[index % len(HEADINGS)]
        front = f"---\naliases: [{alias}]\n---\n" if index % 2 == 0 else ""
        section = f"\n## {heading[0]}\n\n{heading[1]}\n" if index % 4 in (1, 2) else ""
        notes[f"places/{name}.md"] = f"{front}{text}\n\n{extra}\n{section}"
        cases.append({"id": f"N{index + 1:02d}", "type": "name", "question": name,
                      "required": [{"source_path": f"places/{name}.md", "text": text}]})
        cases.append({"id": f"NQ{index + 1:02d}", "type": "name",
                      "question": f"Tell me about {name}",
                      "required": [{"source_path": f"places/{name}.md", "text": text}]})
        if index % 2 == 0:
            cases.append({"id": f"A{index + 1:02d}", "type": "alias", "question": alias,
                          "required": [{"source_path": f"places/{name}.md", "text": text}]})
        if index % 4 in (1, 2):
            cases.append({"id": f"H{index + 1:02d}", "type": "heading", "question": heading[0],
                          "required": [{"source_path": f"places/{name}.md",
                                        "text": heading[1]}]})
    for index, (word, fact) in enumerate(CONTENT_FACTS):
        title = f"Office {word.title()} Facts"
        notes[f"office/{title}.md"] = f"# {title}\n\n{fact}\n"
        cases.append({"id": f"C{index + 1:02d}", "type": "both",
                      "question": f"What do we know about the {word}?",
                      "required": [{"source_path": f"office/{title}.md", "text": fact}]})
    for index, word in enumerate(DECOY_WORDS):
        # Notes that merely say one word of a name: distractors for the name questions.
        notes[f"misc/Decoy {index + 1}.md"] = (
            f"The {word} was mentioned in the minutes twice, and again in the annual letter about "
            f"the {word} committee.\n")
    for index, word in enumerate(["zeppelin hangar", "quokka sanctuary", "obelisk garage"]):
        cases.append({"id": f"X{index + 1:02d}", "type": "absent", "question": word,
                      "required": []})
    for relative, text in notes.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _check(root, cases)
    return cases


def _check(root: Path, cases: list[dict]) -> None:
    for case in cases:
        for item in case["required"]:
            text = (root / item["source_path"]).read_text(encoding="utf-8")
            if item["text"] not in text:
                raise ValueError(f"{case['id']}: span not in {item['source_path']}")
            if case["type"] in ("name", "alias", "heading"):
                body = text.split("\n---\n", 1)[-1] if text.startswith("---\n") else text
                body = "\n".join(line for line in body.splitlines() if not line.startswith("#"))
                shared = words(case["question"]) & words(body)
                if shared:
                    raise ValueError(f"{case['id']}: the note's text shares {sorted(shared)}")


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(build(Path(sys.argv[1])), indent=1))
