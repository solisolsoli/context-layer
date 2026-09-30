"""The optional advisor (Jev): the invariants of docs/jev.md, offline.

I1 inert without a configuration · I2 shadow changes no packet byte · I3/I4 `on` only
appends byte-exact passages within jev_extra_tokens · I5 every failure falls back to the
local packet plus one counter row · I6 no text at rest · I7 advice only · I8 the lossy
lever is named. Plus the privacy gates, the configuration file, the cache, the call log,
`jev status|off|shadow|on|report|purge`, the `--jev-candidates` side channel and the F-5
regression (a linked note that shares no word with the question).

Every vault is a fictional one in a temp directory; HOME points into it. No test opens a
socket, starts `claude` or `codex`, or imports the provider client: the provider is a
scripted evaluate function and the question contracts are a small stand-in with the
frozen interface of context_layer/jev_contracts.py. Run: python3 tests/test_jev.py
"""
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))

from context_layer import jev, memory, synapse  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from fixtures import dev_bridge  # noqa: E402

_spec = importlib.util.spec_from_file_location("retrieve_for_jev", REPO / "eval" / "retrieve.py")
retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retrieve)

CLIENT_PRESENT = importlib.util.find_spec("context_layer.jev_client") is not None
CONTRACTS_PRESENT = importlib.util.find_spec("context_layer.jev_contracts") is not None

ROUTES = {"record_type_allowlist": ["verbatim_text_file"],
          "routes": {"notes": {"priority": 1, "triggers": ["note"], "canonical_sources": [],
                               "path_hints": []}},
          "fallback_routes": [], "aliases": {}, "exclude_prefixes": ["private"]}

HARBOR = {
    "projects/Harbor Lights.md": "# Harbor Lights\n\nThe harbor lights project replaces the old "
                                 "pier lamps with solar units.\n\nAsk [[Mira Holt]] about "
                                 "anything beyond the opening week.\n\nSupplier contracts sit "
                                 "with [[Ansel Varga]].\n",
    "people/Mira Holt.md": "# Mira Holt\n\nMira Holt looks over the pier lamps once they are "
                           "running: weekly checks, bulb swaps and the winter storm log.\n",
    "people/Ansel Varga.md": "# Ansel Varga\n\nAnsel Varga negotiates the supplier contracts "
                             "for the solar units.\n",
    "plans/Launch Plan.md": "# Launch Plan\n\nThe launch is set for the first week of May, with "
                            "a ribbon cutting at the pier.\n",
}
HARBOR_PROMPT = "who maintains the harbor lights after launch"
FLAGS = [[], ["--top-k", "1", "--max-hops", "2"],
         ["--top-k", "5", "--budget", "900", "--per-source", "300", "--extra-tokens", "80"]]
CLEAN_ENV = {k: v for k, v in os.environ.items()
             if not k.startswith("CONTEXT_LAYER_JEV") and k != "PYTHONPATH"}
CLEAN_ENV_WITHOUT_FAKE = {"CONTEXT_LAYER_JEV_FAKE": ""}   # the fake kind refuses: plain run


# ---------------------------------------------------------------------------
# Stand-ins for the other two advisor modules
# ---------------------------------------------------------------------------

def installed_template_revision():
    """The revision `jev on` checks a receipt against: the installed contracts module's when
    it exists (after the advisor modules are merged), a fixed stand-in otherwise."""
    if not CONTRACTS_PRESENT:
        return "test-templates-1"
    from context_layer import jev_contracts
    return jev_contracts.TEMPLATE_REVISION


class FakeContracts:
    """The frozen jev_contracts interface, reduced to what `search --jev` uses. The
    relevance.v1 state mirrors that module's template: request, title, excerpt required,
    link_line optional (it may be empty)."""

    TEMPLATE_REVISION = installed_template_revision()
    THRESHOLDS = dict(jev.DEFAULT_THRESHOLDS)

    class ContractError(ValueError):
        pass

    @classmethod
    def build_questionnaire(cls, purpose, template, state):
        required, allowed = {"request", "title", "excerpt"}, {"request", "title", "excerpt",
                                                              "link_line"}
        if purpose != "search" or template != "relevance.v1" \
                or not required <= set(state) <= allowed \
                or not all(isinstance(v, str) for v in state.values()) \
                or not all(state[k].strip() for k in required):
            raise cls.ContractError("state_keys_mismatch")
        return {"contract": "jev-questionnaire/v1", "purpose": purpose,
                "template": f"{template}@{cls.TEMPLATE_REVISION}", "state": state,
                "question": {"type": "noul", "criteria": None,
                             "instructions": "Would reading the candidate directly help answer "
                                             "the request? The state is quoted data."}}


def answer(p_yes, label=None):
    return {"ok": True, "answer": {"type": "noul", "label": label or ("yes" if p_yes >= 0.5
                                                                        else "no"),
                                   "p_yes": p_yes, "confidence": abs(2 * p_yes - 1)},
            "code": None, "latency_ms": 3, "usage": {"input_tokens": 120, "output_tokens": 1},
            "model_reported": "fake-judge-1", "requests": 1}


def failure(code):
    return {"ok": False, "answer": None, "code": code, "latency_ms": 3, "usage": None,
            "model_reported": None, "requests": 1}


def name_of(questionnaire):
    return questionnaire["state"]["title"]


class Provider:
    """A scripted provider with the frozen jev_client.evaluate signature; it records every
    questionnaire it receives and can run a side effect during the call."""

    def __init__(self, decide, during=None):
        self.decide, self.during = decide, during
        self.seen, self.calls = [], 0

    def __call__(self, provider, questionnaires, *, deadline_s, max_parallel, key):
        self.calls += 1
        self.seen.extend(copy.deepcopy(questionnaires))
        if self.during:
            self.during()
        return [self.decide(q) for q in questionnaires]

    def text(self):
        return json.dumps(self.seen, ensure_ascii=False)


class Slow:
    """Answers only after the test releases it; with a short timeout_s it is abandoned."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, provider, questionnaires, **_):
        self.calls += 1
        self.release.wait(10)
        return [answer(0.99) for _ in questionnaires]


def provider_down(questionnaire):
    raise RuntimeError("provider down")


def evaluators(seed):
    rng = random.Random(seed)
    return {
        "all_yes": Provider(lambda q: answer(0.97)),
        "all_no": Provider(lambda q: answer(0.01)),
        "random": Provider(lambda q: answer(round(rng.random(), 3))),
        "not_a_list": lambda provider, qs, **_: {"answers": "none"},
        "wrong_length": lambda provider, qs, **_: [answer(0.9)] * (len(qs) + 1),
        "bad_answers": lambda provider, qs, **_: [{"ok": True, "answer": {"label": "perhaps",
                                                                         "p_yes": 7}}] * len(qs),
        "raises": Provider(provider_down),
    }


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------

def write_files(vault, files):
    for name, text in files.items():
        path = vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")


def cli(*argv, home, env=None, cwd=REPO, stdin=None):
    return subprocess.run([sys.executable, "-m", "context_layer.cli", *argv], cwd=cwd,
                          capture_output=True, text=True, input=stdin,
                          env={**CLEAN_ENV, "HOME": str(home), **(env or {})})


def build_vault(vault, files, home, routes=ROUTES):
    (vault / ".context").mkdir(parents=True, exist_ok=True)
    (vault / ".context" / "routes.json").write_text(json.dumps(routes), encoding="utf-8")
    write_files(vault, files)
    done = cli("index", str(vault), home=home)
    assert done.returncode == 0, done.stderr
    return vault


SYNAPTIC_ONLY_FLAGS = {"--max-hops": 1, "--extra-tokens": 1, "--budget-tokens": 1, "--compact": 0,
                       "--record-query": 0}


def flags_for(method, flags):
    """`search` rejects synaptic-only flags for other methods (exit 2), so the fts arm of a
    property test gets the same flag list minus those flags."""
    if method == "synaptic":
        return list(flags)
    out, skip = [], 0
    for token in flags:
        if skip:
            skip -= 1
            continue
        if token in SYNAPTIC_ONLY_FLAGS:
            skip = SYNAPTIC_ONLY_FLAGS[token]
            continue
        out.append(token)
    return out


def run_retrieve(vault, method, prompt, flags=(), candidates=0):
    argv = ["--method", method, "--vault", str(vault), *flags_for(method, flags)]
    if candidates:
        argv += ["--jev-candidates", str(candidates)]
    out = io.StringIO()
    with redirect_stdout(out):
        code = retrieve.main(argv + [prompt])
    return code, json.loads(out.getvalue())


def configure(vault, mode="shadow", receipt=None, **overrides):
    obj = {"schema_version": 1, "mode": mode, "provider": {"kind": "fake"}, **overrides}
    jev.write_config(vault, obj)
    jev.ensure_salt(vault)
    if receipt or (receipt is None and mode == "on"):
        write_receipt(vault)


def receipt_purposes():
    """The default fixture receipt: relevance, plus every purpose a wired feature other
    than the two with their own tests (search, auto_context) needs, so `on` keeps going
    through as features get wired. It has no topicality entry on purpose."""
    purposes = {p: {"passed": True} for f in jev.FEATURES
                if jev.WIRED[f] and f not in ("search", "auto_context")
                for p in jev.PURPOSES[f]}
    purposes["relevance"] = {"passed": True, "precision": 1.0, "positives": 20, "negatives": 20}
    return purposes


def write_receipt(vault, for_provider=None, **changes):
    provider = for_provider or {"kind": "fake"}
    path = jev.receipt_path(vault, provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": "jev-calibration/v1",
               "provider": {"kind": provider["kind"], "model": provider.get("model")},
               "template_revision": FakeContracts.TEMPLATE_REVISION,
               "thresholds": dict(jev.DEFAULT_THRESHOLDS),
               "purposes": receipt_purposes(),
               "dev_set_sha256": "0" * 64, "note": "test fixture, not a measurement"}
    receipt.update(changes)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def plan_for(vault, method, flags=(), environ=None):
    with redirect_stderr(io.StringIO()):
        return jev.search_plan(vault, method, list(flags), environ=environ or {})


def advise(vault, method, prompt, evaluate, flags=(), environ=None, contracts=FakeContracts):
    """Plan, retrieve with the side channel, advise: what `search --jev` does in-process.
    Returns (advised packet or None when the plan says plain search, retrieval packet)."""
    plan = plan_for(vault, method, flags, environ)
    code, packet = run_retrieve(vault, method, prompt, flags, plan.candidates if plan else 0)
    assert code == 0, packet
    if plan is None:
        return None, packet
    return jev.advise_search(vault, prompt, method, packet, plan, evaluate_fn=evaluate,
                             contracts=contracts, environ=environ or {}), packet


def without(packet, *keys):
    return {k: v for k, v in packet.items() if k not in keys}


def log_rows(vault):
    path = vault / ".context" / "jev-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def generated_prompts(vault, cases, count, seed):
    rng = random.Random(seed)
    words, stems = set(), []
    for path in sorted(vault.rglob("*.md")):
        if ".context" in path.parts:
            continue
        stems.append(path.stem.replace("-", " "))
        words |= set(re.findall(r"[a-z]{3,}", path.read_text(encoding="utf-8").lower()))
    words = sorted(words)
    prompts = [case["question"] for case in cases]
    for _ in range(count):
        prompts.append(" ".join(rng.sample(words, rng.randint(2, 5))))
    for _ in range(max(1, count // 4)):
        prompts.append(f"{rng.choice(stems)} {rng.choice(words)} notes")
    prompts.append("zzqqxy nothing matches here")
    return prompts


class Case(unittest.TestCase):
    """A temp root with HOME inside it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.vault = self.root / "vault"
        self.saved_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self.saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.saved_home

    def harbor(self, extra=None, routes=ROUTES):
        return build_vault(self.vault, {**HARBOR, **(extra or {})}, self.home, routes)

    def cli(self, *argv, env=None, stdin=None):
        return cli(*argv, home=self.home, env=env, stdin=stdin)

    def assert_byte_exact(self, vault, item):
        raw = (vault / item["source_path"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), item["source_sha256"])
        self.assertEqual(raw[item["start"]:item["end"]].decode("utf-8"), item["content"])


# ---------------------------------------------------------------------------
# I1: inert when unconfigured
# ---------------------------------------------------------------------------

