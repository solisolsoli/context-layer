"""The advisor's development set and its evaluator (development aids, not a benchmark).

Fast, always: the generator is deterministic and self-consistent, its sizes are the
pre-registered ones, its hash is the version's, and no key-shaped literal sits in its
source. The recording chain (oracle capture, `jev record`, replay, `jev calibrate`,
`jev on`) runs only with JEV_DEV_E2E=1: it drives the CLI a few hundred times and takes
about two minutes. Run: python3 tests/test_dev_jev.py
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from context_layer import jev  # noqa: E402
from fixtures import dev_jev  # noqa: E402

# The hash of the set this version ships; a changed note or label is a new version.
DEV_SET_SHA256 = "1d2ab7b28fda7f2dc2bc64b4c7a72d150a06d7a493e7c89cc10224341407e1fa"
SIZES = {"bridges": 16, "unanswerable": 8, "privacy_prompts": 1, "word_sharing": 8,
         "bm25_tail": 8, "injection": 4, "gate_topical": 6, "gate_not_topical": 6,
         "claims": {"supported": 4, "contradicted": 4, "silent": 4, "cancelled_plan": 4},
         "memory_proposals": 10, "memory_priors": 8, "privacy_traps": 8}


class DevSet(unittest.TestCase):

    def test_generator_is_deterministic_self_checked_and_pinned(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = dev_jev.build(Path(a) / "vault")
            second = dev_jev.build(Path(b) / "vault")
            self.assertEqual(dev_jev.canonical(first), dev_jev.canonical(second))
            texts = {path: (Path(a) / "vault" / path).read_text(encoding="utf-8")
                     for path, _ in first["vault"]["manifest"]}
        self.assertEqual(dev_jev.check(texts, first), [])
        self.assertEqual(dev_jev.dev_set_sha256(first), DEV_SET_SHA256)
        self.assertEqual((first["schema"], first["version"]), ("jev-dev-set/v1", 1))
        counts = dev_jev.counts(first)
        for key, expected in SIZES.items():
            self.assertEqual(counts[key], expected, key)
        self.assertGreaterEqual(counts["tempting"], 8)

    def test_no_key_shaped_literal_in_the_generator(self):
        source = (REPO / "tests" / "fixtures" / "dev_jev.py").read_text(encoding="utf-8")
        # The literal key shapes (a PEM block, cloud, GitHub, sk- and Slack tokens) must not
        # occur in the source: the fake secrets are built at run time from a hash. The
        # broader assignment heuristic is not applied to code that names such variables.
        literal = {"private_key", "cloud_key_id", "cloud_api_key", "github_token", "sk_key",
                   "slack_token"}
        for name, pattern in jev.SECRET_PATTERNS:
            if name in literal:
                self.assertIsNone(pattern.search(source), name)
        with tempfile.TemporaryDirectory() as root:
            data = dev_jev.build(Path(root) / "vault")
            traps = [trap for trap in data["privacy"] if trap["path"]]
            self.assertTrue(traps)
            for trap in traps:                       # the traps hold what the scan stops
                text = (Path(root) / "vault" / trap["path"]).read_text(encoding="utf-8")
                self.assertTrue(jev.local_only_reason(text) or jev.secret_hit(text), trap["id"])


@unittest.skipUnless(os.environ.get("JEV_DEV_E2E") == "1",
                     "set JEV_DEV_E2E=1 to run the recording chain (about two minutes)")
class RecordingChain(unittest.TestCase):
    """Oracle capture -> `jev record` -> replayed evaluation -> `jev calibrate` -> `jev on`."""

    def test_the_oracle_recording_calibrates_and_on_rescues_the_bridge(self):
        import jev_dev_eval
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "home").mkdir()
            env = {k: v for k, v in os.environ.items() if not k.startswith("CONTEXT_LAYER_JEV")}
            env.update({"HOME": str(root / "home"), "PYTHONDONTWRITEBYTECODE": "1"})

            def run(*argv, extra=None):
                return subprocess.run([sys.executable, *argv], cwd=REPO, capture_output=True,
                                      text=True, env={**env, **(extra or {})}, timeout=900)

            questions = root / "questions.jsonl"
            done = run("tests/jev_dev_eval.py", "--provider", "oracle", "--json",
                       "--dump-questions", str(questions))
            self.assertEqual(done.returncode, 0, done.stderr)
            vault = root / "vault"
            data = dev_jev.build(vault)
            labels = root / "labels.json"
            labels.write_text(dev_jev.canonical(data), encoding="utf-8")
            wrapper = jev_dev_eval.write_provider(root / "provider", "oracle", None, labels)
            run("-m", "context_layer.cli", "init", str(vault))
            self.assertEqual(run("-m", "context_layer.cli", "index", str(vault)).returncode, 0)
            self.assertEqual(run("-m", "context_layer.cli", "jev", "shadow", str(vault),
                                 "--provider-kind", "fake").returncode, 0)
            recording = root / "recording.jsonl"
            done = run("-m", "context_layer.cli", "jev", "record", str(vault), "--questions",
                       str(questions), "--out", str(recording), "--run", "--max-requests", "64",
                       "--max-parallel", "8", extra={"CONTEXT_LAYER_JEV_FAKE": str(wrapper)})
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            report = root / "report.json"
            done = run("tests/jev_dev_eval.py", "--provider", f"recorded:{recording}", "--json")
            self.assertEqual(done.returncode, 0, done.stderr)
            report.write_text(done.stdout, encoding="utf-8")
            done = run("-m", "context_layer.cli", "jev", "calibrate", str(vault), "--report",
                       str(report), "--recording", str(recording), "--apply")
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            self.assertEqual(run("-m", "context_layer.cli", "jev", "on", str(vault)).returncode, 0)
            case = data["relevance"][0]
            done = run("-m", "context_layer.cli", "search", str(vault), "--prompt",
                       case["question"], "--method", "synaptic", "--jev",
                       extra={"CONTEXT_LAYER_JEV_FAKE": str(wrapper)})
            packet = json.loads(done.stdout)
            self.assertTrue(packet["jev"]["applied"])
            rescued = [e["source_path"] for e in packet["evidence"] if e.get("origin") == "jev"]
            self.assertTrue(set(case["must_rescue"]) <= set(rescued), (case["must_rescue"], rescued))


if __name__ == "__main__":
    unittest.main()
