"""The advisor's memory review (`jev review-memory`) and its status check (`jev status --check`).

Memory review (feature `memory`): the mechanical part runs in every mode without a model;
the four questions (support, commitment, kind, relation to each prior record) go through the
same provider, gates, cache, freshness check, kill switch and counters as `search --jev`;
the store is only ever read; `off` makes no call, `shadow` prints what `off` prints, `on`
(with a receipt) adds the advisor's block and route. Status check: `jev_client.probe` reaches
only a loopback provider, or asks `claude --version`, and is bounded in time.

Offline by construction: every provider is a scripted function or a script this file writes
(`CONTEXT_LAYER_JEV_FAKE`), every server is a loopback `http.server` fake, `claude` is a fake
script on a temporary PATH, HOME points into a temporary directory, and the vaults are
fictional. Run: python3 tests/test_jev_memory.py
"""
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from _portable_helpers import isolated_home_env

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))

from context_layer import jev, jev_client, jev_contracts as contracts, memory  # noqa: E402

ROUTES = {"record_type_allowlist": ["verbatim_text_file"],
          "routes": {"notes": {"priority": 1, "triggers": ["note"], "canonical_sources": [],
                               "path_hints": []}},
          "fallback_routes": [], "aliases": {}, "exclude_prefixes": ["private"]}
CLEAN_ENV = {k: v for k, v in os.environ.items()
             if not k.startswith("CONTEXT_LAYER_JEV") and k != "PYTHONPATH"}

APRIL = ("# Harbour Log April\n\n## Fuel pump\n\nThe fuel pump on the east pier was tested and "
         "passed its safety check.\n\n## Night watch\n\nThe night watch rota now starts at 8 in "
         "the evening instead of 10.\n")
MARCH = "# Harbour Log March\n\n## Night watch\n\nThe night watch rota starts at 10 in the evening.\n"
NIGHT_SPAN = "The night watch rota now starts at 8 in the evening instead of 10."
NIGHT_TEXT = "The night watch rota now starts at 8 in the evening instead of 10."
PRIOR_TEXT = "The night watch rota starts at 10 in the evening."
SECRET_LINE = "token = abcdefghijklmnopqrstuvwxyz"
DEFAULT_PICKS = {"memory_support.v1": "supports", "memory_commitment.v1": "asserted",
                 "memory_kind.v1": "decision", "memory_relation.v1": "replaces"}