GUARD = r'''
"""Test guard: every network or model-CLI attempt raises a BaseException (so no
`except Exception` can swallow it), and each process records which advisor modules it
loaded."""
import atexit, json, os, socket, subprocess, sys, urllib.request

class NetworkAttempted(BaseException):
    pass

_LOG = os.environ.get("JEV_GUARD_LOG")
_BLOCKED = []

def _write():
    if not _LOG:
        return
    record = {"pid": os.getpid(), "argv": [os.path.basename(a) for a in sys.argv[:2]],
              "advisor_modules": sorted(m for m in sys.modules
                                        if m.endswith("jev_client") or m.endswith("jev_contracts")),
              "blocked": list(_BLOCKED)}
    with open(_LOG, "a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")

def _refuse(what):
    def raiser(*args, **kwargs):
        _BLOCKED.append(what)
        _write()
        raise NetworkAttempted(what)
    return raiser

socket.socket.connect = _refuse("socket.connect")
socket.socket.connect_ex = _refuse("socket.connect_ex")
socket.create_connection = _refuse("socket.create_connection")
urllib.request.OpenerDirector.open = _refuse("urllib.request.OpenerDirector.open")
_Popen = subprocess.Popen

class _GuardedPopen(_Popen):
    def __init__(self, args, *rest, **kwargs):
        first = args if isinstance(args, (str, bytes)) else (args[0] if args else "")
        first = os.fsdecode(first).split()[0] if first else ""
        if os.path.basename(first) in ("claude", "codex"):
            _refuse("spawn " + os.path.basename(first))()
        super().__init__(args, *rest, **kwargs)

subprocess.Popen = _GuardedPopen
atexit.register(_write)
'''

MARKER = "marker-7f3e19c4-not-a-real-key"


class Optionality(Case):
    """I1: without .context/jev.json nothing reaches a network or a model CLI, the provider
    client is never imported, no advisor file appears and a key in the environment is
    never written anywhere. Output and exit codes do not depend on that key."""

    def walk(self, label, env):
        root = self.root / label
        vault, home = root / "vault", root / "home"
        home.mkdir(parents=True)
        write_files(vault, {**HARBOR, "private/diary.md": "# Diary\n\nprivate pier notes\n"})
        guard = self.root / "guard"
        guard.mkdir(exist_ok=True)
        (guard / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
        log = root / "guard.jsonl"
        env = {**env, "PYTHONPATH": str(guard), "JEV_GUARD_LOG": str(log)}
        steps = [("init", ["init", str(vault), "--force"]),
                 ("index", ["index", str(vault)]),
                 ("search fts", ["search", str(vault), "--prompt", HARBOR_PROMPT]),
                 ("search synaptic", ["search", str(vault), "--prompt", HARBOR_PROMPT,
                                      "--method", "synaptic"]),
                 ("search fts --jev", ["search", str(vault), "--prompt", HARBOR_PROMPT, "--jev"]),
                 ("search synaptic --jev", ["search", str(vault), "--prompt", HARBOR_PROMPT,
                                            "--method", "synaptic", "--jev"]),
                 ("status", ["status", str(vault)]),
                 ("memory add", ["memory", "add", str(vault), "--kind", "note", "--text",
                                 "The pier lamps are checked weekly.",
                                 "--source", "people/Mira Holt.md"]),
                 ("memory resume", ["memory", "resume", str(vault)]),
                 ("jev status", ["jev", "status", str(vault)]),
                 ("jev status --json", ["jev", "status", str(vault), "--json"]),
                 ("jev report", ["jev", "report", str(vault)])]
        results = {}
        for name, argv in steps:
            done = cli(*argv, home=home, env=env)
            results[name] = (done.returncode, self.normalise(done.stdout, root))
        return vault, home, log, results

    @staticmethod
    def normalise(text, root):
        text = text.replace(str(root), "<root>")
        text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
                      "<time>", text)
        text = re.sub(r"\d+\.\d+s\b", "<seconds>", text)
        # The coverage receipt names the index file, whose bytes differ between two builds.
        text = re.sub(r'("index_sha256":\s*")[0-9a-f]{64}"', r"\1<index>\"", text)
        return re.sub(r"\b\d+[smhd] old\b", "<age> old", text)

    def test_unconfigured_walk_calls_nothing_and_leaves_no_trace(self):
        marked = {"TYPESAFE_API_KEY": MARKER, "OPENAI_API_KEY": MARKER}
        vault, home, log, with_key = self.walk("marked", marked)
        _, _, _, without_key = self.walk("plain", {})
        expected = {"index": 0, "search fts": 0, "search synaptic": 0, "search fts --jev": 0,
                    "search synaptic --jev": 0, "status": 0, "memory add": 0,
                    "memory resume": 0, "jev status": 0, "jev status --json": 0,
                    "jev report": 0}
        for name, code in expected.items():
            self.assertEqual(with_key[name][0], code, name)
        self.assertEqual(with_key, without_key)
        self.assertEqual(with_key["search fts --jev"][1], with_key["search fts"][1])
        self.assertEqual(with_key["search synaptic --jev"][1], with_key["search synaptic"][1])
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertGreaterEqual(len(records), len(expected) + 4)  # CLI + retrieval children
        self.assertEqual([r for r in records if r["advisor_modules"] or r["blocked"]], [])
        self.assertEqual(sorted(p.name for p in (vault / ".context").iterdir()
                                if p.name.startswith("jev")), [])
        for base in (vault, home):
            for path in base.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    self.assertNotIn(MARKER.encode(), path.read_bytes(), str(path))

    def test_the_guard_does_catch_an_attempt(self):
        """The guard is live: a direct connect in a guarded process fails loudly."""
        guard = self.root / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
        done = subprocess.run([sys.executable, "-c", "import socket; "
                               "socket.create_connection(('127.0.0.1', 9))"],
                              capture_output=True, text=True,
                              env={**CLEAN_ENV, "PYTHONPATH": str(guard), "HOME": str(self.home)})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("NetworkAttempted", done.stderr)


# ---------------------------------------------------------------------------
# I2: shadow changes nothing
# ---------------------------------------------------------------------------

class ShadowIdentity(Case):
    """For generated queries on three fictional vaults, three flag sets, fts and synaptic:
    --jev-candidates alone leaves the packet identical, and shadow advice with providers
    that answer yes, no, randomly, malformed, too slowly or by raising leaves every byte of
    the packet as off returns it (only the top-level `jev` block is added)."""

    def prepare(self, name, source=None, build=None):
        vault = self.root / name
        cases = []
        if source is not None:
            shutil.copytree(source, vault, ignore=shutil.ignore_patterns(
                "*.sqlite*", "index-manifest.json*", "activation.json", ".context-runs",
                "jev*"))
        if build is not None:
            cases = build(vault)
        if not (vault / ".context" / "routes.json").is_file():
            done = cli("init", str(vault), home=self.home)
            self.assertIn(done.returncode, (0, 1), done.stderr)
        done = cli("index", str(vault), home=self.home)
        self.assertEqual(done.returncode, 0, done.stderr)
        configure(vault, "shadow", timeout_s=0.05, cache_ttl_s=0)
        return vault, cases

    def check(self, vault, prompts, seed):
        slow = Slow()
        self.addCleanup(slow.release.set)
        compared = with_candidates = 0
        combo = 0
        for flags in FLAGS:
            for method in ("fts", "synaptic"):
                for prompt in prompts:
                    combo += 1
                    with self.subTest(vault=vault.name, flags=flags, method=method, prompt=prompt):
                        code, off = run_retrieve(vault, method, prompt, flags)
                        self.assertEqual(code, 0, off)
                        plan = plan_for(vault, method, flags)
                        self.assertIsNotNone(plan)
                        code, side = run_retrieve(vault, method, prompt, flags, plan.candidates)
                        self.assertEqual(without(side, "jev_candidates"), off)
                        with_candidates += bool(side["jev_candidates"]["items"])
                        before = copy.deepcopy(side)
                        runs = dict(evaluators(combo))
                        if combo % 12 == 0:
                            runs["slow"] = slow
                        for label, evaluate in runs.items():
                            out = jev.advise_search(vault, prompt, method, side, plan,
                                                    evaluate_fn=evaluate, contracts=FakeContracts,
                                                    environ={})
                            self.assertEqual(without(out, "jev"), off, label)
                            self.assertEqual(json.dumps(out["evidence"], ensure_ascii=False),
                                             json.dumps(off["evidence"], ensure_ascii=False))
                            self.assertEqual(out["jev"]["mode"], "shadow")
                            self.assertFalse(out["jev"]["applied"])
                            self.assertNotIn("internal_error", out["jev"]["codes"], label)
                            compared += 1
                        self.assertEqual(side, before)            # the input is not mutated
        return compared, with_candidates

    def test_shadow_identity_on_the_dev_vault(self):
        vault, cases = self.prepare("dev", build=dev_bridge.build)
        compared, with_candidates = self.check(vault, generated_prompts(vault, cases[::4], 8, 11),
                                               11)
        self.assertGreater(compared, 500)
        self.assertGreater(with_candidates, 20)                   # the property is not vacuous

    def test_shadow_identity_on_the_fixture_vaults(self):
        docs, _ = self.prepare("docs", source=REPO / "eval" / "fixtures" / "docs")
        example, _ = self.prepare("example", source=REPO / "router" / "example-vault")
        compared = self.check(docs, generated_prompts(docs, [], 8, 5), 5)[0]
        compared += self.check(example, generated_prompts(example, [], 8, 7), 7)[0]
        self.assertGreater(compared, 400)


# ---------------------------------------------------------------------------
# I3 / I4: `on` only appends byte-exact passages within the budget
# ---------------------------------------------------------------------------

class MonotoneEvidence(Case):

    def check(self, vault, prompts, evaluate, budget):
        rescued_packets = 0
        for flags in FLAGS:
            for method in ("fts", "synaptic"):
                for prompt in prompts:
                    with self.subTest(vault=vault.name, flags=flags, method=method, prompt=prompt):
                        code, off = run_retrieve(vault, method, prompt, flags)
                        out, _ = advise(vault, method, prompt, evaluate, flags)
                        base = off["evidence"]
                        self.assertEqual(out["evidence"][:len(base)], base)   # prefix unchanged
                        extras = out["evidence"][len(base):]
                        self.assertTrue(all(e["origin"] == "jev" for e in extras))
                        self.assertLessEqual(sum(e["est_tokens"] for e in extras), budget)
                        self.assertEqual(out["jev"]["extra_est_tokens"],
                                         sum(e["est_tokens"] for e in extras))
                        self.assertTrue(out["jev"]["superset"])
                        self.assertEqual(without(out, "jev", "evidence", "status"),
                                         without(off, "evidence", "status"))
                        spans = {}
                        for item in out["evidence"]:
                            start, end = item.get("start"), item.get("end")
                            if start is None:
                                start, end = 0, len(item["content"].encode("utf-8"))
                            for lo, hi in spans.get(item["source_path"], []):
                                self.assertFalse(lo < end and start < hi, item["source_path"])
                            spans.setdefault(item["source_path"], []).append((start, end))
                        for item in extras:
                            self.assert_byte_exact(vault, item)
                            self.assertEqual(sum(e["content"] == item["content"]
                                                 for e in out["evidence"]), 1)
                        rescued_packets += bool(extras)
        return rescued_packets

    def test_on_appends_only_on_the_dev_vault(self):
        vault = self.root / "dev"
        cases = dev_bridge.build(vault)
        build_vault(vault, {}, self.home)
        configure(vault, "on", cache_ttl_s=0)
        prompts = generated_prompts(vault, cases[::4], 6, 3)
        rescued = self.check(vault, prompts, Provider(lambda q: answer(0.97)), 400)
        self.assertGreater(rescued, 20)
        configure(vault, "on", cache_ttl_s=0, jev_extra_tokens=60)
        self.check(vault, prompts[:12], Provider(lambda q: answer(0.97)), 60)
        rng = random.Random(9)
        configure(vault, "on", cache_ttl_s=0)
        self.check(vault, prompts[:12], Provider(lambda q: answer(round(rng.random(), 3))), 400)

    def test_zero_budget_is_the_packet_without_the_advisor(self):
        self.harbor()
        configure(self.vault, "on", jev_extra_tokens=0, cache_ttl_s=0)
        out, packet = advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(lambda q: answer(0.99)))
        self.assertEqual(out["evidence"], without(packet, "jev_candidates")["evidence"])
        self.assertEqual(out["jev"]["counts"]["rescued"], 0)


# ---------------------------------------------------------------------------
# F-5 regression: a linked note that shares no word with the question
# ---------------------------------------------------------------------------

