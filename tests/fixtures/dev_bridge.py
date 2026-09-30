"""Development set for synaptic retrieval: a fictional linked vault plus labelled questions.

This is a DEV set: it is used while developing context_layer/synapse.py and is
kept separate from any sealed benchmark. Everything is invented. `build(root)`
writes the vault and returns the cases; the generator is deterministic.

Question types:
- direct:    the answer sits in the note the question names (a control).
- bridge:    the question names one note ("the lead of Project Lantern"); the
             answer sits in a linked note that shares no distinctive word with
             the question. Required: the linking line in the named note AND the
             answer line in the linked note.
- aggregate: the answer is spread over three linked notes (plus the linking line).
- prose:     a bridge whose link sits in running prose that shares no word with the
             question ("... is steered by [[Someone]] ..." / "the steward of ..."), so
             the link line itself gives no lexical hint. Added after the first dev run,
             to keep the dev set from rewarding one signal only.

The generator checks its own labels: every required span occurs verbatim in its
note, and for bridge/aggregate cases no distinctive question word occurs in an
answer note.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

FIRST = ["Mira", "Tomas", "Ilse", "Orin", "Petra", "Caius", "Wenna", "Bram", "Selka", "Dario",
         "Nell", "Anouk", "Fenn", "Lusa", "Ravi", "Edda", "Joss", "Kalle", "Yara", "Hugo",
         "Mette", "Soren", "Ines", "Otto", "Vela", "Rune", "Tilde", "Aksel", "Brisa", "Colm",
         "Dunja", "Emil", "Frida", "Gideon", "Hanne", "Ivo"]
LAST = ["Stone", "Reed", "Varga", "Maple", "Quill", "Harrow", "Lind", "Okafor", "Brandt", "Castel",
        "Dunmore", "Ekberg", "Falk", "Gorse", "Holm", "Iver", "Jessop", "Kerr", "Lowe", "Marsh",
        "Nyberg", "Orme", "Pike", "Rask", "Sallow", "Thorne", "Ulm", "Voss", "Wick", "Yates",
        "Zorn", "Albright", "Birch", "Crane", "Dahl", "Ellery"]
TOWNS = ["Port Velar", "Kessa", "Drummond Ferry", "Olvik", "Saltmere", "Brackwater", "Tiller Cove",
         "Norrby", "Heddon", "Carrow", "Lissane", "Ember Hollow", "Quarrington", "Valmo", "Wexley",
         "Astby", "Brume", "Corran", "Dalsett", "Fenwick", "Gallow Rise", "Hesk", "Istra", "Jorvale",
         "Kelda", "Larnoch", "Moss End", "Nethery", "Orlaith", "Penhale", "Rookby", "Sennet",
         "Tarn Hill", "Uskmoor", "Vardo", "Wrenfield"]
INSTRUMENTS = ["cello", "oboe", "banjo", "harp", "bassoon", "accordion", "viola", "zither",
               "mandolin", "flute", "tuba", "marimba"]
SUBJECTS = ["hydrology", "glass chemistry", "cartography", "forestry", "acoustics", "metallurgy",
            "linguistics", "soil science", "optics", "tidal mechanics", "botany", "geodesy"]
DESKS = ["4B, north wing", "2F, east annex", "7A, mezzanine", "1C, courtyard side",
         "3D, records floor", "5E, south wing", "6A, tower room", "2B, garden side"]
PROJECTS = ["Lantern", "Cobalt Gate", "Tern", "Halyard", "Juniper", "Ostrich Bay", "Quartzline",
            "Saffron", "Umber", "Wickerwork", "Brightwater", "Moth"]
VENTURES = ["Kestrel Yard", "Ironbark", "Solace Pier", "Thimble", "Greywater", "Nightjar"]
VENTURE_PURPOSES = ["restores the tram shelters", "catalogues the seed bank",
                    "repaints the harbour buoys", "repairs the market clocks",
                    "reroutes the storm drains", "surveys the hedgerows"]
CREWS = ["Harbor Crew", "Signal Crew", "Orchard Crew", "Kiln Crew", "Bridge Crew", "Archive Crew",
         "Lighthouse Crew", "Canal Crew"]
PURPOSES = ["replaces the ferry timetable service", "moves the tide gauges to solar power",
            "rebuilds the grain ledger", "digitises the lighthouse logbooks",
            "retires the old pager network", "maps the drainage culverts",
            "upgrades the pier cranes", "rewrites the market stall permits",
            "consolidates the weather stations", "audits the bridge sensors",
            "migrates the library catalogue", "standardises the canal locks"]
DUTIES = ["maintains the pier cranes", "keeps the signal lamps lit", "tends the cider orchards",
          "fires the brick kilns", "inspects the swing bridges", "catalogues the parish records",
          "services the lighthouse lenses", "clears the canal weirs"]

STOP = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "is", "are", "was",
        "what", "which", "who", "whom", "where", "when", "how", "does", "do", "did", "can", "be",
        "by", "with", "from", "that", "this", "their", "its", "his", "her", "they", "three"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP}


def person_note(name: str, info: dict, order: list[str]) -> str:
    sections = {
        "background": f"## Background\n\nStudied {info['subject']} before joining. "
                      f"Keeps a tidy notebook of every decision.",
        "home": f"## Home\n\nBased in {info['town']}, a short walk from the water.",
        "hobbies": f"## Hobbies\n\nPlays the {info['instrument']} on weekends.",
        "desk": f"## Desk\n\nDesk {info['desk']}.",
    }
    body = [f"# {name}", "",
            f"{name} joined the cooperative in {info['year']} and works on public "
            f"infrastructure.", ""]
    for key in order:
        body += [sections[key], ""]
    return "\n".join(body)


def build(root: Path, seed: int = 11) -> list[dict]:
    """Write the dev vault under root; return the labelled cases."""
    rng = random.Random(seed)
    root = Path(root)
    people = []
    for first, last in zip(FIRST, LAST):
        name = f"{first} {last}"
        info = {"town": TOWNS[len(people)], "instrument": rng.choice(INSTRUMENTS),
                "subject": rng.choice(SUBJECTS), "desk": rng.choice(DESKS),
                "year": rng.randint(2009, 2024)}
        order = ["background", "home", "hobbies", "desk"]
        rng.shuffle(order)
        people.append((name, info, order))
    notes: dict[str, str] = {}
    for name, info, order in people:
        notes[f"people/{name}.md"] = person_note(name, info, order)
    # A hub every person links to (degree > the adjacency cap).
    notes["people/Directory.md"] = "# Directory\n\nEveryone in the cooperative:\n\n" + "\n".join(
        f"- [[{name}]]" for name, _, _ in people) + "\n"
    cases: list[dict] = []
    pool = list(range(len(people)))
    rng.shuffle(pool)
    attributes = [
        ("town", "In which town does the lead of Project {p} reside?",
         lambda i: f"Based in {i['town']}, a short walk from the water."),
        ("instrument", "Which musical instrument does the lead of Project {p} own?",
         lambda i: f"Plays the {i['instrument']} on weekends."),
        ("subject", "What academic field was the lead of Project {p} trained in?",
         lambda i: f"Studied {i['subject']} before joining."),
        ("desk", "Where is the lead of Project {p} seated in the office building?",
         lambda i: f"Desk {i['desk']}."),
    ]
    for index, project in enumerate(PROJECTS):
        lead = people[pool[index]]
        members = [people[pool[(index + 12 + k) % len(pool)]] for k in range(2)]
        path = f"projects/{slug(project)}.md"
        lead_line = f"Lead: [[{lead[0]}]]"
        notes[path] = "\n".join([
            f"# Project {project}", "",
            f"Project {project} {PURPOSES[index]}. Status: active since {2020 + index % 5}.", "",
            lead_line,
            "Members: " + ", ".join(f"[[{m[0]}]]" for m in members), "",
            "## Milestones", "",
            f"Pilot in the first quarter, full rollout in the third. Budget review in "
            f"{['March', 'June', 'September', 'December'][index % 4]}.", ""])
        cases.append({"id": f"D{index + 1:02d}", "type": "direct",
                      "question": f"What does Project {project} do and what is its status?",
                      "required": [{"source_path": path,
                                    "text": f"Project {project} {PURPOSES[index]}."}]})
        for offset in (0, 1):
            key, template, answer = attributes[(index * 2 + offset) % len(attributes)]
            cases.append({"id": f"B{index * 2 + offset + 1:02d}", "type": "bridge",
                          "question": template.format(p=project),
                          "required": [{"source_path": path, "text": lead_line},
                                       {"source_path": f"people/{lead[0]}.md",
                                        "text": answer(lead[1])}]})
    for index, venture in enumerate(VENTURES):
        steward = people[pool[(index + 20) % len(pool)]]
        helper = people[pool[(index + 27) % len(pool)]]
        path = f"ventures/{slug(venture)}.md"
        # Even ventures: one long line (Obsidian style). Odd ventures: hard-wrapped, so
        # the line holding the link carries no word of the question at all.
        joiner = " " if index % 2 == 0 else "\n"
        prose = (f"The {venture} venture {VENTURE_PURPOSES[index]}.{joiner}It is steered by "
                 f"[[{steward[0]}]], who reports to the council each month.")
        notes[path] = "\n".join([
            f"# {venture}", "", prose, "",
            f"Volunteers are welcome; [[{helper[0]}]] keeps the rota.", ""])
        for offset, (key, template, answer) in enumerate(
                [("town", "In which town does the steward of the {v} venture reside?",
                  lambda i: f"Based in {i['town']}, a short walk from the water."),
                 ("subject", "What academic field was the steward of the {v} venture trained in?",
                  lambda i: f"Studied {i['subject']} before joining.")]):
            cases.append({"id": f"P{index * 2 + offset + 1:02d}", "type": "prose",
                          "question": template.format(v=venture),
                          "required": [{"source_path": path, "text": f"[[{steward[0]}]]"},
                                       {"source_path": f"people/{steward[0]}.md",
                                        "text": answer(steward[1])}]})
    for index, crew in enumerate(CREWS):
        chosen = [people[pool[(index * 3 + k + 5) % len(pool)]] for k in range(3)]
        path = f"crews/{slug(crew)}.md"
        crew_line = "Crew: " + ", ".join(f"[[{c[0]}]]" for c in chosen)
        notes[path] = "\n".join([
            f"# {crew}", "", f"The {crew.lower()} {DUTIES[index]}.", "", crew_line, "",
            "## Schedule", "", "Shifts rotate on the first of each month.", ""])
        if index % 2 == 0:
            question = f"In which towns do the members of the {crew} reside?"
            answers = [f"Based in {c[1]['town']}, a short walk from the water." for c in chosen]
        else:
            question = f"Which musical instruments do the members of the {crew} own?"
            answers = [f"Plays the {c[1]['instrument']} on weekends." for c in chosen]
        cases.append({"id": f"A{index + 1:02d}", "type": "aggregate", "question": question,
                      "required": [{"source_path": path, "text": crew_line}] + [
                          {"source_path": f"people/{c[0]}.md", "text": a}
                          for c, a in zip(chosen, answers)]})
    # Distractors that share question words but answer nothing.
    notes["notes/town-hall.md"] = ("# Town hall\n\nThe town hall reading room opens at nine. "
                                   "Residents reside nearby and the lead architect retired.\n")
    notes["notes/music-library.md"] = ("# Music library\n\nThe library lends a musical "
                                       "instrument for a week; owners must sign the ledger.\n")
    notes["notes/office-building.md"] = ("# Office building\n\nThe office building has four "
                                         "floors; seating is assigned by the facilities desk.\n")
    notes["notes/training.md"] = ("# Training\n\nNew staff are trained in safety; the academic "
                                  "field trips run in spring.\n")
    for relative, text in notes.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # This controlled fixture must have identical source hashes on every OS.
        target.write_bytes(text.encode("utf-8"))
    _check(root, cases)
    return cases


def _check(root: Path, cases: list[dict]) -> None:
    for case in cases:
        for item in case["required"]:
            text = (root / item["source_path"]).read_text(encoding="utf-8")
            if item["text"] not in text:
                raise ValueError(f"{case['id']}: span not in {item['source_path']}")
        if case["type"] == "direct":
            continue
        asked = words(case["question"])
        for item in case["required"][1:]:
            shared = asked & words((root / item["source_path"]).read_text(encoding="utf-8"))
            if shared:
                raise ValueError(f"{case['id']}: answer note shares {sorted(shared)}")


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(build(Path(sys.argv[1])), indent=1))