PURPOSES = ("memory_support", "memory_commitment", "memory_kind", "memory_relation")
FAKE_PROFILE = {"kind": "fake", "probabilities": "optional", "rounding": None}
LABEL_ONLY_PROFILE = {"kind": "fake", "probabilities": "none", "rounding": None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class NetworkAttempted(BaseException):
    """Raised (never caught by code under test) when a test that must stay off the network
    connects anywhere."""


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def line_span(text: str, span: str) -> tuple:
    start = text[:text.index(span)].count("\n") + 1
    return start, start + span.count("\n")


def choice_result(questionnaire, pick, confidence=0.94, label_only=False, model="fake-judge-1"):
    """A jev_client.evaluate result for one choice question, built with the real validator."""
    options = list(questionnaire["question"]["criteria"])
    rest = (1.0 - confidence) / max(1, len(options) - 1)
    probabilities = None if label_only else {o: (confidence if o == pick else rest)
                                             for o in options}
    raw = {"q": {"type": "choice", "choice": pick, "probabilities": probabilities}}
    answer = contracts.validate_answer(questionnaire, raw,
                                       LABEL_ONLY_PROFILE if label_only else FAKE_PROFILE)
    return {"ok": True, "answer": answer, "code": None, "latency_ms": 2,
            "usage": {"input_tokens": 60, "output_tokens": 1}, "model_reported": model,
            "requests": 1}


class Judge:
    """A scripted provider with the jev_client.evaluate signature. `picks` maps a template id
    to a label (or to a function of the questionnaire); it records what it was sent."""

    def __init__(self, picks=None, confidence=0.94, label_only=False, during=None):
        self.picks = {**DEFAULT_PICKS, **(picks or {})}
        self.confidence, self.label_only, self.during = confidence, label_only, during
        self.seen, self.calls = [], 0

    def __call__(self, provider, questionnaires, *, deadline_s, max_parallel, key):
        self.calls += 1
        self.seen.extend(copy.deepcopy(questionnaires))
        if self.during:
            self.during()
        out = []
        for questionnaire in questionnaires:
            template = questionnaire["template"].split("@")[0]
            pick = self.picks[template]
            if callable(pick):
                pick = pick(questionnaire)
            out.append(choice_result(questionnaire, pick, self.confidence, self.label_only))
        return out

    def templates(self):
        return sorted(q["template"].split("@")[0] for q in self.seen)

    def text(self):
        return json.dumps(self.seen, ensure_ascii=False)


JUDGE_SCRIPT = '''#!{python}
import json, os, sys
q = json.loads(sys.stdin.read())
template = q["template"].split("@")[0]
if os.environ.get("JUDGE_LOG"):
    with open(os.environ["JUDGE_LOG"], "a") as handle:
        handle.write(template + "\\n")
default = {defaults}
options = list(q["question"]["criteria"])
pick = json.loads(os.environ.get("JUDGE_PICKS", "{{}}")).get(template, default[template])
confidence = float(os.environ.get("JUDGE_CONFIDENCE", "0.94"))
rest = (1.0 - confidence) / (len(options) - 1)
print(json.dumps({{"answers": {{"q": {{"type": "choice", "choice": pick, "probabilities":
                 {{o: (confidence if o == pick else rest) for o in options}}}}}},
                 "usage": {{"input_tokens": 60, "output_tokens": 1}}, "model": "fake-judge-1"}}))
'''


class Case(unittest.TestCase):
    """A temp root with HOME inside it, a fictional vault, and helpers to drive the review."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.vault = self.root / "vault"
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text(json.dumps(ROUTES))
        self.write("logbook/April.md", APRIL)
        self.write("logbook/March.md", MARCH)
        self.home_patch = mock.patch.dict(os.environ, isolated_home_env(os.environ, self.home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def write(self, name, text):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def prior(self, text=PRIOR_TEXT, path="logbook/April.md", kind="decision"):
        source = self.vault / path
        return memory.record(self.vault, kind=kind, text=text,
                             sources=[{"path": path, "sha256": sha_of(source)}])["id"]

    def proposal(self, text=NIGHT_TEXT, span=NIGHT_SPAN, path="logbook/April.md",
                 kind="decision", prior="default", **changes):
        source = self.vault / path
        start, end = line_span(source.read_text(encoding="utf-8"), span)
        obj = {"schema": "jev-memory-proposal/v1", "kind": kind, "text": text,
               "evidence": [{"path": path, "sha256": sha_of(source), "line_start": start,
                             "line_end": end, "span": span}]}
        if prior != "default":
            obj["prior"] = prior
        obj.update(changes)
        return obj

    def proposal_file(self, obj, name="proposal.json"):
        path = self.root / name
        path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
        return path

    # -- the advisor's files -------------------------------------------------
    def configure(self, mode="shadow", receipt=True, **overrides):
        obj = {"schema_version": 1, "mode": mode, "provider": {"kind": "fake"}, **overrides}
        jev.write_config(self.vault, obj)
        jev.ensure_salt(self.vault)
        if receipt and mode == "on":
            self.receipt()

    def receipt(self, purposes=PURPOSES, **changes):
        provider = {"kind": "fake"}
        path = jev.receipt_path(self.vault, provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"schema": "jev-calibration/v1", "provider": {"kind": "fake", "model": None},
                    "template_revision": contracts.TEMPLATE_REVISION,
                    "thresholds": dict(jev.DEFAULT_THRESHOLDS),
                    "purposes": {p: {"passed": True} for p in purposes},
                    "note": "test fixture, not a measurement"}
        document.update(changes)
        path.write_text(json.dumps(document), encoding="utf-8")

    def rows(self):
        path = self.vault / ".context" / "jev-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def memory_rows(self):
        return [r for r in self.rows() if r["feature"] == "memory"]

    def jev_files(self):
        return sorted(p.name for p in (self.vault / ".context").iterdir()
                      if p.name.startswith("jev"))

    def store(self):
        """Every byte under .context/memory, by relative name."""
        base = self.vault / ".context" / "memory"
        if not base.exists():
            return {}
        return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*"))
                if p.is_file()}

    # -- driving the review --------------------------------------------------
    def review(self, proposal=None, evaluate=None, environ=None, notes=None, mode_plan=None):
        environ = {} if environ is None else environ
        plan = jev.memory_plan(self.vault, environ=environ)
        return jev.review_memory(self.vault, proposal or self.proposal(), plan,
                                 evaluate_fn=evaluate, contracts=contracts, environ=environ,
                                 notes=notes)

    def cli(self, *argv, env=None):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", *argv], cwd=REPO,
                              capture_output=True, text=True,
                              env={**isolated_home_env(CLEAN_ENV, self.home), **(env or {})})

    def judge_script(self):
        script = self.root / "judge.py"
        script.write_text(JUDGE_SCRIPT.format(python=sys.executable,
                                              defaults=json.dumps(DEFAULT_PICKS)))
        if os.name != "nt": script.chmod(0o755)
        self.judge_log = self.root / "judge.log"
        return {"CONTEXT_LAYER_JEV_FAKE": str(script), "JUDGE_LOG": str(self.judge_log)}

    def judge_calls(self):
        if not self.judge_log.exists():
            return []
        return self.judge_log.read_text().splitlines()

    def review_cli(self, obj, *extra, env=None):
        return self.cli("jev", "review-memory", str(self.vault), "--proposal",
                        str(self.proposal_file(obj)), *extra, env=env)


# ---------------------------------------------------------------------------
# off, shadow, on
# ---------------------------------------------------------------------------

class Modes(Case):
    def test_the_feature_is_wired_and_status_says_so(self):
        self.assertIs(jev.WIRED["memory"], True)
        self.configure("shadow")
        done = self.cli("jev", "status", str(self.vault), "--json")
        info = json.loads(done.stdout)
        self.assertTrue(info["features"]["memory"]["available_in_this_version"])
        self.assertIn("memory proposal", info["features"]["memory"]["sends"])

    def test_off_asks_nothing_reads_no_key_and_never_loads_the_client(self):
        self.prior()
        env = self.judge_script()
        code = ("import json, sys\nfrom pathlib import Path\nfrom context_layer import jev\n"
                "vault = Path(sys.argv[1])\nplan = jev.memory_plan(vault)\n"
                "proposals, _ = jev.load_memory_proposals(Path(sys.argv[2]))\n"
                "report = jev.review_memory(vault, proposals[0], plan)\n"
                "print(json.dumps({'mode': plan.mode, 'why': plan.why, 'route': report['route'],\n"
                "                  'loaded': sorted(m for m in sys.modules if m.startswith("
                "'context_layer.jev_'))}))\n")
        proposal = self.proposal_file(self.proposal())
        done = subprocess.run([sys.executable, "-c", code, str(self.vault), str(proposal)],
                              cwd=REPO, capture_output=True, text=True,
                              env={**isolated_home_env(CLEAN_ENV, self.home), **env,
                                   "TYPESAFE_API_KEY": "fictional-key-4410"})
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(done.stdout)
        self.assertEqual((seen["mode"], seen["why"]), ("off", "not_configured"))
        self.assertEqual(seen["loaded"], [])            # neither the client nor the contracts
        self.assertEqual(self.judge_calls(), [])
        self.assertEqual(self.jev_files(), [])          # no file under .context/jev*

    def test_configured_off_and_a_disabled_feature_make_no_call(self):
        env = self.judge_script()
        self.configure("off")
        done = self.review_cli(self.proposal(), "--json", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("the advisor is off (mode_off)", done.stderr)
        off_stdout = done.stdout
        self.configure("shadow", features=["search"])
        done = self.review_cli(self.proposal(), "--json", env=env)
        self.assertEqual(done.stdout, off_stdout)
        self.assertIn("feature_disabled", done.stderr)
        self.assertEqual(self.judge_calls(), [])
        self.assertEqual([(r["mode"], r["code"]) for r in self.memory_rows()],
                         [("off", "feature_disabled")])

    def test_shadow_prints_what_off_prints_and_counts_the_answers(self):
        self.prior()
        env = self.judge_script()
        off = self.review_cli(self.proposal(), "--json", env=env)
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertEqual(self.judge_calls(), [])
        self.configure("shadow")
        shadow = self.review_cli(self.proposal(), "--json", env=env)
        self.assertEqual(shadow.returncode, 0, shadow.stderr)
        self.assertEqual(shadow.stdout, off.stdout)        # no visible change
        self.assertEqual(shadow.stderr, "")
        self.assertEqual(sorted(self.judge_calls()),
                         sorted(f"{t}.v1" for t in PURPOSES))    # asked four questions
        report = json.loads(shadow.stdout)
        self.assertIsNone(report["advisor"])
        (row,) = self.memory_rows()
        self.assertEqual((row["mode"], row["applied"], row["degraded"], row["judged"],
                          row["candidates"]), ("shadow", False, False, 4, 4))
        self.assertEqual(row["flagged"], 0)                # `on` would call it a candidate
        text = "\n".join(p.read_text() for p in (self.vault / ".context").rglob("*")
                         if p.is_file() and p.name.startswith(("jev-calls", "jev.json"))
                         or p.parent.name == "jev-cache")
        self.assertNotIn("night watch", text.lower())      # no proposal text at rest

    def test_shadow_answers_are_cached_and_a_hit_asks_nothing(self):
        self.prior()
        env = self.judge_script()
        self.configure("shadow")
        self.review_cli(self.proposal(), env=env)
        self.assertEqual(len(self.judge_calls()), 4)
        self.review_cli(self.proposal(), env=env)
        self.assertEqual(len(self.judge_calls()), 4)
        self.assertTrue(self.memory_rows()[-1]["cache_hit"])

    def test_on_adds_the_advisors_block_and_the_route(self):
        prior = self.prior()
        self.configure("on")
        judge = Judge()
        report = self.review(evaluate=judge)
        self.assertEqual(judge.templates(), sorted(f"{t}.v1" for t in PURPOSES))
        self.assertEqual(set(report), {
            "schema", "route", "reasons", "approved", "memory_written", "proposal", "mechanical",
            "priors", "suggested_supersedes", "semantic_duplicate_of", "suggested_command",
            "advisor"})
        self.assertEqual((report["schema"], report["route"], report["reasons"]),
                         ("jev-memory-review/v1", "candidate", []))
        self.assertEqual((report["approved"], report["memory_written"]), (False, False))
        self.assertEqual(report["priors"], [prior])
        self.assertEqual(report["suggested_supersedes"], [prior])
        self.assertIn(f"--supersedes {prior}", report["suggested_command"])
        self.assertIn("--kind decision --state draft", report["suggested_command"])
        self.assertNotIn(str(self.root), json.dumps(report))
        advisor = report["advisor"]
        self.assertEqual((advisor["schema"], advisor["feature"], advisor["mode"],
                          advisor["applied"], advisor["skipped"]),
                         ("jev-advice/v1", "memory", "on", True, None))
        answers = advisor["answers"]
        self.assertEqual([answers[k]["label"] for k in ("support", "commitment", "kind")],
                         ["supports", "asserted", "decision"])
        self.assertEqual(answers["relations"][0]["prior"], prior)
        self.assertEqual(answers["relations"][0]["label"], "replaces")
        self.assertTrue(all(answers[k]["confident"] for k in ("support", "commitment", "kind")))
        self.assertEqual(advisor["counts"]["judged"], 4)
        (row,) = self.memory_rows()
        self.assertEqual((row["mode"], row["applied"], row["judged"], row["flagged"]),
                         ("on", True, 4, 0))
        # The text form says the same, and that nothing was run.
        done = self.review_cli(self.proposal(), env=self.judge_script())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("route: candidate", done.stdout)
        self.assertIn("approved: false; memory_written: false", done.stdout)

    def test_on_without_a_receipt_is_shadow_and_says_so(self):
        self.prior()
        env = self.judge_script()
        off = self.review_cli(self.proposal(), "--json", env=env)
        self.configure("on", receipt=False)
        done = self.review_cli(self.proposal(), "--json", env=env)
        self.assertEqual(done.stdout, off.stdout)
        self.assertIn("calibration_required", done.stderr)
        self.assertEqual(len(self.judge_calls()), 4)      # judged and counted
        self.assertEqual((self.memory_rows()[-1]["mode"], self.memory_rows()[-1]["applied"],
                          self.memory_rows()[-1]["code"]),
                         ("shadow", False, "calibration_required"))
        self.receipt(purposes=("memory_support", "memory_commitment", "memory_kind"))
        again = self.review_cli(self.proposal(), "--json", env=env)   # relation bar not met
        self.assertEqual(again.stdout, off.stdout)
        self.receipt()
        self.assertIsNotNone(json.loads(self.review_cli(self.proposal(), "--json",
                                                        env=env).stdout)["advisor"])

    def test_the_route_follows_the_answers(self):
        prior = self.prior()
        self.configure("on", cache_ttl_s=0)

        def route(judge):
            report = self.review(evaluate=judge)
            return report["route"], report["reasons"], report

        self.assertEqual(route(Judge())[0], "candidate")
        self.assertEqual(route(Judge({"memory_commitment.v1": "tentative"}))[:2],
                         ("inspect_sources", ["commitment_not_asserted"]))
        self.assertEqual(route(Judge({"memory_support.v1": "silent"}))[:2],
                         ("inspect_sources", ["support_not_supports"]))
        self.assertEqual(route(Judge({"memory_support.v1": "contradicts"}))[1],
                         ["support_not_supports"])
        for kind in ("question", "hypothesis", "other"):
            self.assertEqual(route(Judge({"memory_kind.v1": kind}))[:2],
                             ("inspect_sources", ["kind_not_a_record"]))
        got = route(Judge({"memory_relation.v1": "contradicts"}))
        self.assertEqual(got[:2], ("inspect_sources", ["relation_contradicts"]))
        self.assertIsNone(got[2]["suggested_command"])
        self.assertEqual(got[2]["suggested_supersedes"], [])
        low = route(Judge(confidence=0.6))
        self.assertEqual(low[0], "inspect_sources")
        self.assertEqual(set(low[1]), {"support_low_confidence", "commitment_low_confidence",
                                       "kind_low_confidence", "relation_low_confidence"})
        self.assertEqual(low[2]["suggested_supersedes"], [])   # not confident: no suggestion
        dup = route(Judge({"memory_relation.v1": "duplicate"}))
        self.assertEqual((dup[0], dup[2]["semantic_duplicate_of"], dup[2]["suggested_command"]),
                         ("candidate", [prior], None))
        refines = route(Judge({"memory_relation.v1": "refines"}))
        self.assertEqual((refines[0], refines[2]["suggested_supersedes"]), ("candidate", []))
        self.assertIn("memory add <vault>", refines[2]["suggested_command"])
        self.assertNotIn("--supersedes", refines[2]["suggested_command"])
        # The kind the proposal claims does not change the route; the answer shows the kind.
        other = route(Judge({"memory_kind.v1": "note"}))
        self.assertEqual(other[0], "candidate")
        self.assertEqual(other[2]["advisor"]["answers"]["kind"]["label"], "note")

    def test_a_label_only_provider_counts_only_under_a_receipt(self):
        self.prior()
        self.configure("on", cache_ttl_s=0, provider={"kind": "fake", "label_only": True})
        report = self.review(evaluate=Judge(label_only=True))
        self.assertEqual(report["route"], "candidate")
        answers = report["advisor"]["answers"]
        self.assertIsNone(answers["support"]["confidence"])
        self.assertTrue(answers["support"]["confident"])

    def test_no_prior_means_three_questions(self):
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        report = self.review(evaluate=judge)
        self.assertEqual(report["priors"], [])
        self.assertEqual(judge.templates(), ["memory_commitment.v1", "memory_kind.v1",
                                             "memory_support.v1"])
        self.assertEqual(report["route"], "candidate")

    def test_a_batch_gets_one_report_each(self):
        self.prior()
        self.configure("on", cache_ttl_s=0)
        second = self.proposal(text="The fuel pump on the east pier passed its safety check.",
                               span="The fuel pump on the east pier was tested and passed its "
                                    "safety check.", kind="result")
        env = self.judge_script()
        done = self.review_cli(json.dumps([self.proposal(), second]), "--json", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        document = json.loads(done.stdout)
        self.assertEqual(document["schema"], "jev-memory-review-batch/v1")
        self.assertEqual(len(document["reviews"]), 2)
        lines = "\n".join(json.dumps(p) for p in (self.proposal(), second))
        one_per_line = self.review_cli(lines, "--json", env=env)
        self.assertEqual(json.loads(one_per_line.stdout)["schema"], "jev-memory-review-batch/v1")
        single = self.review_cli(self.proposal(), "--json", env=env)
        self.assertEqual(json.loads(single.stdout)["schema"], "jev-memory-review/v1")


# ---------------------------------------------------------------------------
# The kill switch, the gates, freshness
# ---------------------------------------------------------------------------

class KillSwitchGatesAndFreshness(Case):
    def test_kill_switch_variable_and_file_make_no_call(self):
        self.prior()
        env = self.judge_script()
        off = self.review_cli(self.proposal(), "--json", env=env)
        self.configure("on")
        done = self.review_cli(self.proposal(), "--json", env={**env, "CONTEXT_LAYER_JEV_DISABLE": "1"})
        self.assertEqual(done.stdout, off.stdout)
        self.assertIn("kill_switch", done.stderr)
        flag = self.vault / ".context" / "jev.disabled"
        flag.write_text("")
        done = self.review_cli(self.proposal(), "--json", env=env)
        self.assertEqual(done.stdout, off.stdout)
        flag.unlink()
        done = self.review_cli(self.proposal(), "--json", env={**env, "CONTEXT_LAYER_JEV_CHILD": "1"})
        self.assertEqual(done.stdout, off.stdout)
        self.assertEqual(self.judge_calls(), [])
        self.assertEqual([r["code"] for r in self.memory_rows()],
                         ["kill_switch", "kill_switch", "child_guard"])
        status = json.loads(self.cli("jev", "status", str(self.vault), "--json").stdout)
        self.assertEqual((status["mode"], status["kill_switch"]), ("on", False))

    def test_a_kill_switch_set_during_the_call_discards_the_answers(self):
        self.prior()
        self.configure("on", cache_ttl_s=0)
        flag = self.vault / ".context" / "jev.disabled"
        judge = Judge(during=lambda: flag.write_text(""))
        notes = []
        report = self.review(evaluate=judge, notes=notes)
        flag.unlink()
        row = self.memory_rows()[-1]
        off = jev.review_memory(self.vault, self.proposal(),
                                jev.memory_plan(self.vault, environ={"CONTEXT_LAYER_JEV_DISABLE": "1"}),
                                environ={"CONTEXT_LAYER_JEV_DISABLE": "1"})
        self.assertEqual(report, off)
        self.assertIsNone(report["advisor"])
        self.assertTrue(any("kill_switch" in n for n in notes))
        self.assertEqual((row["code"], row["degraded"], row["applied"]),
                         ("kill_switch", True, False))

    def test_a_secret_in_a_prior_record_drops_only_its_relation_question(self):
        self.prior(text="The rota starts at 10. " + SECRET_LINE)
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        report = self.review(evaluate=judge)
        self.assertEqual(judge.templates(), ["memory_commitment.v1", "memory_kind.v1",
                                             "memory_support.v1"])
        self.assertNotIn("abcdefghijkl", judge.text())
        advisor = report["advisor"]
        self.assertEqual(advisor["answers"]["support"]["label"], "supports")
        self.assertFalse(advisor["answers"]["relations"][0]["judged"])
        self.assertEqual(advisor["answers"]["relations"][0]["why"], "sensitive")
        self.assertIn("sensitive_input", advisor["codes"])
        self.assertEqual((report["route"], report["reasons"]),
                         ("inspect_sources", ["prior_not_judged"]))

    def test_a_secret_in_an_evidence_span_drops_the_questions_that_carry_it(self):
        self.write("logbook/Keys.md", "# Keys\n\n## Rotation\n\nThe " + SECRET_LINE
                   + " was replaced on the east pier.\n")
        self.prior()
        span = "The " + SECRET_LINE + " was replaced on the east pier."
        proposal = self.proposal(text="A credential on the east pier was replaced.",
                                 span=span, path="logbook/Keys.md", kind="result",
                                 prior=[self.prior(text="An old pier credential exists.",
                                                   path="logbook/March.md", kind="note")])
        self.assertEqual(proposal["evidence"][0]["span"], span)
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        report = self.review(proposal, evaluate=judge)
        self.assertEqual(judge.templates(), ["memory_relation.v1"])
        self.assertNotIn("abcdefghijkl", judge.text())
        self.assertEqual(report["route"], "inspect_sources")
        self.assertEqual(set(report["reasons"]), {"support_not_judged", "commitment_not_judged",
                                                  "kind_not_judged"})

    def test_a_secret_in_the_proposal_text_asks_nothing(self):
        self.prior()
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        notes = []
        report = self.review(self.proposal(text=NIGHT_TEXT + " " + SECRET_LINE),
                             evaluate=judge, notes=notes)
        self.assertEqual(judge.calls, 0)
        self.assertIsNone(report["advisor"])
        self.assertTrue(any("sensitive_input" in n for n in notes))
        self.assertEqual(self.memory_rows()[-1]["code"], "sensitive_input")

    def test_a_local_only_source_is_never_sent(self):
        self.write("logbook/Private Watch.md",
                   "---\nremote_allowed: false\n---\n# Watch\n\n## Rota\n\nThe night watch rota "
                   "now starts at 9 in the evening.\n")
        judge = Judge()
        self.configure("on", cache_ttl_s=0)
        report = self.review(self.proposal(text="The night watch rota starts at 9.",
                                           span="The night watch rota now starts at 9 in the "
                                                "evening.", path="logbook/Private Watch.md"),
                             evaluate=judge)
        self.assertEqual(judge.calls, 0)
        self.assertEqual((report["advisor"]["skipped"], report["route"], report["reasons"]),
                         ("local_only", "inspect_sources", ["local_only"]))
        self.assertEqual((self.memory_rows()[-1]["mode"], self.memory_rows()[-1]["code"]),
                         ("off", "local_only"))
        # A prior whose source is under local_only_prefixes is not compared, the rest is asked.
        march = self.prior(path="logbook/March.md")
        self.configure("on", cache_ttl_s=0, local_only_prefixes=["logbook/March.md"])
        judge = Judge()
        report = self.review(self.proposal(prior=[march]), evaluate=judge)
        self.assertEqual(judge.templates(), ["memory_commitment.v1", "memory_kind.v1",
                                             "memory_support.v1"])
        self.assertEqual(report["advisor"]["counts"]["local_only"], 1)
        self.assertNotIn(PRIOR_TEXT, judge.text())

    def test_a_view_that_does_not_fit_is_never_cut(self):
        self.write("logbook/Long.md", "# Long\n\n## Rota\n\nThe night watch rota now starts at 8 "
                   "in the evening instead of 10.\n\n" + ("Filler line about the quay. " * 200)
                   + "\n")
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        report = self.review(self.proposal(path="logbook/Long.md"), evaluate=judge)
        self.assertEqual(judge.calls, 0)
        self.assertEqual((report["advisor"]["skipped"], report["reasons"]),
                         ("context_incomplete", ["context_incomplete"]))

    def test_the_view_is_the_span_and_its_own_section_only(self):
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        self.review(evaluate=judge)
        views = judge.seen[0]["state"]["evidence"]
        self.assertEqual(len(views), 1)
        self.assertIn(NIGHT_SPAN, views[0])
        self.assertIn("## Night watch", views[0])
        self.assertNotIn("fuel pump", views[0].lower())     # another section of the note
        self.assertEqual(judge.seen[0]["state"]["proposal"], NIGHT_TEXT)

    def test_a_source_edited_during_the_call_discards_the_answers(self):
        self.prior()
        self.configure("on", cache_ttl_s=0)
        off = self.review(mode_plan=None, evaluate=None, environ={"CONTEXT_LAYER_JEV_DISABLE": "1"})
        judge = Judge(during=lambda: self.write("logbook/April.md", APRIL + "\nA later line.\n"))
        notes = []
        report = self.review(evaluate=judge, notes=notes)
        self.assertEqual(report, off)
        self.assertTrue(any("source_changed" in n for n in notes))
        self.assertEqual((self.memory_rows()[-1]["code"], self.memory_rows()[-1]["degraded"]),
                         ("source_changed", True))

    def test_a_prior_superseded_during_the_call_discards_the_answers(self):
        prior = self.prior()
        self.configure("on", cache_ttl_s=0)

        def supersede():
            memory.record(self.vault, kind="decision", text="The rota starts at 9.",
                          sources=[{"path": "logbook/March.md",
                                    "sha256": sha_of(self.vault / "logbook/March.md")}],
                          supersedes=prior)

        report = self.review(evaluate=Judge(during=supersede))
        self.assertIsNone(report["advisor"])
        self.assertEqual(self.memory_rows()[-1]["code"], "source_changed")

    def test_provider_failures_leave_the_mechanical_report(self):
        self.prior()
        self.configure("on", cache_ttl_s=0)
        off = self.review(environ={"CONTEXT_LAYER_JEV_DISABLE": "1"})

        def down(provider, questionnaires, **_):
            raise RuntimeError("provider down")

        def failing(provider, questionnaires, **_):
            return [{"ok": False, "answer": None, "code": "deadline_exceeded", "latency_ms": 1,
                     "usage": None, "model_reported": None, "requests": 1}
                    for _ in questionnaires]

        for name, evaluate in (("down", down), ("failing", failing),
                               ("not a list", lambda *a, **k: {"answers": 1}),
                               ("wrong length", lambda p, qs, **k: [])):
            with self.subTest(name):
                self.assertEqual(self.review(evaluate=evaluate), off)
                self.assertTrue(self.memory_rows()[-1]["degraded"])
        # One question failing keeps the others.
        first = {"n": 0}

        def partial(provider, questionnaires, **kwargs):
            out = Judge()(provider, questionnaires, **kwargs)
            out[0] = failing(provider, questionnaires[:1])[0]
            first["n"] += 1
            return out

        report = self.review(evaluate=partial)
        self.assertEqual(report["route"], "inspect_sources")
        self.assertTrue(report["advisor"]["degraded"])
        self.assertIn("deadline_exceeded", report["advisor"]["codes"])

    def test_a_slow_provider_is_abandoned_at_the_deadline(self):
        self.prior()
        self.configure("on", cache_ttl_s=0, timeout_s=0.3)
        release = threading.Event()

        def slow(provider, questionnaires, **_):
            release.wait(5)
            return []

        started = time.monotonic()
        report = self.review(evaluate=slow)
        release.set()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIsNone(report["advisor"])
        self.assertEqual(self.memory_rows()[-1]["code"], "deadline_exceeded")


# ---------------------------------------------------------------------------
# The mechanical part, priors, input, and the store
# ---------------------------------------------------------------------------

class MechanicalPriorsAndStore(Case):
    def test_a_span_that_is_not_in_the_file_fails_in_every_mode_and_asks_nothing(self):
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        bad = self.proposal()
        bad["evidence"][0]["span"] = "The night watch rota starts at 7 in the evening or so."
        report = self.review(bad, evaluate=judge)
        self.assertEqual(judge.calls, 0)
        self.assertFalse(report["mechanical"]["ok"])
        self.assertIn("span not found", report["mechanical"]["evidence"][0]["reasons"][0])
        self.assertEqual((report["route"], report["reasons"]),
                         ("inspect_sources", ["mechanical_failed"]))
        off = self.review(bad, environ={"CONTEXT_LAYER_JEV_DISABLE": "1"})
        self.assertFalse(off["mechanical"]["ok"])
        self.assertEqual(off["reasons"], ["mechanical_failed", "advisor_not_applied"])

    def test_a_stale_hash_an_excluded_path_and_a_missing_file_are_named(self):
        self.write("private/Secret.md", "# Private\n\nThe night watch rota is a secret one.\n")
        stale = self.proposal()
        stale["evidence"][0]["sha256"] = "0" * 64
        report = self.review(stale)
        item = report["mechanical"]["evidence"][0]
        self.assertTrue(item["stale"] and not item["ok"])
        self.assertIn("does not match the current file", item["reasons"][0])
        excluded = self.proposal()
        excluded["evidence"][0].update(path="private/Secret.md", sha256="1" * 64,
                                       span="The night watch rota is a secret one.",
                                       line_start=3, line_end=3)
        report = self.review(excluded)
        self.assertIn("source_path refused", report["mechanical"]["evidence"][0]["reasons"][0])
        gone = self.proposal()
        gone["evidence"][0]["path"] = "logbook/Nowhere.md"
        report = self.review(gone)
        self.assertTrue(report["mechanical"]["evidence"][0]["stale"])
        self.assertIn("source file missing", report["mechanical"]["evidence"][0]["reasons"][0])

    def test_an_exact_duplicate_of_a_record_in_force_is_named_without_a_model(self):
        source = self.vault / "logbook/April.md"
        existing = memory.record(self.vault, kind="decision", text=NIGHT_TEXT,
                                 sources=[{"path": "logbook/April.md",
                                           "sha256": sha_of(source)}])["id"]
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        report = self.review(evaluate=judge)
        self.assertEqual(judge.calls, 0)
        self.assertEqual(report["mechanical"]["exact_duplicate_of"], existing)
        self.assertEqual((report["route"], report["reasons"]),
                         ("inspect_sources", ["exact_duplicate"]))
        # Once a later record replaces it, it is no longer a duplicate in force.
        memory.record(self.vault, kind="decision", text="The rota starts at 9.",
                      sources=[{"path": "logbook/April.md", "sha256": sha_of(source)}],
                      supersedes=existing)
        again = self.review(evaluate=Judge())
        self.assertIsNone(again["mechanical"]["exact_duplicate_of"])

    def test_default_priors_are_the_newest_four_records_in_force_sharing_a_path(self):
        ids = [self.prior(text=f"The rota, version {n}.", path="logbook/April.md",
                          kind="note") for n in range(6)]
        self.prior(text="An unrelated fact about the March log.", path="logbook/March.md")
        memory.record(self.vault, kind="note", text="The rota, version 6.",
                      sources=[{"path": "logbook/April.md",
                                "sha256": sha_of(self.vault / "logbook/April.md")}],
                      supersedes=ids[5])
        report = self.review()
        self.assertEqual(len(report["priors"]), 4)
        self.assertNotIn(ids[5], report["priors"])              # superseded: not in force
        self.assertEqual(report["priors"][1:], [ids[4], ids[3], ids[2]])   # newest first
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        self.review(evaluate=judge)
        self.assertEqual(judge.templates().count("memory_relation.v1"), 4)

    def test_explicit_priors_must_be_in_force_and_an_empty_list_asks_no_relation(self):
        first = self.prior()
        memory.record(self.vault, kind="decision", text="The rota starts at 9.",
                      sources=[{"path": "logbook/March.md",
                                "sha256": sha_of(self.vault / "logbook/March.md")}],
                      supersedes=first)
        with self.assertRaises(jev.Refused):
            self.review(self.proposal(prior=[first]))
        with self.assertRaises(jev.Refused):
            self.review(self.proposal(prior=["m-0000000000000000"]))
        self.configure("on", cache_ttl_s=0)
        judge = Judge()
        report = self.review(self.proposal(prior=[]), evaluate=judge)
        self.assertEqual((report["priors"], report["route"]), ([], "candidate"))
        self.assertNotIn("memory_relation.v1", judge.templates())

    def test_the_memory_store_is_byte_identical_after_a_review_in_every_mode(self):
        self.prior()
        env = self.judge_script()
        before = self.store()
        self.assertIn("records.jsonl", before)
        for mode in ("off", "shadow", "on"):
            with self.subTest(mode=mode):
                self.configure(mode, cache_ttl_s=0)
                done = self.review_cli(self.proposal(), "--json", env=env)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.store(), before)
        # No store at all: a review does not create one.
        fresh = self.root / "fresh"
        (fresh / ".context").mkdir(parents=True)
        (fresh / ".context" / "routes.json").write_text(json.dumps(ROUTES))
        (fresh / "a.md").write_text("# A\n\n## S\n\nThe kiosk opens at nine on weekdays.\n")
        span = "The kiosk opens at nine on weekdays."
        proposal = {"kind": "note", "text": span, "evidence": [{
            "path": "a.md", "sha256": sha_of(fresh / "a.md"), "line_start": 5, "line_end": 5,
            "span": span}]}
        plan = jev.memory_plan(fresh, environ={})
        report = jev.review_memory(fresh, proposal, plan, environ={})
        self.assertTrue(report["mechanical"]["ok"])
        self.assertFalse((fresh / ".context" / "memory").exists())

    def test_an_unreadable_store_is_reported_not_fatal(self):
        self.prior()
        (self.vault / ".context" / "memory" / "records.jsonl").write_bytes(b"{not json\n")
        report = self.review()
        self.assertEqual(report["memory_store"], "store_unreadable")
        self.assertEqual(report["priors"], [])

    def test_proposals_are_checked_strictly(self):
        good = self.proposal()
        for name, change in {
            "unknown key": {"extra": 1},
            "other schema": {"schema": "jev-memory-proposal/v9"},
            "bad kind": {"kind": "hypothesis"},
            "empty text": {"text": "  "},
            "long text": {"text": "x" * 4001},
            "no evidence": {"evidence": []},
            "nine spans": {"evidence": good["evidence"] * 9},
            "bad prior": {"prior": ["x-1"]},
            "five priors": {"prior": [f"m-{n:016x}" for n in range(5)]},
            "repeated prior": {"prior": ["m-0000000000000000"] * 2},
        }.items():
            with self.subTest(name):
                with self.assertRaises(jev.Refused):
                    jev.parse_memory_proposal({**good, **change})
        for key, value in (("sha256", "abc"), ("line_start", 0), ("line_end", 1.5),
                           ("span", ""), ("path", "")):
            with self.subTest(key):
                broken = copy.deepcopy(good)
                broken["evidence"][0][key] = value
                with self.assertRaises(jev.Refused):
                    jev.parse_memory_proposal(broken)
        broken = copy.deepcopy(good)
        broken["evidence"][0]["note"] = "extra"
        with self.assertRaises(jev.Refused):
            jev.parse_memory_proposal(broken)
        broken = copy.deepcopy(good)
        broken["evidence"][0]["line_start"], broken["evidence"][0]["line_end"] = 3, 2
        with self.assertRaises(jev.Refused):
            jev.parse_memory_proposal(broken)

    def test_command_line_exit_codes(self):
        proposal = self.proposal_file(self.proposal())
        ok = self.cli("jev", "review-memory", str(self.vault), "--proposal", str(proposal))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        for name, text in {"not json": "{oops", "empty array": "[]",
                           "bad proposal": json.dumps({"kind": "decision"}),
                           "duplicate key": '{"kind": "note", "kind": "note"}'}.items():
            with self.subTest(name):
                bad = self.cli("jev", "review-memory", str(self.vault), "--proposal",
                               str(self.proposal_file(text, "bad.json")))
                self.assertEqual(bad.returncode, 1)
                self.assertIn("refused", bad.stderr)
                self.assertEqual(bad.stdout, "")
        missing = self.cli("jev", "review-memory", str(self.vault), "--proposal",
                           str(self.root / "nope.json"))
        self.assertEqual(missing.returncode, 1)
        self.assertNotIn(str(self.root), missing.stderr)
        self.assertEqual(self.cli("jev", "review-memory", str(self.vault)).returncode, 2)
        self.assertEqual(self.cli("jev", "review-memory", str(self.root / "no-vault"),
                                  "--proposal", str(proposal)).returncode, 2)
        self.assertEqual(self.cli("jev", "review-memory", str(self.vault), "--proposal",
                                  str(proposal), "--bogus").returncode, 2)
        bad_prior = self.proposal_file(self.proposal(prior=["m-0000000000000000"]), "p2.json")
        refused = self.cli("jev", "review-memory", str(self.vault), "--proposal", str(bad_prior))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("not a record in force", refused.stderr)


# ---------------------------------------------------------------------------
# Status check: jev_client.probe and `jev status --check`
# ---------------------------------------------------------------------------

_REAL_CONNECT = socket.socket.connect


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class FakeServer:
    """A loopback HTTP server: `routes` maps a path to (status, headers) or a callable."""

    def __init__(self, routes, default=(404, {})):
        self.lock = threading.Lock()
        self.requests = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with fake.lock:
                    fake.requests.append({"method": "GET", "path": self.path,
                                          "headers": {k.lower(): v for k, v in
                                                      self.headers.items()}})
                behaviour = routes.get(self.path, default)
                try:
                    if callable(behaviour):
                        behaviour(self)
                        return
                    status, headers = behaviour
                    body = b"PRIVATE-BODY-MARKER"
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self.httpd = _QuietServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    @property
    def paths(self):
        with self.lock:
            return [r["path"] for r in self.requests]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def sleepy(seconds):
    def behaviour(handler):
        time.sleep(seconds)
    return behaviour


def dribbling_status(handler):
    """The status line, one byte at a time: a per-read socket timeout never fires."""
    for byte in b"HTTP/1.0 200 OK\r\n\r\n":
        handler.wfile.write(bytes([byte]))
        handler.wfile.flush()
        time.sleep(0.25)


def openai(url):
    return {"kind": "openai_compat", "base_url": url, "model": "local-model"}


def fake_claude(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        program = directory / "claude.py"
        program.write_text(f"{body}\n", encoding="utf-8", newline="\n")
        command = directory / "claude.cmd"
        command.write_text(f'@"{sys.executable}" "{program}" %*\r\n',
                           encoding="utf-8", newline="")
        return command
    script = directory / "claude"
    script.write_text(f"#!{sys.executable}\n{body}\n")
    script.chmod(0o755)
    return script


class Probe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.servers = []
        self.addCleanup(lambda: [s.close() for s in self.servers])

    def server(self, routes, **kwargs):
        fake = FakeServer(routes, **kwargs)
        self.servers.append(fake)
        return fake

    def test_a_loopback_server_answering_health_is_ok_in_one_request(self):
        fake = self.server({"/health": (200, {})})
        result = jev_client.probe(openai(fake.url))
        self.assertEqual((result["kind"], result["checked"], result["ok"], result["code"],
                          result["requests"]), ("openai_compat", True, True, "ok", 1))
        self.assertEqual(fake.paths, ["/health"])
        self.assertNotIn("PRIVATE-BODY-MARKER", json.dumps(result))
        self.assertNotIn(fake.url, json.dumps(result))
        self.assertIsInstance(result["latency_ms"], int)
        request = fake.requests[0]
        self.assertNotIn("authorization", request["headers"])
        self.assertTrue(request["headers"]["user-agent"].startswith("context-layer/"))

    def test_models_is_asked_next_and_at_most_two_requests_are_made(self):
        fake = self.server({"/v1/models": (200, {})})
        result = jev_client.probe(openai(fake.url))
        self.assertEqual((result["ok"], result["code"], result["requests"]), (True, "ok", 2))
        self.assertEqual(fake.paths, ["/health", "/v1/models"])
        self.assertIn("/v1/models", result["detail"])
        nothing = self.server({})
        result = jev_client.probe(openai(nothing.url))
        self.assertEqual((result["ok"], result["code"], result["requests"]),
                         (False, "http_not_found", 2))
        self.assertEqual(nothing.paths, ["/health", "/v1/models"])
        broken = self.server({"/health": (500, {}), "/v1/models": (503, {})})
        self.assertEqual(jev_client.probe(openai(broken.url))["code"], "http_server_error")
        base = self.server({"/v1/models": (200, {})})
        result = jev_client.probe(openai(base.url + "/v1"))     # a base that ends in /v1
        self.assertEqual(base.paths, ["/health", "/v1/models"])
        self.assertTrue(result["ok"])

    def test_auth_required_counts_as_reachable_and_no_key_is_sent(self):
        fake = self.server({"/health": (401, {})})
        provider = {"kind": "systemone", "base_url": fake.url, "model": "jev-1",
                    "key_env": "TYPESAFE_API_KEY"}
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "fictional-key-9021"}):
            result = jev_client.probe(provider)
        self.assertEqual((result["ok"], result["code"]), (True, "reachable_auth_required"))
        self.assertNotIn("authorization", fake.requests[0]["headers"])
        self.assertNotIn("fictional-key-9021", json.dumps(result))

    def test_redirects_are_refused_and_not_followed(self):
        target = self.server({"/health": (200, {})})
        fake = self.server({"/health": (302, {"Location": target.url + "/health"}),
                            "/v1/models": (302, {"Location": target.url + "/health"})})
        result = jev_client.probe(openai(fake.url))
        self.assertEqual((result["ok"], result["code"]), (False, "redirect_refused"))
        self.assertEqual(target.paths, [])
        self.assertEqual(fake.paths, ["/health"])              # a redirect ends the check

    def test_nothing_listening_is_unreachable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        result = jev_client.probe(openai(f"http://127.0.0.1:{port}"))
        self.assertEqual((result["checked"], result["ok"], result["code"], result["requests"]),
                         (True, False, "provider_unreachable", 1))

    def test_a_non_loopback_provider_is_refused_before_any_io(self):
        def refuse(*args, **kwargs):
            raise NetworkAttempted("a probe of a remote provider tried to connect")

        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse), \
                mock.patch.object(socket, "getaddrinfo", refuse):
            for url in ("https://judge.example", "https://judge.example/v1", "https://203.0.113.7",
                        "https://[2001:db8::1]"):
                with self.subTest(url):
                    result = jev_client.probe({"kind": "systemone", "base_url": url,
                                               "model": "jev-1", "key_env": "TYPESAFE_API_KEY"})
                    self.assertEqual((result["checked"], result["ok"], result["code"],
                                      result["requests"]), (False, None, "not_loopback", 0))
                    self.assertNotIn("judge.example", json.dumps(result))
            for url in ("http://203.0.113.7:8080", "http://localhost:8080", "http://[2001:db8::1]",
                        "http://192.168.1.10:11434", "ftp://127.0.0.1", "http://user@127.0.0.1:1"):
                with self.subTest(url):
                    for kind in ("openai_compat", "systemone"):
                        provider = {"kind": kind, "base_url": url, "model": "m"}
                        result = jev_client.probe(provider)
                        self.assertFalse(result["checked"])
                        self.assertIn(result["code"], ("provider_invalid", "endpoint_invalid", "not_loopback"))
                        self.assertEqual(result["requests"], 0)

    def test_the_probe_is_bounded_in_time(self):
        fake = self.server({"/health": sleepy(3.0), "/v1/models": sleepy(3.0)})
        started = time.monotonic()
        result = jev_client.probe(openai(fake.url), timeout_s=0.5)
        elapsed = time.monotonic() - started
        self.assertEqual((result["ok"], result["code"]), (False, "deadline_exceeded"))
        self.assertLess(elapsed, 1.6)
        dribble = self.server({"/health": dribbling_status, "/v1/models": dribbling_status})
        started = time.monotonic()
        result = jev_client.probe(openai(dribble.url), timeout_s=0.6)
        self.assertEqual((result["ok"], result["code"]), (False, "deadline_exceeded"))
        self.assertLess(time.monotonic() - started, 1.8)
        for bad in (0, -1, 10.5, float("nan"), True, "1", None):
            with self.subTest(timeout=bad):
                with self.assertRaises(ValueError):
                    jev_client.probe(openai(fake.url), timeout_s=bad)
        with self.assertRaises(TypeError):
            jev_client.probe("http://127.0.0.1:1")

    def test_recorded_and_fake_send_and_start_nothing(self):
        def refuse(*args, **kwargs):
            raise NetworkAttempted("no probe for this kind may connect or start a process")

        recording = self.root / "rec.jsonl"
        recording.write_text("")
        providers = [{"kind": "fake"},
                     {"kind": "recorded", "recording": str(recording),
                      "replays": {"kind": "systemone", "base_url": "http://127.0.0.1:9",
                                  "model": "jev-1", "key_env": "TYPESAFE_API_KEY"}}]
        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse), \
                mock.patch.object(subprocess, "Popen", refuse):
            for provider in providers:
                result = jev_client.probe(provider)
                self.assertEqual((result["checked"], result["ok"], result["code"],
                                  result["requests"]), (False, None, "not_applicable", 0))

    def test_cmd_is_looked_up_and_never_started(self):
        marker = self.root / "started"
        script = self.root / "judge-cmd"
        script.write_text(f"#!{sys.executable}\nopen({str(marker)!r}, 'w').close()\n")
        if os.name != "nt": script.chmod(0o755)
        found = jev_client.probe({"kind": "cmd", "argv": [str(script), "{questionnaire_file}"]})
        self.assertEqual((found["checked"], found["ok"], found["code"]), (False, True, "ok"))
        missing = jev_client.probe({"kind": "cmd",
                                    "argv": [str(self.root / "absent"), "{questionnaire_file}"]})
        self.assertEqual((missing["ok"], missing["code"]), (False, "program_missing"))
        self.assertFalse(marker.exists())

    def test_host_cli_asks_claude_for_its_version_only(self):
        bindir = self.root / "bin"
        log = self.root / "argv.log"
        fake_claude(bindir, "import os, sys\n"
                            f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '|' "
                            "+ os.environ.get('CONTEXT_LAYER_JEV_CHILD', '') + '|' "
                            "+ os.getcwd() + '\\n')\n"
                            "print('2.1.9 (Claude Code)')\n")
        provider = {"kind": "host_cli", "model": "haiku"}
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}):
            result = jev_client.probe(provider)
        self.assertEqual((result["checked"], result["ok"], result["code"], result["requests"]),
                         (True, True, "ok", 1))
        self.assertEqual(result["version"], "2.1.9 (Claude Code)")
        argv, child, cwd = log.read_text().strip().split("|")
        self.assertEqual((argv, child), ("--version", "1"))
        self.assertNotEqual(Path(cwd).resolve(), REPO)
        self.assertFalse(Path(cwd).exists())                   # the private directory is gone

    def test_host_cli_failures_have_fixed_codes(self):
        bindir = self.root / "bin"
        provider = {"kind": "host_cli", "model": "haiku"}

        def run(body):
            fake_claude(bindir, body)
            with mock.patch.dict(os.environ, {"PATH": str(bindir)}):
                return jev_client.probe(provider)

        self.assertEqual(run("import sys\nsys.exit(3)")["code"], "program_failed")
        self.assertEqual(run("print('not a version')")["code"], "output_invalid")
        self.assertEqual(run("print('')")["code"], "output_invalid")
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            result = jev_client.probe(provider)
        self.assertEqual((result["ok"], result["code"], result["requests"]),
                         (False, "program_missing", 0))
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"PATH": str(bindir)}):
            fake_claude(bindir, "import time\ntime.sleep(20)")
            result = jev_client.probe(provider, timeout_s=0.5)
        self.assertEqual(result["code"], "deadline_exceeded")
        self.assertLess(time.monotonic() - started, 2.5)

    def test_an_unusable_provider_block_is_a_result_not_a_raise(self):
        for provider in ({}, {"kind": "nonsense"}, {"kind": "openai_compat"},
                         {"kind": "openai_compat", "base_url": "http://127.0.0.1:9",
                          "model": "m", "surprise": 1}):
            with self.subTest(provider):
                result = jev_client.probe(provider)
                self.assertEqual((result["checked"], result["code"]), (False, "provider_invalid"))

    def test_the_network_surface_guard_passes(self):
        done = subprocess.run([sys.executable, str(REPO / "scripts" / "check_network_surface.py")],
                              cwd=REPO, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("network surface: ok", done.stdout)


class StatusCheck(Case):
    def setUp(self):
        super().setUp()
        self.servers = []
        self.addCleanup(lambda: [s.close() for s in self.servers])

    def server(self, routes, **kwargs):
        fake = FakeServer(routes, **kwargs)
        self.servers.append(fake)
        return fake

    def configure_loopback(self, url, mode="shadow"):
        done = self.cli("jev", mode, str(self.vault), "--provider-kind", "openai_compat",
                        "--base-url", url, "--model", "local-model")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_plain_status_probes_nothing_and_check_prints_the_result(self):
        fake = self.server({"/health": (200, {})})
        self.configure_loopback(fake.url)
        for argv in (["jev", "status", str(self.vault)],
                     ["jev", "status", str(self.vault), "--json"]):
            done = self.cli(*argv)
            self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(fake.paths, [])
        done = self.cli("jev", "status", str(self.vault), "--check")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("check: endpoint accepted; probe ok (ok: GET /health: 200; 1 request(s),",
                      done.stdout)
        self.assertEqual(fake.paths, ["/health"])
        info = json.loads(self.cli("jev", "status", str(self.vault), "--json",
                                   "--check").stdout)
        probe = info["check"]["probe"]
        self.assertEqual((probe["checked"], probe["ok"], probe["code"]), (True, True, "ok"))
        self.assertNotIn(fake.url, json.dumps(probe))
        self.assertEqual(fake.paths, ["/health", "/health"])

    def test_check_reports_a_provider_that_is_not_there(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.configure_loopback(f"http://127.0.0.1:{port}")
        done = self.cli("jev", "status", str(self.vault), "--check")
        self.assertEqual(done.returncode, 0, done.stderr)        # a check never fails status
        self.assertIn("probe failed (provider_unreachable:", done.stdout)

    def test_check_sends_nothing_when_the_advisor_is_off_or_killed(self):
        fake = self.server({"/health": (200, {})})
        self.configure_loopback(fake.url)
        off = self.cli("jev", "off", str(self.vault))
        self.assertEqual(off.returncode, 0, off.stderr)
        done = self.cli("jev", "status", str(self.vault), "--check")
        self.assertIn("probe not sent (mode_off:", done.stdout)
        self.configure_loopback(fake.url, "shadow")
        done = self.cli("jev", "status", str(self.vault), "--check",
                        env={"CONTEXT_LAYER_JEV_DISABLE": "1"})
        self.assertIn("probe not sent (mode_off:", done.stdout)
        self.assertEqual(fake.paths, [])

    def test_check_never_leaves_loopback_and_never_prints_a_key(self):
        done = self.cli("jev", "shadow", str(self.vault), "--provider-kind", "systemone",
                        "--base-url", "https://judge.example", "--model", "jev-1.13.0",
                        "--key-env", "TYPESAFE_API_KEY")
        self.assertEqual(done.returncode, 0, done.stderr)
        code = ("import runpy, socket, sys\n"
                "def refuse(*a, **k):\n    raise SystemExit('a status check tried to connect')\n"
                "socket.socket.connect = refuse\nsocket.create_connection = refuse\n"
                "socket.getaddrinfo = refuse\n"
                "sys.argv = ['context-layer', 'jev', 'status', sys.argv[1], '--json', '--check']\n"
                "runpy.run_module('context_layer.cli', run_name='__main__')\n")
        done = subprocess.run([sys.executable, "-c", code, str(self.vault)], cwd=REPO,
                              capture_output=True, text=True,
                              env={**isolated_home_env(CLEAN_ENV, self.home),
                                   "TYPESAFE_API_KEY": "fictional-key-5530"})
        self.assertEqual(done.returncode, 0, done.stderr)
        probe = json.loads(done.stdout)["check"]["probe"]
        self.assertEqual((probe["checked"], probe["code"]), (False, "not_loopback"))
        self.assertNotIn("fictional-key-5530", done.stdout + done.stderr)

    def test_recorded_and_fake_providers_say_nothing_is_probed(self):
        self.assertEqual(self.cli("jev", "shadow", str(self.vault), "--provider-kind",
                                  "fake").returncode, 0)
        done = self.cli("jev", "status", str(self.vault), "--check")
        self.assertIn("probe not sent (not_applicable:", done.stdout)

    def test_an_unconfigured_vault_is_not_probed_and_loads_no_client(self):
        code = ("import json, sys\nfrom pathlib import Path\nfrom context_layer import jev\n"
                "info = jev.status(Path(sys.argv[1]), check=True)\n"
                "print(json.dumps({'configured': info['configured'], 'check': 'check' in info,\n"
                "                  'loaded': sorted(m for m in sys.modules if "
                "m.startswith('context_layer.jev_'))}))\n")
        done = subprocess.run([sys.executable, "-c", code, str(self.vault)], cwd=REPO,
                              capture_output=True, text=True,
                              env=isolated_home_env(CLEAN_ENV, self.home))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout),
                         {"configured": False, "check": False, "loaded": []})


if __name__ == "__main__":
    unittest.main()