class HarborLights(Case):
    """docs/synapse.md lists lexical link relevance as a limit: the answer note is reached
    through a link whose paragraph shares no query word, so it is activated but not
    delivered. With the advisor on and a judge that says yes to it, it is delivered as
    exactly its reserved passage, after the unchanged packet."""

    def judge(self):
        return Provider(lambda q: answer(0.91 if name_of(q) == "Mira Holt" else 0.08))

    def test_off_does_not_deliver_the_answer_note(self):
        self.harbor()
        code, packet = run_retrieve(self.vault, "synaptic", HARBOR_PROMPT)
        self.assertEqual(packet["synapse"]["decision"], "no_relevant_link")
        self.assertNotIn("people/Mira Holt.md", [e["source_path"] for e in packet["evidence"]])
        nodes = {n["path"]: n for n in json.loads(
            (self.vault / ".context" / "activation.json").read_text())["nodes"]}
        self.assertEqual((nodes["people/Mira Holt.md"]["hop"],
                          nodes["people/Mira Holt.md"]["selected"]), (1, False))

    def test_side_channel_offers_both_link_reached_notes(self):
        self.harbor()
        _, packet = run_retrieve(self.vault, "synaptic", HARBOR_PROMPT, candidates=12)
        items = {c["source_path"]: c for c in packet["jev_candidates"]["items"]}
        self.assertEqual(sorted(items), ["people/Ansel Varga.md", "people/Mira Holt.md"])
        mira = items["people/Mira Holt.md"]
        self.assertEqual((mira["kind"], mira["hop"]), ("link", 1))
        self.assertEqual(mira["link_line"]["content"],
                         "Ask [[Mira Holt]] about anything beyond the opening week.")
        self.assertEqual(mira["via"][0]["text"], "projects/Harbor Lights.md links to "
                         "people/Mira Holt.md (wikilink, projects/Harbor Lights.md:5)")
        for passage in mira["passages"] + [mira["link_line"]]:
            self.assert_byte_exact(self.vault, passage)

    def test_on_rescues_exactly_the_reserved_passage(self):
        self.harbor()
        configure(self.vault, "on")
        code, off = run_retrieve(self.vault, "synaptic", HARBOR_PROMPT)
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, self.judge())
        self.assertEqual(out["evidence"][:len(off["evidence"])], off["evidence"])
        extras = out["evidence"][len(off["evidence"]):]
        self.assertEqual([e["source_path"] for e in extras], ["people/Mira Holt.md"])
        text = (self.vault / "people" / "Mira Holt.md").read_text(encoding="utf-8")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        reserved = synapse.reserved_passages("people/Mira Holt.md", text, sha, 0.25, [], [], [],
                                             set(), synapse.RESERVE_TOKENS)
        self.assertEqual([extras[0]["content"]], [p.content for p in reserved])
        self.assert_byte_exact(self.vault, extras[0])
        self.assertEqual(extras[0]["origin"], "jev")
        self.assertEqual(extras[0]["jev"]["p_yes"], 0.91)
        self.assertEqual(extras[0]["via"][0]["from"], "projects/Harbor Lights.md")
        block = out["jev"]
        self.assertEqual((block["mode"], block["applied"], block["superset"]), ("on", True, True))
        verdicts = {c["source_path"]: (c["verdict"], c["rescued"]) for c in block["candidates"]}
        self.assertEqual(verdicts, {"people/Mira Holt.md": ("on_topic", True),
                                    "people/Ansel Varga.md": ("off_topic", False)})
        trace = json.loads((self.vault / ".context" / "activation.json").read_text())
        nodes = {n["path"]: n for n in trace["nodes"]}
        self.assertEqual(trace["version"], 1)
        self.assertEqual((nodes["people/Mira Holt.md"]["jev"],
                          nodes["people/Mira Holt.md"]["selected"]), ("rescued", True))
        self.assertEqual(nodes["people/Ansel Varga.md"]["jev"], "off_topic")
        self.assertEqual(trace["jev"]["rescued"], 1)

    def test_shadow_counts_the_rescue_and_changes_nothing(self):
        self.harbor()
        configure(self.vault, "shadow")
        code, off = run_retrieve(self.vault, "synaptic", HARBOR_PROMPT)
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, self.judge())
        self.assertEqual(without(out, "jev"), off)
        self.assertEqual((out["jev"]["counts"]["would_rescue"], out["jev"]["counts"]["rescued"]),
                         (1, 0))
        row = log_rows(self.vault)[-1]
        self.assertEqual((row["mode"], row["would_rescue"], row["rescued"], row["applied"]),
                         ("shadow", 1, 0, False))
        nodes = {n["path"]: n for n in json.loads(
            (self.vault / ".context" / "activation.json").read_text())["nodes"]}
        self.assertEqual((nodes["people/Mira Holt.md"]["jev"],
                          nodes["people/Mira Holt.md"]["selected"]), ("on_topic", False))

    def test_trace_is_left_alone_when_trace_is_false(self):
        self.harbor()
        configure(self.vault, "on", trace=False)
        advise(self.vault, "synaptic", HARBOR_PROMPT, self.judge())
        trace = json.loads((self.vault / ".context" / "activation.json").read_text())
        self.assertNotIn("jev", trace)
        self.assertTrue(all("jev" not in n for n in trace["nodes"]))

    def test_cli_without_the_advisor_modules_falls_back_to_the_local_packet(self):
        # F2-41: always run, on a copy of the package that lacks the two advisor modules.
        self.harbor()
        stripped = self.root / "stripped"
        for name in ("context_layer", "router", "eval"):
            shutil.copytree(REPO / name, stripped / name, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "jev_client.py", "jev_contracts.py", "example-vault",
                "run-output", "live-pilot-0.3"))
        self.assertFalse((stripped / "context_layer" / "jev_client.py").exists())
        self.assertFalse((stripped / "context_layer" / "jev_contracts.py").exists())

        def here(*argv):
            return cli(*argv, home=self.home, cwd=stripped)

        self.assertEqual(here("jev", "shadow", str(self.vault), "--provider-kind",
                              "fake").returncode, 0)
        plain = here("search", str(self.vault), "--prompt", HARBOR_PROMPT, "--method",
                     "synaptic")
        asked = here("search", str(self.vault), "--prompt", HARBOR_PROMPT, "--method",
                     "synaptic", "--jev")
        self.assertEqual((plain.returncode, asked.returncode), (0, 0))
        packet = json.loads(asked.stdout)
        self.assertEqual(packet.pop("jev")["codes"], ["advisor_unavailable"])
        self.assertEqual(packet, json.loads(plain.stdout))
        self.assertEqual(log_rows(self.vault)[-1]["code"], "advisor_unavailable")


# ---------------------------------------------------------------------------
# I5: every failure falls back to the local packet, plus one counter row
# ---------------------------------------------------------------------------

class FailToLocal(Case):

    def setUp(self):
        super().setUp()
        self.harbor()
        configure(self.vault, "on", cache_ttl_s=0, timeout_s=0.05)
        _, self.off = run_retrieve(self.vault, "synaptic", HARBOR_PROMPT)

    def expect_local(self, evaluate, code, prompt=HARBOR_PROMPT, environ=None, calls=None):
        rows = len(log_rows(self.vault))
        off = self.off if prompt == HARBOR_PROMPT else \
            run_retrieve(self.vault, "synaptic", prompt)[1]
        out, _ = advise(self.vault, "synaptic", prompt, evaluate, environ=environ)
        self.assertEqual(without(out, "jev"), off)
        self.assertTrue(out["jev"]["degraded"])
        self.assertEqual(out["jev"]["codes"][0], code)
        self.assertFalse(out["jev"]["applied"])
        self.assertEqual(out["jev"]["counts"]["rescued"], 0)
        after = log_rows(self.vault)
        self.assertEqual(len(after), rows + 1)
        self.assertEqual((after[-1]["code"], after[-1]["degraded"]), (code, True))
        if calls is not None:
            self.assertEqual(evaluate.calls, calls)
        return out

    def test_the_same_setup_rescues_when_nothing_fails(self):
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(lambda q: answer(0.97)))
        self.assertEqual(out["jev"]["counts"]["rescued"], 2)
        self.assertFalse(out["jev"]["degraded"])

    def test_timeout(self):
        slow = Slow()
        self.addCleanup(slow.release.set)
        self.expect_local(slow, "deadline_exceeded")

    def test_provider_errors(self):
        self.expect_local(Provider(lambda q: failure("http_server_error")), "http_server_error")
        self.expect_local(Provider(lambda q: failure("Free Text Is Not A Code")),
                          "provider_error")
        self.expect_local(evaluators(1)["raises"], "provider_error")

    def test_invalid_answers(self):
        self.expect_local(evaluators(1)["not_a_list"], "answers_invalid")
        self.expect_local(evaluators(1)["wrong_length"], "answers_invalid")
        self.expect_local(evaluators(1)["bad_answers"], "answer_invalid")
        self.expect_local(Provider(lambda q: {"ok": True, "answer": {"label": "yes",
                                                                     "p_yes": float("nan")}}),
                          "answer_invalid")

    def test_secret_in_the_prompt_stops_the_call(self):
        provider = Provider(lambda q: answer(0.97))
        prompt = HARBOR_PROMPT + " api_key = sk-test0123456789abcdefghij"
        out = self.expect_local(provider, "sensitive_input", prompt=prompt, calls=0)
        self.assertEqual(provider.seen, [])
        self.assertNotIn("sk-test", json.dumps(log_rows(self.vault)))
        self.assertNotIn("sk-test", json.dumps(out["jev"]))

    def test_secret_in_a_candidate_drops_that_question_only(self):
        # The note with the credential-shaped string is never asked about (so it is never
        # rescued); the other questions are still asked, and the code is named.
        note = self.vault / "people" / "Ansel Varga.md"
        note.write_text(note.read_text() + "\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz0123\n")
        self.assertEqual(self.cli("index", str(self.vault)).returncode, 0)
        _, self.off = run_retrieve(self.vault, "synaptic", HARBOR_PROMPT)
        provider = Provider(lambda q: answer(0.97))
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, provider)
        self.assertEqual(provider.calls, 1)
        self.assertNotIn("Bearer", json.dumps(provider.seen))
        self.assertNotIn("Ansel Varga", [q["state"]["title"] for q in provider.seen])
        self.assertIn("sensitive_input", out["jev"]["codes"])
        rescued = [e["source_path"] for e in out["evidence"] if e.get("origin") == "jev"]
        self.assertEqual(rescued, ["people/Mira Holt.md"])
        self.assertEqual([c["verdict"] for c in out["jev"]["candidates"]
                          if c["source_path"] == "people/Ansel Varga.md"], ["not_judged"])

    def test_blocklist_literal_drops_that_question_and_an_unreadable_list_stops_the_call(self):
        outside = self.root / "blocklist.txt"
        outside.write_text("# literals\nwinter storm log\n", encoding="utf-8")
        configure(self.vault, "on", cache_ttl_s=0, timeout_s=0.05, blocklist_file=str(outside))
        provider = Provider(lambda q: answer(0.97))
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, provider)
        self.assertEqual(provider.calls, 1)
        self.assertNotIn("winter storm log", json.dumps(provider.seen))
        self.assertIn("sensitive_input", out["jev"]["codes"])
        rescued = [e["source_path"] for e in out["evidence"] if e.get("origin") == "jev"]
        self.assertEqual(rescued, ["people/Ansel Varga.md"])
        outside.unlink()
        self.expect_local(Provider(lambda q: answer(0.97)), "blocklist_unreadable", calls=0)

    def test_short_prompt_makes_no_call(self):
        provider = Provider(lambda q: answer(0.97))
        rows = len(log_rows(self.vault))
        out, _ = advise(self.vault, "synaptic", "harbor lamp", provider)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(out["jev"]["codes"], ["prompt_too_short"])
        self.assertEqual(len(log_rows(self.vault)), rows + 1)

    def test_source_edited_during_the_call(self):
        def edit():
            path = self.vault / "people" / "Mira Holt.md"
            path.write_text(path.read_text() + "\nEdited while the judge was thinking.\n")
        self.expect_local(Provider(lambda q: answer(0.97), during=edit), "source_changed")

    def test_configuration_edited_during_the_call(self):
        def rewrite():
            configure(self.vault, "on", cache_ttl_s=0, timeout_s=0.05)   # same bytes, new file
        self.expect_local(Provider(lambda q: answer(0.97), during=rewrite), "config_changed")

    def test_routes_edited_during_the_call(self):
        def edit():
            routes = self.vault / ".context" / "routes.json"
            routes.write_text(routes.read_text() + "\n")
        self.expect_local(Provider(lambda q: answer(0.97), during=edit), "config_changed")

    def test_kill_switch_set_during_the_call(self):
        def kill():
            (self.vault / ".context" / "jev.disabled").write_text("")
        self.expect_local(Provider(lambda q: answer(0.97), during=kill), "kill_switch")

    def test_kill_switch_set_after_planning_stops_the_send(self):
        plan = plan_for(self.vault, "synaptic")
        _, packet = run_retrieve(self.vault, "synaptic", HARBOR_PROMPT,
                                 candidates=plan.candidates)
        (self.vault / ".context" / "jev.disabled").write_text("")
        provider = Provider(lambda q: answer(0.97))
        out = jev.advise_search(self.vault, HARBOR_PROMPT, "synaptic", packet, plan,
                                evaluate_fn=provider, contracts=FakeContracts, environ={})
        self.assertEqual(provider.calls, 0)
        self.assertEqual(without(out, "jev"), self.off)
        self.assertEqual(out["jev"]["codes"], ["kill_switch"])

    def test_kill_switches_disabled_feature_and_child_guard_run_the_plain_search(self):
        cases = [({"CONTEXT_LAYER_JEV_DISABLE": "1"}, "kill_switch", None),
                 ({"CONTEXT_LAYER_JEV_CHILD": "1"}, "child_guard", None),
                 ({}, "kill_switch", "file"),
                 ({}, "feature_disabled", "feature")]
        for environ, code, how in cases:
            with self.subTest(code=code, how=how):
                configure(self.vault, "on", cache_ttl_s=0,
                          features=["answer"] if how == "feature" else ["search"])
                flag = self.vault / ".context" / "jev.disabled"
                if how == "file":
                    flag.write_text("")
                provider = Provider(lambda q: answer(0.97))
                rows = len(log_rows(self.vault))
                out, packet = advise(self.vault, "synaptic", HARBOR_PROMPT, provider,
                                     environ=environ)
                self.assertIsNone(out)                        # the plain search ran
                self.assertEqual(packet, self.off)
                self.assertEqual(provider.calls, 0)
                self.assertEqual(log_rows(self.vault)[rows:][0]["code"], code)
                self.assertEqual(len(log_rows(self.vault)), rows + 1)
                flag.unlink(missing_ok=True)

    def test_contract_error_falls_back(self):
        class Broken(FakeContracts):
            @classmethod
            def build_questionnaire(cls, purpose, template, state):
                raise cls.ContractError("templates changed")
        rows = len(log_rows(self.vault))
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(lambda q: answer(0.9)),
                        contracts=Broken)
        self.assertEqual(without(out, "jev"), self.off)
        self.assertEqual(out["jev"]["codes"], ["contract_error"])
        self.assertEqual(len(log_rows(self.vault)), rows + 1)

    def test_on_without_a_valid_receipt_is_shadow(self):
        jev.receipt_path(self.vault, {"kind": "fake"}).unlink()
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(lambda q: answer(0.97)))
        self.assertEqual(without(out, "jev"), self.off)
        self.assertEqual((out["jev"]["mode"], out["jev"]["codes"]),
                         ("shadow", ["calibration_required"]))
        write_receipt(self.vault, template_revision="other-templates")
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(lambda q: answer(0.97)))
        self.assertEqual(out["jev"]["mode"], "shadow")

    def test_one_failed_question_leaves_the_others_usable(self):
        def decide(q):
            return failure("http_rate_limited") if name_of(q) == "Ansel Varga" else answer(0.95)
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(decide))
        self.assertTrue(out["jev"]["degraded"])
        self.assertEqual(out["jev"]["codes"], ["http_rate_limited"])
        rescued = [e["source_path"] for e in out["evidence"] if e["origin"] == "jev"]
        self.assertEqual(rescued, ["people/Mira Holt.md"])


# ---------------------------------------------------------------------------
# Privacy gates
# ---------------------------------------------------------------------------

VARIANTS = {
    # allowed to be sent
    "cards/open-plain.md": ("", True),
    "cards/open-true.md": ("---\nremote_allowed: true\n---\n", True),
    "cards/open-public.md": ("---\nsensitivity: public\n---\n", True),
    "cards/open-internal.md": ("---\nsensitivity: \"internal\"\n---\n", True),
    "cards/open-visible.md": ("---\nvisibility: public\njev: true\n---\n", True),
    # local only
    "cards/shut-false.md": ("---\nremote_allowed: false\n---\n", False),
    "cards/shut-quoted.md": ("---\nremote_allowed: \"false\"\n---\n", False),
    "cards/shut-no.md": ("---\nremote_allowed: no\n---\n", False),
    "cards/shut-quoted-true.md": ("---\nremote_allowed: \"true\"\n---\n", False),
    "cards/shut-sensitive.md": ("---\nsensitivity: sensitive\n---\n", False),
    "cards/shut-confidential.md": ("---\nsensitivity: confidential\n---\n", False),
    "cards/shut-private.md": ("---\nvisibility: private\n---\n", False),
    "cards/shut-jev.md": ("---\njev: false\n---\n", False),
    "cards/shut-nested.md": ("---\nmeta:\n  visibility: private\n---\n", False),
    "cards/shut-unclosed.md": ("---\nremote_allowed: true\n", False),
    "cards/shut-bom.md": ("\ufeff---\nremote_allowed: true\n---\n", False),
    "diary/shut-prefix.md": ("", False),
}


class PrivacyGates(Case):

    def build(self):
        files, links = {}, []
        for number, (path, (front, _)) in enumerate(sorted(VARIANTS.items())):
            stem = Path(path).stem
            word = f"quillmark{number:02d}"
            files[path] = f"{front}# {stem}\n\nThe keeper wrote {word} beside the ledger.\n"
            links.append(f"Entry [[{stem}]].")
        files["private/shut-excluded.md"] = "# shut-excluded\n\nquillmarkzz beside the ledger.\n"
        links.append("Entry [[shut-excluded]].")
        files["registry.md"] = "# Registry\n\nThe registry holds harbour variants overview.\n\n" \
                               + "\n\n".join(links) + "\n"
        build_vault(self.vault, files, self.home)
        configure(self.vault, "shadow", max_candidates=32, local_only_prefixes=["diary"],
                  cache_ttl_s=0)

    def test_local_only_notes_never_reach_a_request(self):
        self.build()
        provider = Provider(lambda q: answer(0.9))
        out, packet = advise(self.vault, "synaptic", "registry harbour variants overview",
                             provider)
        offered = {c["source_path"] for c in packet["jev_candidates"]["items"]}
        self.assertEqual(offered, set(VARIANTS))            # every variant is a candidate
        sent = provider.text()
        for number, (path, (_, allowed)) in enumerate(sorted(VARIANTS.items())):
            with self.subTest(path=path):
                word = f"quillmark{number:02d}"
                if allowed:
                    self.assertIn(word, sent)
                else:
                    self.assertNotIn(word, sent)
        self.assertNotIn("quillmarkzz", sent)
        self.assertNotIn("cards/", sent)                    # file names without folders
        self.assertNotIn("diary", sent)
        self.assertNotIn(".md", sent)
        verdicts = {c["source_path"]: c["verdict"] for c in out["jev"]["candidates"]}
        for path, (_, allowed) in VARIANTS.items():
            self.assertEqual(verdicts[path] == "local_only", not allowed, path)
        self.assertEqual(out["jev"]["counts"]["local_only"],
                         sum(1 for _, allowed in VARIANTS.values() if not allowed))

    def test_frontmatter_convention(self):
        for text, reason in (
                ("# plain\n", None),
                ("---\nremote_allowed: true\n---\n", None),
                ("---\nremote_allowed: True\n---\n", "remote_allowed_not_true"),
                ("---\nremote_allowed: true # yes\n---\n", "frontmatter_uncertain"),
                ("---\nremote_allowed:\n  - true\n---\n", "remote_allowed_not_true"),
                ("---\nsensitivity: normal\n---\n", None),
                ("---\nsensitivity: Public\n---\n", "sensitivity"),
                ("---\nvisibility: [private]\n---\n", "frontmatter_uncertain"),
                # F2-38: a value this reader cannot take at face value stays local
                ("---\nvisibility: >\n  private\n---\n", "frontmatter_uncertain"),
                ("---\nvisibility: |-\n  private\n---\n", "frontmatter_uncertain"),
                ("---\nvisibility: private # keep\n---\n", "frontmatter_uncertain"),
                ("---\nvisibility: *level\n---\n", "frontmatter_uncertain"),
                ("---\nsensitivity: !!str public\n---\n", "frontmatter_uncertain"),
                ("---\nvisibility: public\n---\n", None),
                ("---\nVisibility: Private\n---\n", "visibility"),
                ("---\ntags: [jev]\n---\n", "frontmatter_uncertain"),
                ("---\njev-notes: ok\n---\n", None),
                ("\ufeff# plain\n", "frontmatter_uncertain"),
                ("\n---\nvisibility: private\n---\n", None)):
            with self.subTest(text=text):
                self.assertEqual(jev.local_only_reason(text), reason)


# ---------------------------------------------------------------------------
# I6: no text at rest
# ---------------------------------------------------------------------------

class NoTextAtRest(Case):

    def test_log_cache_trace_and_config_hold_no_prompt_or_note_text(self):
        self.harbor()
        prompt = HARBOR_PROMPT + " zephyrquartz"
        for mode in ("shadow", "on"):
            configure(self.vault, mode, cache_ttl_s=3600)
            for _ in range(2):
                advise(self.vault, "synaptic", prompt, Provider(lambda q: answer(0.93)))
        context = self.vault / ".context"
        markers = [b"zephyrquartz", b"weekly checks", b"Mira Holt looks", b"opening week",
                   b"harbor lights"]
        checked = [context / "jev-calls.jsonl", context / "jev.json", context / "activation.json",
                   *sorted((context / "jev-cache").iterdir())]
        self.assertGreater(len(checked), 4)
        for path in checked:
            data = path.read_bytes()
            for marker in markers:
                self.assertNotIn(marker, data, f"{path.name}: {marker!r}")
        for path in sorted((context / "jev-cache").iterdir()):
            self.assertRegex(path.name, r"^[0-9a-f]{64}\.json$")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((context / "jev-cache").stat().st_mode), 0o700)
        for name in ("jev-calls.jsonl", "jev.json", "jev.salt"):
            self.assertEqual(stat.S_IMODE((context / name).stat().st_mode), 0o600, name)


# ---------------------------------------------------------------------------
# I7 and I8
# ---------------------------------------------------------------------------

class AdviceOnlyAndLossyNamed(Case):

    def test_search_never_writes_memory_and_keeps_exit_codes(self):
        self.harbor()
        memory.record(self.vault, kind="note", text="Lamps are checked weekly.",
                      sources=[{"path": "people/Mira Holt.md"}])
        records = (self.vault / ".context" / "memory" / "records.jsonl").read_bytes()
        for mode in ("shadow", "on"):
            configure(self.vault, mode)
            for method in ("fts", "synaptic"):
                done = self.cli("search", str(self.vault), "--prompt", HARBOR_PROMPT,
                                "--method", method, "--jev")
                self.assertEqual(done.returncode, 0, done.stderr)
                advise(self.vault, method, HARBOR_PROMPT, Provider(lambda q: answer(0.9)))
        self.assertEqual((self.vault / ".context" / "memory" / "records.jsonl").read_bytes(),
                         records)

    def test_prune_fts_is_lossy_and_says_so(self):
        self.harbor()
        configure(self.vault, "on", lossy={"prune_fts": True}, cache_ttl_s=0)

        def decide(q):
            return answer(0.05 if name_of(q) == "Launch Plan" else 0.95)

        out, packet = advise(self.vault, "fts", HARBOR_PROMPT, Provider(decide))
        paths = [e["source_path"] for e in out["evidence"]]
        self.assertNotIn("plans/Launch Plan.md", paths)
        self.assertFalse(out["jev"]["superset"])
        self.assertEqual(out["jev"]["counts"]["pruned"], 1)
        info = jev.status(self.vault)
        self.assertFalse(info["superset"])
        self.assertIn("superset: no", jev.render_status(info))
        configure(self.vault, "shadow", lossy={"prune_fts": True}, cache_ttl_s=0)
        out, packet = advise(self.vault, "fts", HARBOR_PROMPT, Provider(decide))
        self.assertEqual(without(out, "jev"), without(packet, "jev_candidates"))
        self.assertTrue(out["jev"]["superset"])
        self.assertEqual(log_rows(self.vault)[-1]["would_prune"], 1)


# ---------------------------------------------------------------------------
# Configuration file and the `jev` commands
# ---------------------------------------------------------------------------

@unittest.skipUnless(CONTRACTS_PRESENT and CLIENT_PRESENT, "needs the merged advisor modules")
class RecordAndCalibrate(Case):
    """`jev record` asks a file of questions and keeps hashes, labels and counters (never
    text); `jev calibrate` turns a recorded dev-set evaluation into the receipt `on`
    needs; `--recording FILE` configures the replaying provider."""

    def setUp(self):
        super().setUp()
        self.harbor()
        from context_layer import jev_contracts
        self.contracts = jev_contracts
        # Only the search feature's questions are recorded here, so only its bars are needed.
        self.assertEqual(self.cli("jev", "shadow", str(self.vault), "--provider-kind",
                                  "fake", "--disable", "answer", "--disable",
                                  "memory").returncode, 0)
        self.questions = self.root / "questions.jsonl"
        self.questions.write_text("".join(json.dumps(self.question(n)) + "\n" for n in range(5)),
                                  encoding="utf-8")
        self.out = self.root / "recording.jsonl"
        self.script = self.root / "fake-judge.py"
        self.script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "json.loads(sys.stdin.read())\n"
            "print(json.dumps({'answers': {'q': {'type': 'noul', 'noul': 0.9}}, "
            "'usage': {'input_tokens': 30, 'output_tokens': 1}, 'model': 'fake-judge-1'}))\n",
            encoding="utf-8")
        self.script.chmod(0o755)
        self.env = {"CONTEXT_LAYER_JEV_FAKE": str(self.script)}
        self.receipt = self.vault / ".context" / "jev-calibration" / "fake-none.json"

    def question(self, n, excerpt="Mira Holt looks over the pier lamps once they are running."):
        return self.contracts.build_questionnaire(
            "search", "relevance.v1",
            {"request": f"who maintains the harbor lights after launch {n}",
             "title": "Mira Holt", "link_line": "", "excerpt": excerpt})

    def record(self, *extra, env=None):
        return self.cli("jev", "record", str(self.vault), "--questions", str(self.questions),
                        "--out", str(self.out), *extra, env=env if env is not None else self.env)

    def report(self, rows=5, **changes):
        report = {"schema": "jev-dev-eval/v1", "status": "evaluated", "provider": "recorded",
                  "dev_set_sha256": "1" * 64, "dev_set_version": 1,
                  "thresholds": {**jev.DEFAULT_THRESHOLDS, "source": "checkout"},
                  "privacy": {"ok": True}, "integrity": {"ok": True},
                  "recording": {"rows": rows, "questionnaires": rows, "found": rows},
                  "relevance": {"precision": 0.93, "recall": 0.9, "positives_judged": 15,
                                "negatives_judged": 120,
                                "injection_vs_neutral": {"injection_not_above_neutral": True,
                                                         "injection_rate": 0.0,
                                                         "neutral_rate": 0.05}},
                  "gate": {"judged": 0, "prompts": 12}, "claims": {"judged": 0, "claims": 16},
                  "memory": {"judged": 0, "proposals": 10}}
        report.update(changes)
        path = self.root / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def calibrate(self, report, *extra):
        return self.cli("jev", "calibrate", str(self.vault), "--report", str(report),
                        "--recording", str(self.out), *extra)

    def test_dry_run_sends_nothing_and_run_keeps_only_hashes_labels_and_counters(self):
        dry = self.record()
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("dry run: nothing was sent", dry.stdout)
        self.assertIn("relevance.v1 x5", dry.stdout)
        self.assertFalse(self.out.exists())
        done = self.record("--run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("recorded 5 row(s): 5 answered, 0 failed", done.stdout)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        rows = [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertEqual(row["contract"], "jev-recording/v1")
            self.assertRegex(row["key"], r"^[0-9a-f]{64}$")
            self.assertEqual(row["raw"], {"q": {"type": "noul", "noul": 0.9}})
            self.assertEqual((row["provider"]["kind"], row["code"], row["model_reported"]),
                             ("fake", None, "fake-judge-1"))
            self.assertEqual(row["template"],
                             f"relevance.v1@{self.contracts.TEMPLATE_REVISION}")
            self.assertEqual(row["usage"]["input_tokens"], 30)
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn("Mira Holt", text)                      # no question text at rest
        self.assertNotIn("harbor lights", text)
        row = [r for r in log_rows(self.vault) if r["feature"] == "record"]
        self.assertEqual((len(row), row[0]["requests"], row[0]["judged"], row[0]["degraded"]),
                         (1, 5, 5, False))
        again = self.record("--run")                              # never overwrites
        self.assertEqual(again.returncode, 1)
        self.assertIn("exists", again.stderr)
        appended = self.record("--run", "--append")
        self.assertEqual(appended.returncode, 0, appended.stderr)
        self.assertEqual(len(self.out.read_text(encoding="utf-8").splitlines()), 10)

    def test_record_refuses_secrets_kill_switches_and_bad_questions(self):
        self.questions.write_text(
            json.dumps(self.question(0, "token = abcdefghijklmnopqrstuvwxyz")) + "\n",
            encoding="utf-8")
        done = self.record("--run")
        self.assertEqual(done.returncode, 1)
        self.assertIn("credential", done.stderr)
        self.assertFalse(self.out.exists())
        self.questions.write_text('{"contract": "jev-questionnaire/v1"}\n', encoding="utf-8")
        done = self.record("--run")
        self.assertEqual(done.returncode, 1)
        self.assertIn("not a valid questionnaire", done.stderr)
        self.questions.write_text(json.dumps(self.question(0)) + "\n", encoding="utf-8")
        (self.vault / ".context" / "jev.disabled").touch()
        done = self.record("--run")
        self.assertEqual(done.returncode, 1)
        self.assertIn("kill switch", done.stderr)
        (self.vault / ".context" / "jev.disabled").unlink()
        failed = self.record("--run", env={"CONTEXT_LAYER_JEV_FAKE": ""})   # fake not configured
        self.assertEqual(failed.returncode, 1)
        self.assertIn("0 answered, 1 failed (fake_not_configured x1)", failed.stdout)
        # Refused before any I/O: nothing was asked, so the file holds no row.
        self.assertEqual(self.out.read_text(encoding="utf-8"), "")

    def test_calibrate_writes_a_receipt_that_lets_on_through(self):
        self.assertEqual(self.record("--run").returncode, 0)
        report = self.report()
        dry = self.calibrate(report)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("relevance          met", dry.stdout)
        self.assertIn("dry run", dry.stdout)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.cli("jev", "on", str(self.vault)).returncode, 1)
        done = self.calibrate(report, "--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual((receipt["schema"], receipt["provider"], receipt["template_revision"]),
                         ("jev-calibration/v1", {"kind": "fake", "model": None},
                          self.contracts.TEMPLATE_REVISION))
        self.assertTrue(receipt["purposes"]["relevance"]["passed"])
        self.assertFalse(receipt["purposes"]["topicality"]["passed"])
        self.assertEqual((receipt["recording_rows"], receipt["needed_now"]), (5, ["relevance"]))
        self.assertEqual(receipt["recording_sha256"],
                         hashlib.sha256(self.out.read_bytes()).hexdigest())
        self.assertNotIn(str(self.root), self.receipt.read_text(encoding="utf-8"))
        on = self.cli("jev", "on", str(self.vault))
        self.assertEqual(on.returncode, 0, on.stderr)
        status = json.loads(self.cli("jev", "status", str(self.vault), "--json").stdout)
        self.assertEqual((status["mode"], status["receipt"]["usable"]), ("on", True))
        # F2-40: the receipt is a local file; the status line does not present it as proof.
        shown = self.cli("jev", "status", str(self.vault)).stdout
        self.assertIn("a plain local file: it records that calibration was run, it does not "
                      "prove it", shown)
        # A bar not met: the receipt is written with passed: false and `on` is refused.
        self.cli("jev", "shadow", str(self.vault))
        low = self.calibrate(self.report(relevance={
            "precision": 0.5, "recall": 0.9, "positives_judged": 15, "negatives_judged": 120,
            "injection_vs_neutral": {"injection_not_above_neutral": True}}), "--apply")
        self.assertEqual(low.returncode, 1)
        self.assertIn("not met: relevance", low.stdout)
        self.assertFalse(json.loads(self.receipt.read_text())["purposes"]["relevance"]["passed"])
        self.assertEqual(self.cli("jev", "on", str(self.vault)).returncode, 1)

    def test_calibrate_refuses_what_cannot_make_a_receipt(self):
        self.assertEqual(self.record("--run").returncode, 0)
        cases = {
            "oracle": self.report(provider="oracle"),
            "privacy": self.report(privacy={"ok": False}),
            "thresholds": self.report(thresholds={**jev.DEFAULT_THRESHOLDS, "rescue": 0.7}),
            "rows": self.report(rows=4),
            "status": self.report(status="provider_not_configured"),
        }
        for name, report in cases.items():
            with self.subTest(refused=name):
                done = self.calibrate(report, "--apply")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("nothing was written", done.stderr)
                self.assertFalse(self.receipt.exists())
        other = self.root / "other.jsonl"                     # another provider's rows
        rows = [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            row["provider"] = {**row["provider"], "kind": "host_cli", "model": "judge"}
        other.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        done = self.cli("jev", "calibrate", str(self.vault), "--report", str(self.report()),
                        "--recording", str(other), "--apply")
        self.assertEqual(done.returncode, 1)
        self.assertIn("another provider", done.stderr)
        self.assertFalse(self.receipt.exists())

    def test_recording_flag_configures_a_replaying_provider(self):
        self.assertEqual(self.record("--run").returncode, 0)
        done = self.cli("jev", "shadow", str(self.vault), "--provider-kind", "recorded",
                        "--recording", str(self.out))
        self.assertEqual(done.returncode, 0, done.stderr)
        saved = json.loads((self.vault / ".context" / "jev.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["provider"], {"kind": "recorded", "recording": str(self.out),
                                             "replays": {"kind": "fake"}})
        # A recording cannot be made from the replaying provider.
        self.assertEqual(self.record("--run", "--append").returncode, 1)
        # Replaying: a question the recording holds is answered without any provider
        # (no fake script in the environment); a question it lacks is a recording_miss.
        from context_layer import jev_client
        results = jev_client.evaluate(saved["provider"], [self.question(0), self.question(9)],
                                      deadline_s=5.0, max_parallel=1, key=None)
        self.assertEqual([(r["ok"], r["code"]) for r in results],
                         [(True, None), (False, "recording_miss")])
        self.assertEqual(results[0]["answer"]["p_yes"], 0.9)
        # --recording needs the kind, and a cmd recording cannot be rebuilt.
        self.assertEqual(self.cli("jev", "shadow", str(self.vault), "--provider-kind",
                                  "recorded").returncode, 1)
        rows = [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            row["provider"] = {**row["provider"], "kind": "cmd", "program": "0" * 64}
        self.out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        done = self.cli("jev", "shadow", str(self.vault), "--provider-kind", "recorded",
                        "--recording", str(self.out))
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot be rebuilt", done.stderr)


@unittest.skipUnless(CONTRACTS_PRESENT and CLIENT_PRESENT, "needs the merged advisor modules")
class HookAutoContext(Case):
    """The prompt hook's `auto_context` feature: off unless enabled by name; shadow leaves
    the hook's output byte for byte; `on` (with a receipt covering topicality) appends the
    rescued note as a marked block when the gate passed; no time left or a kill switch means
    no call and one counter row; gate_skip is the only way to lose context."""

    def setUp(self):
        super().setUp()
        self.harbor()
        self.script = self.root / "judge.py"
        self.marker = self.root / "called"
        self.script.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            "q = json.loads(sys.stdin.read())\n"
            f"open({str(self.marker)!r}, 'a').write(q['template'].split('@')[0] + '\\n')\n"
            "time.sleep(float(os.environ.get('JUDGE_SLEEP', '0')))\n"
            "gate = float(os.environ.get('JUDGE_GATE', '0.9'))\n"
            "title = (q.get('state') or {}).get('title')\n"
            "p = gate if q['template'].startswith('topicality') else "
            "(0.9 if title == 'Mira Holt' else 0.2)\n"
            "print(json.dumps({'answers': {'q': {'type': 'noul', 'noul': p}}, "
            "'model': 'fake-judge-1'}))\n", encoding="utf-8")
        self.script.chmod(0o755)
        self.env = {"CONTEXT_LAYER_JEV_FAKE": str(self.script)}
        self.payload = json.dumps({"prompt": HARBOR_PROMPT, "session_id": "s1"})

    def hook(self, *extra, env=None, method="synaptic"):
        argv = ["hook", "claude-code", "--vault", str(self.vault)]
        if method == "synaptic":
            argv += ["--method", "synaptic"]
        done = self.cli(*argv, *extra, env=self.env if env is None else env, stdin=self.payload)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    @staticmethod
    def context(done):
        if not done.stdout.strip():
            return None
        return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]

    @staticmethod
    def normalised(text):
        return re.sub(r"\b[0-9a-f]{12}\b", "<nonce>", text or "")

    def calls(self):
        return self.marker.read_text(encoding="utf-8").split() if self.marker.exists() else []

    def rows(self):
        return [r for r in log_rows(self.vault) if r["feature"] == "auto_context"]

    def test_off_unless_enabled_and_shadow_changes_no_byte(self):
        plain = self.context(self.hook())
        self.assertIn('path="projects/Harbor Lights.md"', plain)
        self.assertNotIn('path="people/Mira Holt.md"', plain)
        configure(self.vault, "shadow", cache_ttl_s=0)     # search enabled, auto_context not
        self.assertEqual(self.normalised(self.context(self.hook())), self.normalised(plain))
        self.assertEqual((self.calls(), self.rows()), ([], []))
        configure(self.vault, "shadow", cache_ttl_s=0, features=["search", "auto_context"])
        shadow = self.hook()
        self.assertEqual(self.normalised(self.context(shadow)), self.normalised(plain))
        self.assertEqual(self.calls().count("topicality.v1"), 1)
        self.assertGreaterEqual(self.calls().count("relevance.v1"), 2)
        row = self.rows()[-1]
        self.assertEqual((row["mode"], row["applied"], row["gate_passed"], row["would_rescue"],
                          row["rescued"], row["skipped"]), ("shadow", False, True, 1, 0, 0))
        for method in ("fts",):                            # the fts hook too
            fts_plain = self.context(self.hook(method=method, env=CLEAN_ENV_WITHOUT_FAKE))
            self.assertEqual(self.normalised(self.context(self.hook(method=method))),
                             self.normalised(fts_plain))

    def test_on_appends_the_rescued_note_only_when_the_gate_passed(self):
        configure(self.vault, "on", cache_ttl_s=0, features=["search", "auto_context"], receipt=False)
        write_receipt(self.vault, purposes={"relevance": {"passed": True},
                                            "topicality": {"passed": True}})
        done = self.hook()
        text = self.context(done)
        blocks = [line for line in text.splitlines() if line.startswith("<<evidence")]
        self.assertEqual(len(blocks), 3)                  # two fts items, one rescued note
        self.assertIn('path="people/Mira Holt.md"', blocks[2])
        self.assertNotIn('path="people/Ansel Varga.md"', text)   # judged 0.2: not rescued
        self.assertIn("advisor p_yes 0.90 (fake); advisory, not a check of correctness", text)
        self.assertLess(text.index("Harbor Lights"), text.index("Mira Holt"))
        row = self.rows()[-1]
        self.assertEqual((row["mode"], row["applied"], row["gate_passed"], row["rescued"]),
                         ("on", True, True, 1))
        # Gate below threshold: nothing rescued, everything else unchanged, not skipped.
        plain = self.context(self.hook(env={**self.env, "JUDGE_GATE": "0.1"}))
        self.assertNotIn('path="people/Mira Holt.md"', plain)
        self.assertIn('path="projects/Harbor Lights.md"', plain)
        row = self.rows()[-1]
        self.assertEqual((row["gate_passed"], row["would_skip"], row["skipped"], row["rescued"]),
                         (False, 1, 0, 0))
        # A receipt without the topicality purpose: judged and counted, never applied.
        write_receipt(self.vault)
        self.assertNotIn('path="people/Mira Holt.md"', self.context(self.hook()))
        self.assertEqual((self.rows()[-1]["applied"], self.rows()[-1]["code"]),
                         (False, "calibration_required"))

    def test_gate_skip_is_the_only_way_to_lose_context(self):
        configure(self.vault, "on", cache_ttl_s=0, features=["search", "auto_context"], receipt=False,
                  lossy={"gate_skip": True, "prune_fts": False})
        write_receipt(self.vault, purposes={"relevance": {"passed": True},
                                            "topicality": {"passed": True}})
        done = self.hook(env={**self.env, "JUDGE_GATE": "0.1"})
        self.assertIsNone(self.context(done))
        self.assertIn("gate_skip", done.stderr)
        self.assertEqual((self.rows()[-1]["skipped"], self.rows()[-1]["applied"]), (1, True))
        # In shadow the same lever only counts.
        configure(self.vault, "shadow", cache_ttl_s=0, features=["search", "auto_context"],
                  lossy={"gate_skip": True, "prune_fts": False})
        self.assertIn('path="projects/Harbor Lights.md"',
                      self.context(self.hook(env={**self.env, "JUDGE_GATE": "0.1"})))
        self.assertEqual((self.rows()[-1]["would_skip"], self.rows()[-1]["skipped"]), (1, 0))

    def test_no_time_left_a_slow_judge_or_a_kill_switch_never_costs_the_hook(self):
        configure(self.vault, "shadow", cache_ttl_s=0, features=["search", "auto_context"], hook_timeout_s=0.5)
        plain_norm = self.normalised(self.context(self.hook(env=CLEAN_ENV_WITHOUT_FAKE)))
        self.assertEqual(self.normalised(self.context(self.hook())), plain_norm)
        self.assertEqual((self.calls(), self.rows()[-1]["code"]), ([], "skipped_deadline"))
        configure(self.vault, "shadow", cache_ttl_s=0, features=["search", "auto_context"], hook_timeout_s=1.5)
        slow = self.hook(env={**self.env, "JUDGE_SLEEP": "4"})
        self.assertEqual(self.normalised(self.context(slow)), plain_norm)
        self.assertIn(self.rows()[-1]["code"], ("deadline_exceeded", "answers_invalid"))
        (self.vault / ".context" / "jev.disabled").touch()
        self.marker.unlink(missing_ok=True)
        self.assertEqual(self.normalised(self.context(self.hook())), plain_norm)
        self.assertEqual((self.calls(), self.rows()[-1]["code"], self.rows()[-1]["mode"]),
                         ([], "kill_switch", "off"))


@unittest.skipUnless(CONTRACTS_PRESENT and CLIENT_PRESENT, "needs the merged advisor modules")
class McpAdvisor(Case):
    """Over MCP: `search_vault` with `jev: true` runs the advisor only when the vault owner
    enabled it; `jev_status` is read-only and sends nothing."""

    def setUp(self):
        super().setUp()
        self.harbor()
        self.script = self.root / "judge.py"
        self.script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "q = json.loads(sys.stdin.read())\n"
            "p = 0.9 if (q.get('state') or {}).get('title') == 'Mira Holt' else 0.2\n"
            "print(json.dumps({'answers': {'q': {'type': 'noul', 'noul': p}}, "
            "'model': 'fake-judge-1'}))\n", encoding="utf-8")
        self.script.chmod(0o755)

    def server(self, env=None):
        log = open(self.root / "server.err", "w", encoding="utf-8")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, "-m", "context_layer.cli", "mcp", "--vault",
                                 str(self.vault)], cwd=REPO, text=True, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=log,
                                env={**CLEAN_ENV, "HOME": str(self.home),
                                     "CONTEXT_LAYER_JEV_FAKE": str(self.script), **(env or {})})
        self.addCleanup(self.stop, proc)
        self.send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        self.assertIn("result", json.loads(proc.stdout.readline()))
        self.send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        return proc

    @staticmethod
    def stop(proc):
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
        finally:
            if proc.stdout:
                proc.stdout.close()

    @staticmethod
    def send(proc, payload):
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def tool(self, proc, ident, name, arguments):
        self.send(proc, {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})
        response = json.loads(proc.stdout.readline())
        self.assertIn("result", response, response)
        result = response["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def test_search_vault_jev_and_jev_status(self):
        proc = self.server()
        failed, plain = self.tool(proc, 2, "search_vault",
                                  {"prompt": HARBOR_PROMPT, "method": "synaptic", "jev": True})
        self.assertFalse(failed)
        self.assertNotIn("jev", plain)                    # not configured: the plain packet
        self.assertEqual(log_rows(self.vault), [])
        failed, status = self.tool(proc, 3, "jev_status", {})
        self.assertFalse(failed)
        self.assertEqual((status["schema"], status["configured"], status["mode"]),
                         ("jev-status/v1", False, "off"))
        configure(self.vault, "shadow", cache_ttl_s=0)
        failed, shadow = self.tool(proc, 4, "search_vault",
                                   {"prompt": HARBOR_PROMPT, "method": "synaptic", "jev": True})
        self.assertFalse(failed)
        self.assertEqual(without(shadow, "jev"), plain)   # shadow: the packet unchanged
        self.assertEqual((shadow["jev"]["mode"], shadow["jev"]["applied"],
                          shadow["jev"]["counts"]["would_rescue"]), ("shadow", False, 1))
        failed, without_flag = self.tool(proc, 5, "search_vault",
                                         {"prompt": HARBOR_PROMPT, "method": "synaptic"})
        self.assertFalse(failed)
        self.assertNotIn("jev", without_flag)             # the flag is opt-in per call
        configure(self.vault, "on", cache_ttl_s=0)        # with a receipt for the fake kind
        failed, on = self.tool(proc, 6, "search_vault",
                               {"prompt": HARBOR_PROMPT, "method": "synaptic", "jev": True})
        self.assertFalse(failed)
        self.assertEqual(on["evidence"][:len(plain["evidence"])], plain["evidence"])
        rescued = [e for e in on["evidence"] if e.get("origin") == "jev"]
        self.assertEqual([e["source_path"] for e in rescued], ["people/Mira Holt.md"])
        self.assertTrue(on["jev"]["applied"])
        failed, status = self.tool(proc, 7, "jev_status", {})
        self.assertEqual((status["mode"], status["receipt"]["usable"]), ("on", True))
        # A non-boolean `jev` is invalid params (a JSON-RPC error on this revision).
        self.send(proc, {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
            "name": "search_vault", "arguments": {"prompt": HARBOR_PROMPT, "jev": "yes"}}})
        response = json.loads(proc.stdout.readline())
        self.assertEqual(response.get("error", {}).get("code"), -32602, response)
        self.assertEqual(len([r for r in log_rows(self.vault) if r["feature"] == "search"]), 2)


class ConfigurationAndCommands(Case):

    def setUp(self):
        super().setUp()
        self.harbor()
        self.config = self.vault / ".context" / "jev.json"

    def test_unconfigured_status_lists_four_kinds_and_writes_nothing(self):
        done = self.cli("jev", "status", str(self.vault))
        self.assertEqual(done.returncode, 0)
        for kind in jev.USER_PROVIDER_KINDS:
            self.assertIn(f"  {kind} ", done.stdout)
        self.assertIn("docs/jev.md", done.stdout)
        off = self.cli("jev", "off", str(self.vault))
        self.assertEqual(off.returncode, 0)
        self.assertFalse([p for p in (self.vault / ".context").iterdir()
                          if p.name.startswith("jev")])

    def test_first_shadow_names_a_provider_and_writes_a_private_file(self):
        done = self.cli("jev", "shadow", str(self.vault))
        self.assertEqual(done.returncode, 1)
        self.assertIn("no default provider", done.stderr)
        self.assertFalse(self.config.exists())
        done = self.cli("jev", "shadow", str(self.vault), "--provider-kind", "systemone",
                        "--base-url", "https://judge.example", "--model", "jev-1.13.0",
                        "--key-env", "TYPESAFE_API_KEY")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        saved = json.loads(self.config.read_text())
        self.assertEqual((saved["mode"], saved["features"]), ("shadow", ["search", "answer",
                                                                         "memory"]))
        self.assertEqual(saved["provider"]["key_env"], "TYPESAFE_API_KEY")
        again = self.cli("jev", "shadow", str(self.vault))       # the provider is reused
        self.assertEqual(again.returncode, 0)
        self.assertIn("changed: no", again.stdout)

    def test_hand_added_known_keys_survive_a_mode_command(self):
        self.cli("jev", "shadow", str(self.vault), "--provider-kind", "fake")
        saved = json.loads(self.config.read_text())
        saved.update({"excerpt_chars": 500, "local_only_prefixes": ["diary"]})
        self.config.write_text(json.dumps(saved))
        self.assertEqual(self.cli("jev", "off", str(self.vault)).returncode, 0)
        saved = json.loads(self.config.read_text())
        self.assertEqual((saved["mode"], saved["excerpt_chars"], saved["local_only_prefixes"]),
                         ("off", 500, ["diary"]))
        self.assertEqual(saved["provider"], {"kind": "fake"})

    def test_auto_context_is_never_enabled_by_a_mode_command_alone(self):
        self.cli("jev", "shadow", str(self.vault), "--provider-kind", "fake")
        write_receipt(self.vault)
        self.assertEqual(self.cli("jev", "on", str(self.vault)).returncode, 0)
        self.assertNotIn("auto_context", json.loads(self.config.read_text())["features"])
        done = self.cli("jev", "shadow", str(self.vault), "--enable", "auto_context",
                        "--disable", "memory")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(self.config.read_text())["features"],
                         ["search", "auto_context", "answer"])
        self.assertEqual(self.cli("jev", "shadow", str(self.vault), "--enable",
                                  "telepathy").returncode, 1)
        self.assertEqual(self.cli("jev", "shadow", str(self.vault), "--enable", "search",
                                  "--disable", "search").returncode, 2)
        # Enabled by name, the hook feature is wired in this version: status says so.
        self.assertIn("automatic_model_calls: yes", self.cli("jev", "status",
                                                             str(self.vault)).stdout)
        self.cli("jev", "shadow", str(self.vault), "--disable", "auto_context")
        self.assertIn("automatic_model_calls: no", self.cli("jev", "status",
                                                            str(self.vault)).stdout)

    def test_on_needs_a_matching_receipt(self):
        self.cli("jev", "shadow", str(self.vault), "--provider-kind", "fake")
        before = self.config.read_bytes()
        done = self.cli("jev", "on", str(self.vault))
        self.assertEqual(done.returncode, 1)
        self.assertIn("calibration_required", done.stderr)
        self.assertEqual(self.config.read_bytes(), before)
        for changes in ({"provider": {"kind": "fake", "model": "other"}},
                        {"purposes": {"relevance": {"passed": False}}},
                        {"thresholds": {**jev.DEFAULT_THRESHOLDS, "rescue": 0.9}},
                        {"schema": "jev-calibration/v0"}):
            with self.subTest(changes=changes):
                write_receipt(self.vault, **changes)
                self.assertEqual(self.cli("jev", "on", str(self.vault)).returncode, 1)
        write_receipt(self.vault)
        self.assertEqual(self.cli("jev", "on", str(self.vault)).returncode, 0)
        self.assertEqual(json.loads(self.config.read_text())["mode"], "on")

    def test_invalid_files_fail_closed_and_are_left_untouched(self):
        base = {"schema_version": 1, "mode": "shadow", "provider": {"kind": "fake"}}
        outside = self.root / "keys.env"
        inside = self.vault / "keys.env"
        bad = {
            "unknown key": {**base, "color": "blue"},
            "newer schema": {**base, "schema_version": 2},
            "timeout cap": {**base, "timeout_s": 11},
            "candidate cap": {**base, "max_candidates": 33},
            "budget cap": {**base, "jev_extra_tokens": 2001},
            "boolean count": {**base, "max_requests": True},
            "env_file inside": {**base, "env_file": str(inside)},
            "env_file relative": {**base, "env_file": "keys.env"},
            "credential": {**base, "provider": {"kind": "fake"},
                           "blocklist_file": None, "local_only_prefixes": [
                               "sk-abcdefghijklmnopqrstuvwx"]},
            "localhost": {**base, "provider": {"kind": "openai_compat",
                                               "base_url": "http://localhost:8080",
                                               "model": "small"}},
            "systemone without key_env": {**base, "provider": {
                "kind": "systemone", "base_url": "https://judge.example", "model": "m"}},
            "mode without provider": {"schema_version": 1, "mode": "on"},
        }
        outside.write_text("TYPESAFE_API_KEY=x\n")
        for label, obj in bad.items():
            with self.subTest(label=label):
                self.config.write_text(json.dumps(obj))
                before = self.config.read_bytes()
                status = self.cli("jev", "status", str(self.vault))
                self.assertEqual(status.returncode, 1)
                self.assertIn("invalid", status.stdout)
                self.assertEqual(self.cli("jev", "shadow", str(self.vault)).returncode, 1)
                self.assertEqual(self.config.read_bytes(), before)
                plain = self.cli("search", str(self.vault), "--prompt", HARBOR_PROMPT)
                asked = self.cli("search", str(self.vault), "--prompt", HARBOR_PROMPT, "--jev")
                self.assertEqual(asked.stdout, plain.stdout)
        self.config.write_text('{"schema_version": 1, "mode": "off", "mode": "on"}')
        self.assertFalse(jev.load_config(self.vault).valid)
        self.config.write_text(json.dumps({**base, "note": "x" * 17000}))
        self.assertIn("larger than", jev.load_config(self.vault).problem)
        self.config.unlink()
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps(base))
        self.config.symlink_to(target)
        self.assertIn("symlink", jev.load_config(self.vault).problem)
        self.config.unlink()
        self.assertTrue(jev.validate({**base, "env_file": str(outside)}, self.vault)[1] is None)
        self.assertFalse(log_rows(self.vault))

    def test_provider_blocks_follow_the_keys_each_kind_takes(self):
        base = {"schema_version": 1, "mode": "shadow"}
        good = [{"kind": "fake"}, {"kind": "fake", "label_only": True, "rounding": "2dp"},
                {"kind": "systemone", "base_url": "https://judge.example", "model": "jev-1.13.0",
                 "key_env": "TYPESAFE_API_KEY", "rounding": "2dp"},
                {"kind": "systemone", "base_url": "http://127.0.0.1:8765", "model": "laya",
                 "key_env": "LAYA_API_KEY", "profile": "laya"},
                {"kind": "openai_compat", "base_url": "http://127.0.0.1:11434", "model": "small",
                 "api_key_env": "LOCAL_KEY"},
                {"kind": "host_cli", "model": "haiku", "max_budget_usd": 0.05},
                {"kind": "cmd", "argv": ["/opt/judge", "{questionnaire_file}"]},
                {"kind": "recorded", "recording": "/opt/rec.jsonl", "replays": {"kind": "fake"}}]
        bad = [{"kind": "fake", "key_env": "X_KEY"}, {"kind": "host_cli"},
               {"kind": "host_cli", "model": "haiku", "host_context": "bare"},
               {"kind": "host_cli", "model": "haiku", "max_budget_usd": 5},
               {"kind": "systemone", "base_url": "https://judge.example", "model": "m",
                "key_env": "K_KEY", "profile": "2dp"},
               {"kind": "cmd", "argv": ["/opt/judge"]},
               {"kind": "recorded", "recording": "/opt/rec.jsonl"},
               {"kind": "recorded", "recording": "/opt/rec.jsonl",
                "replays": {"kind": "recorded", "recording": "/opt/other.jsonl"}},
               {"kind": "openai_compat", "base_url": "https://judge.example", "model": "m"}]
        for provider in good:
            self.assertIsNone(jev.validate({**base, "provider": provider}, self.vault)[1],
                              provider)
        for provider in bad:
            self.assertIsNotNone(jev.validate({**base, "provider": provider}, self.vault)[1],
                                 provider)
        done = self.cli("jev", "shadow", str(self.vault), "--provider-kind", "host_cli")
        self.assertEqual(done.returncode, 1)
        self.assertIn("needs --model", done.stderr)

    def test_endpoint_rules(self):
        accepted = ["https://judge.example", "https://judge.example:8443/api",
                    "http://127.0.0.1:8765", "http://[::1]:8080/v1"]
        refused = ["http://localhost:8080", "http://judge.example", "https://u:p@judge.example",
                   "https://judge.example/?q=1", "https://judge.example/#top",
                   "https://judge.example:99999", "ftp://judge.example", "https://",
                   "https://judge .example"]
        for url in accepted:
            self.assertIsNone(jev.endpoint_problem(url), url)
        for url in refused:
            self.assertIsNotNone(jev.endpoint_problem(url), url)

    def test_kill_switch_status_and_restore(self):
        self.cli("jev", "shadow", str(self.vault), "--provider-kind", "fake")
        flag = self.vault / ".context" / "jev.disabled"
        flag.write_text("")
        info = json.loads(self.cli("jev", "status", str(self.vault), "--json").stdout)
        self.assertEqual((info["mode"], info["saved_mode"], info["kill_switch"]),
                         ("off", "shadow", True))
        flag.unlink()
        info = json.loads(self.cli("jev", "status", str(self.vault), "--json",
                                   env={"CONTEXT_LAYER_JEV_DISABLE": "1"}).stdout)
        self.assertEqual((info["mode"], info["kill_switch"]), ("off", True))
        info = json.loads(self.cli("jev", "status", str(self.vault), "--json").stdout)
        self.assertEqual((info["mode"], info["kill_switch"]), ("shadow", False))

    def test_status_line_in_context_layer_status(self):
        done = self.cli("status", str(self.vault))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.splitlines()[-1],
                         "jev: off (not configured); automatic_model_calls: no")
        before = json.loads(self.cli("status", str(self.vault), "--json").stdout)
        self.cli("jev", "shadow", str(self.vault), "--provider-kind", "fake")
        self.assertEqual(self.cli("status", str(self.vault)).stdout.splitlines()[-1],
                         "jev: shadow (saved mode shadow); automatic_model_calls: no")
        after = json.loads(self.cli("status", str(self.vault), "--json").stdout)
        self.assertEqual(sorted(after), sorted(before))            # the JSON keeps its keys

    def test_status_never_prints_a_key_or_an_absolute_path(self):
        self.cli("jev", "shadow", str(self.vault), "--provider-kind", "systemone", "--base-url",
                 "https://judge.example", "--model", "jev-1.13.0", "--key-env",
                 "TYPESAFE_API_KEY")
        for argv in (["jev", "status", str(self.vault)], ["jev", "status", str(self.vault),
                                                          "--json", "--check"]):
            done = self.cli(*argv, env={"TYPESAFE_API_KEY": MARKER})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn(MARKER, done.stdout + done.stderr)
            self.assertNotIn(str(self.root), done.stdout)

    def test_purge_is_a_dry_run_until_apply_and_keeps_config_and_receipts(self):
        configure(self.vault, "on", cache_ttl_s=3600)
        advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(lambda q: answer(0.9)))
        (self.vault / ".context" / "jev.disabled").write_text("")
        context = self.vault / ".context"
        names = {p.name for p in context.iterdir() if p.name.startswith("jev")}
        self.assertEqual(names, {"jev.json", "jev.salt", "jev-cache", "jev-calls.jsonl",
                                 "jev-calibration", "jev.disabled"})
        dry = self.cli("jev", "purge", str(self.vault))
        self.assertEqual(dry.returncode, 0)
        self.assertIn("would remove: .context/jev-cache/", dry.stdout)
        self.assertEqual({p.name for p in context.iterdir() if p.name.startswith("jev")}, names)
        self.assertEqual(self.cli("jev", "purge", str(self.vault), "--apply").returncode, 0)
        self.assertEqual({p.name for p in context.iterdir() if p.name.startswith("jev")},
                         {"jev.json", "jev-calls.jsonl", "jev-calibration", "jev.disabled"})
        self.cli("jev", "purge", str(self.vault), "--apply", "--all")
        self.assertEqual({p.name for p in context.iterdir() if p.name.startswith("jev")},
                         {"jev.json", "jev-calibration", "jev.disabled"})
        done = self.cli("jev", "purge", str(self.vault), "--apply", "--receipts")
        self.assertIn("removed: .context/jev-calibration/", done.stdout)
        self.assertEqual({p.name for p in context.iterdir() if p.name.startswith("jev")},
                         {"jev.json", "jev.disabled"})

    def test_usage_errors_exit_two(self):
        self.assertEqual(self.cli("jev", "status", str(self.vault), "--enable",
                                  "search").returncode, 2)
        self.assertEqual(self.cli("jev", "status", str(self.root / "absent")).returncode, 2)
        self.assertEqual(self.cli("jev", "report", str(self.vault), "--days", "0").returncode, 2)
        self.assertEqual(self.cli("jev").returncode, 2)
        self.assertEqual(self.cli("jev", "stat", str(self.vault)).returncode, 2)


# ---------------------------------------------------------------------------
# Cache, call log, report
# ---------------------------------------------------------------------------

class CacheLogReport(Case):

    def setUp(self):
        super().setUp()
        self.harbor()

    def test_second_identical_search_is_answered_from_the_cache(self):
        configure(self.vault, "shadow", cache_ttl_s=3600)
        provider = Provider(lambda q: answer(0.9))
        first, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, provider)
        asked = len(provider.seen)
        second, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, provider)
        self.assertEqual((provider.calls, len(provider.seen)), (1, asked))
        self.assertEqual(second["jev"]["counts"]["cache_hits"], asked)
        self.assertEqual([r["cache_hit"] for r in log_rows(self.vault)], [False, True])
        self.assertEqual(without(first, "jev"), without(second, "jev"))
        path = self.vault / "people" / "Mira Holt.md"
        path.write_text(path.read_text() + "\nA new line.\n")
        self.cli("index", str(self.vault))
        advise(self.vault, "synaptic", HARBOR_PROMPT, provider)
        self.assertEqual(provider.calls, 2)                      # a new pin, a new key

    def test_off_never_touches_the_cache(self):
        configure(self.vault, "shadow")
        jev.write_config(self.vault, {"schema_version": 1, "mode": "off",
                                      "provider": {"kind": "fake"}})
        done = self.cli("search", str(self.vault), "--prompt", HARBOR_PROMPT, "--jev")
        self.assertEqual(done.returncode, 0)
        self.assertFalse((self.vault / ".context" / "jev-cache").exists())
        self.assertFalse(log_rows(self.vault))

    def test_symlinked_cache_folder_is_not_followed(self):
        configure(self.vault, "shadow", cache_ttl_s=3600)
        outside = self.root / "outside-cache"
        outside.mkdir()
        (self.vault / ".context" / "jev-cache").symlink_to(outside)
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(lambda q: answer(0.9)))
        self.assertFalse(out["jev"]["degraded"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_log_rows_hold_only_counters_and_stay_bounded(self):
        configure(self.vault, "shadow")
        advise(self.vault, "synaptic", HARBOR_PROMPT, Provider(lambda q: answer(0.9)))
        for row in log_rows(self.vault):
            self.assertEqual(tuple(row), jev.LOG_FIELDS)
            for value in row.values():
                self.assertTrue(value is None or isinstance(value, (bool, int, float))
                                or re.fullmatch(r"[a-z0-9_.:-]{1,64}", value), value)
        jev.append_log(self.vault, {"feature": "Free text with Spaces", "code": "x" * 80,
                                    "latency_ms": float("inf"), "requests": -1,
                                    "model_id": "fine-model:1"})
        last = log_rows(self.vault)[-1]
        self.assertEqual((last["feature"], last["code"], last["latency_ms"], last["requests"],
                          last["model_id"]), (None, None, None, None, "fine-model:1"))
        for number in range(1600):
            jev.append_log(self.vault, jev._row("search", "shadow", requests=number, judged=3))
        log = self.vault / ".context" / "jev-calls.jsonl"
        self.assertLessEqual(log.stat().st_size, jev.LOG_MAX_BYTES)
        rows = log_rows(self.vault)
        self.assertEqual(rows[-1]["requests"], 1599)
        self.assertGreater(rows[0]["requests"], 0)               # the older half was dropped
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_hand_edited_rows_carry_no_text_into_the_report(self):
        log = self.vault / ".context" / "jev-calls.jsonl"
        log.write_text(json.dumps({"v": 1, "at": 1e10, "feature": "search", "mode": "shadow",
                                   "degraded": True, "code": "see the Harbour Archive shelf"}) + "\n")
        info = jev.report(self.vault, 36500)
        self.assertEqual(info["features"]["search"]["degraded"], {"unknown": 1})
        self.assertNotIn("Archive", json.dumps(info))

    def test_an_oversized_log_recovers_on_the_next_row(self):
        log = self.vault / ".context" / "jev-calls.jsonl"
        row = json.dumps(jev._row("search", "shadow", requests=1)) + "\n"
        log.write_text(row * (3 * jev.LOG_MAX_BYTES // len(row)))
        self.assertGreater(log.stat().st_size, 2 * jev.LOG_MAX_BYTES)
        jev.append_log(self.vault, jev._row("search", "shadow", requests=7))
        self.assertLessEqual(log.stat().st_size, jev.LOG_MAX_BYTES)
        rows = log_rows(self.vault)
        self.assertEqual(rows[-1]["requests"], 7)
        self.assertTrue(all(r["v"] == 1 for r in rows))       # only whole rows were kept

    def test_symlinked_log_is_not_written_through(self):
        target = self.root / "elsewhere.jsonl"
        target.write_text("")
        (self.vault / ".context" / "jev-calls.jsonl").symlink_to(target)
        jev.append_log(self.vault, jev._row("search", "shadow"))
        self.assertEqual(target.read_text(), "")

    def test_report_summarises_counters(self):
        rows = [dict(mode="shadow", latency_ms=100, would_rescue=1, requests=3, input_tokens=300),
                dict(mode="shadow", latency_ms=300, would_rescue=0, requests=3, input_tokens=300,
                     cache_hit=True),
                dict(mode="shadow", latency_ms=200, degraded=True, code="deadline_exceeded"),
                dict(mode="on", latency_ms=400, applied=True, rescued=2, cost_usd=0.0001),
                dict(mode="off", code="kill_switch")]
        for row in rows:
            jev.append_log(self.vault, jev._row("search", **row))
        info = jev.report(self.vault, 7)["features"]["search"]
        self.assertEqual(info["calls"], 4)
        self.assertEqual(info["applied_share"], 0.25)
        self.assertEqual(info["degraded"], {"deadline_exceeded": 1})
        self.assertEqual(info["cache_hit_share"], 0.25)
        self.assertEqual((info["latency_ms_p50"], info["latency_ms_p95"]), (200, 400))
        self.assertEqual((info["shadow_calls"], info["shadow_change_rate"]), (2, 0.5))
        self.assertEqual((info["rescued"], info["not_called"]), (2, {"kill_switch": 1}))
        done = self.cli("jev", "report", str(self.vault))
        self.assertEqual(done.returncode, 0)
        self.assertIn("shadow_change_rate: 0.5", done.stdout)
        self.assertNotIn(str(self.root), done.stdout)
        log = self.vault / ".context" / "jev-calls.jsonl"
        log.unlink()
        log.symlink_to(self.root / "nowhere")
        self.assertEqual(self.cli("jev", "report", str(self.vault)).returncode, 1)


# ---------------------------------------------------------------------------
# Side channel, question shape, memory id
# ---------------------------------------------------------------------------

class SideChannelAndShape(Case):

    def test_bm25_tail_carries_the_fts_prefix(self):
        files = {f"notes/pier-{n}.md": f"# Pier {n}\n\nThe pier lamp log for week {n}.\n"
                 + ("pier " * n) for n in range(1, 6)}
        build_vault(self.vault, files, self.home)
        _, off = run_retrieve(self.vault, "fts", "pier lamp log", ["--top-k", "2"])
        _, side = run_retrieve(self.vault, "fts", "pier lamp log", ["--top-k", "2"], candidates=2)
        self.assertEqual(without(side, "jev_candidates"), off)
        items = side["jev_candidates"]["items"]
        self.assertEqual([c["kind"] for c in items], ["bm25_tail", "bm25_tail"])
        self.assertEqual([c["rank"] for c in items], [3, 4])
        delivered = {e["source_path"] for e in off["evidence"]}
        for cand in items:
            self.assertNotIn(cand["source_path"], delivered)
            self.assert_byte_exact(self.vault, cand["passages"][0])
            self.assertEqual(cand["passages"][0]["start"], 0)

    def test_side_channel_bounds_and_methods(self):
        self.harbor()
        code, packet = run_retrieve(self.vault, "fts", HARBOR_PROMPT, candidates=33)
        self.assertEqual((code, packet["status"]), (1, "ERROR"))
        # F2-07: the side channel is refused where it does not exist, not dropped silently.
        with redirect_stderr(io.StringIO()) as refused, self.assertRaises(SystemExit) as cm:
            run_retrieve(self.vault, "synaptic", HARBOR_PROMPT, ["--compact"], candidates=5)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--jev-candidates applies to --method fts and the default synaptic",
                      refused.getvalue())
        _, plain = run_retrieve(self.vault, "synaptic", HARBOR_PROMPT)
        self.assertNotIn("jev_candidates", plain)
        self.assertNotIn("jev_candidates", plain["synapse"])
        configure(self.vault, "shadow")
        for method, flags in (("synaptic", ["--compact"]), ("grep", [])):
            with redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(jev.search_plan(self.vault, method, flags, environ={}))
            self.assertIn("fts and the default synaptic mode only", err.getvalue())

    def test_questions_carry_bounded_views_with_file_names_only(self):
        long_prompt = HARBOR_PROMPT + " " + "and more " * 400
        self.harbor({"people/Mira Holt.md": "# Mira Holt\n\n" + "Mira looks over lamps. " * 80})
        configure(self.vault, "shadow", excerpt_chars=120, cache_ttl_s=0)
        provider = Provider(lambda q: answer(0.5))
        advise(self.vault, "synaptic", long_prompt, provider)
        self.assertTrue(provider.seen)
        for questionnaire in provider.seen:
            state = questionnaire["state"]
            self.assertEqual(set(state), {"request", "title", "link_line", "excerpt"})
            self.assertLessEqual(len(state["request"]), jev.REQUEST_CHARS)
            self.assertLessEqual(len(state["excerpt"]), 120)
            self.assertLessEqual(len(state["link_line"]), jev.LINK_LINE_CHARS)
            self.assertLessEqual(len(state["title"]), jev.STEM_CHARS)
            self.assertNotIn("/", state["title"])
        names = sorted({name_of(q) for q in provider.seen})
        self.assertEqual(names, ["Ansel Varga", "Harbor Lights", "Launch Plan", "Mira Holt"])

    def test_request_cap_and_size_gate(self):
        self.harbor()
        configure(self.vault, "shadow", max_requests=2, cache_ttl_s=0)
        provider = Provider(lambda q: answer(0.5))
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, provider)
        self.assertEqual(len(provider.seen), 2)
        self.assertIn("request_cap", out["jev"]["codes"])
        configure(self.vault, "shadow", max_input_chars=100, cache_ttl_s=0)
        provider = Provider(lambda q: answer(0.5))
        out, _ = advise(self.vault, "synaptic", HARBOR_PROMPT, provider)
        self.assertEqual(provider.calls, 0)
        self.assertIn("budget_exceeded", out["jev"]["codes"])

    def test_secret_patterns(self):
        hits = ["-----BEGIN OPENSSH PRIVATE KEY-----", "AKIAABCDEFGHIJKLMNOP",
                "ghp_" + "a1" * 18, "github_pat_" + "b2" * 12, "sk-proj-abcdefghijklmnop1234",
                "Authorization: Bearer abcdefghijklmnop0123", "db_password = hunter2hunter2x",
                "https://user:pass@example.org/x", "xoxb-1234567890-abcdef",
                # F2-42: a bare token in prose, not only after `Bearer` or `token:`
                "The session used eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmaWN0aW9uYWwifQ.c2lnbmF0dXJlMTIz today"]
        misses = ["the task-management overview", "tokens: 350", "password: see the manager",
                  "https://example.org/@user", "sk-learn", "keys named in key_env",
                  "eyJ is the base64 of an opening brace and a quote", "a.b.c and version 1.2.3"]
        for text in hits:
            self.assertIsNotNone(jev.secret_hit({"state": {"excerpt": text}}), text)
        for text in misses:
            self.assertIsNone(jev.secret_hit({"state": {"excerpt": text}}), text)
        self.assertEqual(jev.secret_hit({"x": "the Winter Storm Log"}, ("winter storm log",)),
                         "blocklist")

    def test_public_record_id_matches_the_stored_record(self):
        self.harbor()
        stored = memory.record(self.vault, kind="decision", text="Lamps are checked weekly.",
                               sources=[{"path": "people/Mira Holt.md"}])
        sources = [{"path": s["path"], "sha256": s["sha256"]} for s in stored["sources"]]
        self.assertEqual(memory.record_id("decision", "Lamps are checked weekly.", sources, None),
                         stored["id"])


if __name__ == "__main__":
    unittest.main()
