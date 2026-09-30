"""Lean orchestration: shared packets, job contracts, payload budgets, handback checks.

Every test builds its own small fictional vault in a temp directory. Nothing
here reads a real vault, contacts a host or calls a model.
Run: python3 tests/test_orchestrate.py
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
import unicodedata
import unittest

from _portable_helpers import assert_private_path, isolated_home_env

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))

from context_layer import mcp_server, orchestrate as orch  # noqa: E402

FORM_FEED, LINE_SEP, PARA_SEP, NEXT_LINE = chr(12), chr(0x2028), chr(0x2029), chr(0x85)

ROUTES = {"record_type_allowlist": ["verbatim_text_file"],
          "routes": {"notes": {"priority": 1, "triggers": ["note"], "canonical_sources": [],
                               "path_hints": []}},
          "fallback_routes": [], "aliases": {}, "exclude_prefixes": ["private"]}

NOTES = {
    "projects/beacon-retrofit.md": (
        "# Beacon retrofit\n\n"
        "The beacon retrofit replaces the lamp assembly on the north pier.\n"
        "Budget is set in [[decisions/retrofit-budget]]; the supplier is [[suppliers/lumen-works]].\n\n"
        "## Schedule\n\n"
        "Installation is planned for the dry season after the pier survey.\n"),
    "decisions/retrofit-budget.md": (
        "# Retrofit budget\n\n"
        "The harbor board approved a budget of 48,000 credits for the beacon retrofit.\n"
        "Approval came from [[people/iris-vale]] on the spring session.\n"),
    "suppliers/lumen-works.md": (
        "# Lumen Works\n\n"
        "Lumen Works supplies the lamp assembly with a lead time of eleven weeks.\n"),
    "people/iris-vale.md": "# Iris Vale\n\nChair of the harbor board.\n",
    "private/secret.md": "# Hidden\n\nThe beacon retrofit password is not here.\n",
}


def make_vault(root: Path, notes: dict, index: bool = True) -> Path:
    """A fictional vault with routes.json and, by default, its index."""
    vault = Path(root).resolve() / "vault"
    (vault / ".context").mkdir(parents=True)
    (vault / ".context" / "routes.json").write_text(json.dumps(ROUTES), encoding="utf-8")
    for name, text in notes.items():
        path = vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    if index:
        done = subprocess.run([sys.executable, "-m", "context_layer.cli", "index", str(vault)],
                              cwd=REPO, capture_output=True, text=True,
                              env=isolated_home_env(os.environ, vault.parent))
        assert done.returncode == 0, done.stderr
    return vault


class VaultCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = make_vault(Path(self.temp.name), NOTES)

    def cli(self, *argv):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", *argv], cwd=REPO,
                              capture_output=True, text=True,
                              env=isolated_home_env(os.environ, str(self.vault.parent)))

    def packet(self, prompt="beacon retrofit budget supplier", method="fts", **kw):
        return orch.build_packet(self.vault, prompt, method, **kw)

    def job(self, packet_id=None, **overrides):
        packet_id = packet_id or self.packet()["id"]
        args = dict(objective="List the approved budget and the lamp supplier lead time.",
                    packet_id=packet_id, root_goal="Decide whether the retrofit is funded.",
                    allowed_roots=["projects", "decisions", "suppliers"], exclusions=[],
                    known_unknowns=["final installation date"], method=None,
                    acceptance="each record quotes the budget or the lead time verbatim",
                    stop_when="both facts have a record, or a source is missing (BLOCKED)",
                    out=None, task_id="job-test", attempt=1, max_total_tokens=20000,
                    max_seconds=600)
        args.update(overrides)
        return orch.new_job(self.vault, **args)


# ---------------------------------------------------------------------------
# 1. Shared packets
# ---------------------------------------------------------------------------

class Packets(VaultCase):

    def test_id_is_stable_and_second_build_reuses_the_file(self):
        first = self.packet()
        path = self.vault / first["path"]
        assert_private_path(self, path)
        mtime = path.stat().st_mtime_ns
        second = self.packet()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        stored = json.loads(path.read_text(encoding="utf-8"))
        body = {k: v for k, v in stored.items() if k != "id"}
        self.assertEqual(hashlib.sha256(orch.canonical(body)).hexdigest(), first["id"])
        self.assertIsNotNone(stored["index_identity"])
        self.assertIsNone(stored["graph_identity"])          # fts does not use the graph
        for item in stored["evidence"]:
            text = (self.vault / item["source_path"]).read_text(encoding="utf-8")
            self.assertTrue(orch.span_at(text, item["content"], item["line_start"],
                                         item["line_end"]))
            self.assertFalse(item["source_path"].startswith("private/"))

    def test_different_request_or_method_gives_a_different_id(self):
        base = self.packet()["id"]
        self.assertNotEqual(base, self.packet(prompt="lamp supplier lead time")["id"])
        synaptic = self.packet(method="synaptic")
        self.assertNotEqual(base, synaptic["id"])
        self.assertIsNotNone(synaptic["packet"]["graph_identity"])
        self.assertEqual(synaptic["id"], self.packet(method="synaptic")["id"])

    def test_served_after_recheck(self):
        built = self.packet()
        served = orch.read_packet(self.vault, built["id"])
        self.assertTrue(served["served"])
        self.assertEqual(served["status"], "OK")
        self.assertEqual(served["evidence"], built["packet"]["evidence"])
        done = self.cli("packet", "show", str(self.vault), built["id"], "--compact")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["id"], built["id"])

    def test_stale_source_withholds_the_whole_packet(self):
        built = self.packet()
        changed = built["packet"]["evidence"][0]["source_path"]
        with open(self.vault / changed, "a", encoding="utf-8") as handle:
            handle.write("\nA later edit.\n")
        served = orch.read_packet(self.vault, built["id"])
        self.assertFalse(served["served"])
        self.assertEqual(served["status"], "WITHHELD")
        self.assertEqual(served["evidence"], [])
        self.assertTrue(any(changed in r and "changed" in r for r in served["reasons"]))
        done = self.cli("packet", "show", built["id"], "--vault", str(self.vault))
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["status"], "WITHHELD")

    def test_newly_excluded_or_deleted_source_is_withheld(self):
        built = self.packet()
        name = built["packet"]["evidence"][0]["source_path"]
        routes = dict(ROUTES, exclude_prefixes=["private", name.split("/")[0]])
        (self.vault / ".context" / "routes.json").write_text(json.dumps(routes))
        self.assertFalse(orch.read_packet(self.vault, built["id"])["served"])
        (self.vault / ".context" / "routes.json").write_text(json.dumps(ROUTES))
        (self.vault / name).unlink()
        served = orch.read_packet(self.vault, built["id"])
        self.assertIn(f"{name}: source missing", served["reasons"])

    def test_edited_or_forged_packet_file_is_withheld(self):
        built = self.packet()
        path = self.vault / built["path"]
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["evidence"][0]["content"] = "The board approved 480,000 credits."
        path.write_text(json.dumps(stored), encoding="utf-8")
        served = orch.read_packet(self.vault, built["id"])
        self.assertIn("does not match its id", served["reasons"][0])
        # A forger who also recomputes the id still fails: the text is not in the source.
        body = {k: v for k, v in stored.items() if k != "id"}
        forged_id = orch.packet_id_of(body)
        (self.vault / orch.PACKETS_DIR / f"{forged_id}.json").write_text(
            json.dumps({"id": forged_id, **body}), encoding="utf-8")
        served = orch.read_packet(self.vault, forged_id)
        self.assertFalse(served["served"])
        self.assertTrue(any("not verbatim" in r for r in served["reasons"]))

    def test_mcp_read_packet(self):
        built = self.packet()
        state = mcp_server.Server(self.vault)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "read_packet", "arguments": {"id": built["id"]}}}
        result = mcp_server.handle(request, state)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["status"], "OK")
        request["params"]["arguments"] = {"id": "nothex"}
        self.assertEqual(mcp_server.handle(request, state)["error"]["code"], -32602)
        (self.vault / built["packet"]["evidence"][0]["source_path"]).write_text("changed")
        request["params"]["arguments"] = {"id": built["id"]}
        result = mcp_server.handle(request, state)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["status"], "WITHHELD")

    def test_cli_build_prints_id_and_estimate(self):
        done = self.cli("packet", "build", str(self.vault), "--prompt", "lamp supplier",
                        "--method", "synaptic", "--compact", "--budget-tokens", "300")
        self.assertEqual(done.returncode, 0, done.stderr)
        first = done.stdout.splitlines()[0].split()
        self.assertEqual(first[0], "packet")
        self.assertRegex(first[1], r"^[0-9a-f]{64}$")
        self.assertIn("est_tokens", done.stdout)
        self.assertIn("ceil(characters / 4)", done.stdout)

    def test_synaptic_budget_flags_follow_the_install_rules(self):
        def build(*flags):
            return self.cli("packet", "build", str(self.vault), "--prompt", "lamp supplier",
                            "--json", *flags)
        # --budget-tokens does not size the default synaptic packet: refused, not ignored.
        refused = build("--method", "synaptic", "--budget-tokens", "300")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--budget-tokens sizes only the --compact synaptic packet",
                      refused.stderr)
        self.assertEqual(len(refused.stderr.strip().splitlines()), 1)
        for flags in (("--compact",), ("--extra-tokens", "100"),
                      ("--method", "synaptic", "--compact", "--extra-tokens", "100"),
                      ("--method", "synaptic", "--extra-tokens", "-1")):
            with self.subTest(flags=flags):
                self.assertEqual(build(*flags).returncode, 2)
        # The default paths keep their ids; explicit defaults change nothing.
        default = json.loads(build("--method", "synaptic").stdout)["id"]
        self.assertEqual(default, self.packet("lamp supplier", "synaptic")["id"])
        self.assertEqual(default, json.loads(
            build("--method", "synaptic", "--extra-tokens", "600").stdout)["id"])
        fts = json.loads(build().stdout)["id"]
        self.assertEqual(fts, json.loads(build("--budget-tokens", "1200").stdout)["id"])
        # --extra-tokens and --compact reach retrieval and the request.
        small = self.packet("lamp supplier", "synaptic", extra_tokens=0)
        self.assertEqual(small["packet"]["request"]["extra_tokens"], 0)
        self.assertNotEqual(small["id"], default)
        compact = self.packet("lamp supplier", "synaptic", compact=True, budget_tokens=50)
        self.assertTrue(compact["packet"]["request"]["compact"])
        self.assertLessEqual(compact["packet"]["evidence_est_tokens"], 50)

    # -- C-27: a crafted packet is withheld with its reason, never a crash ----------

    def forge(self, mutate):
        built = self.packet()
        body = {k: v for k, v in built["packet"].items() if k != "id"}
        mutate(body)
        packet_id = orch.packet_id_of(body)
        (self.vault / orch.PACKETS_DIR / f"{packet_id}.json").write_text(
            json.dumps({"id": packet_id, **body}), encoding="utf-8")
        return packet_id

    def test_malformed_entries_are_withheld_not_raised(self):
        cases = {
            "line_start": lambda body: body["evidence"][0].update(line_start="three"),
            "not an object": lambda body: body["evidence"].insert(0, "text"),
            "not a list": lambda body: body.update(evidence={"a": 1}),
            "content": lambda body: body["evidence"][0].update(content=7),
            "source_sha256": lambda body: body["evidence"][0].update(source_sha256="x"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                packet_id = self.forge(mutate)
                served = orch.read_packet(self.vault, packet_id)
                self.assertFalse(served["served"])
                self.assertEqual(served["status"], "WITHHELD")
                self.assertTrue(any(label in reason for reason in served["reasons"]),
                                served["reasons"])
        packet_id = self.forge(cases["line_start"])
        shown = self.cli("packet", "show", str(self.vault), packet_id)
        self.assertEqual(shown.returncode, 1)
        self.assertEqual(json.loads(shown.stdout)["status"], "WITHHELD")
        state = mcp_server.Server(self.vault)
        result = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "read_packet",
                                               "arguments": {"id": packet_id}}},
                                   state)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["status"], "WITHHELD")


# ---------------------------------------------------------------------------
# The line model (C-07): a line ends at "\n" and nowhere else
# ---------------------------------------------------------------------------

class LineModel(unittest.TestCase):

    def test_only_newline_ends_a_line(self):
        text = f"a{FORM_FEED}b\nc{LINE_SEP}d\n\ne\r\nf{NEXT_LINE}g{PARA_SEP}h"
        lines = orch.lines_of(text)
        self.assertEqual(lines, [f"a{FORM_FEED}b\n", f"c{LINE_SEP}d\n", "\n", "e\r\n",
                                 f"f{NEXT_LINE}g{PARA_SEP}h"])
        self.assertEqual(orch.line_text("e\r\n"), "e")
        self.assertEqual(orch.lines_of(""), [])
        self.assertEqual(orch.lines_of("x\n"), ["x\n"])
        # locate() and span_at() agree for every separator str.splitlines would split on.
        for separator in (FORM_FEED, LINE_SEP, PARA_SEP, NEXT_LINE, "\r", chr(0x1c)):
            with self.subTest(separator=hex(ord(separator))):
                source = f"# Budget\nintro{separator}more\nThe board approved it.\n"
                found = orch.locate(source, "The board approved it.")
                self.assertEqual(found, (3, 3))
                self.assertTrue(orch.span_at(source, "The board approved it.", *found))

    def test_jsonl_lines_keep_a_record_whose_text_holds_a_line_separator(self):
        record = {"id": "U1", "observation": f"left{LINE_SEP}right"}
        raw = (json.dumps(record, ensure_ascii=False) + "\r\n" + "{}\n").encode("utf-8")
        lines = orch.jsonl_lines(raw)
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0][1]), record)

    def test_packets_over_unusual_separators_are_served(self):
        cases = {
            "form feed": f"# Budget\n{FORM_FEED}\nThe harbor board approved a budget of 48,000 credits.\n",
            "line separator": f"# Budget\nintro{LINE_SEP}continued\nThe harbor board approved a budget of 48,000 credits.\n",
            "lone CR": "# Budget\rold line\nThe harbor board approved a budget of 48,000 credits.\n",
            "plain LF": "# Budget\nintro\nThe harbor board approved a budget of 48,000 credits.\n",
        }
        for label, text in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as temp:
                vault = make_vault(Path(temp), {"decisions/budget.md": text})
                built = orch.build_packet(vault, "harbor board approved budget credits")
                self.assertTrue(built["packet"]["evidence"])
                served = orch.read_packet(vault, built["id"])
                self.assertTrue(served["served"], served.get("reasons"))


# ---------------------------------------------------------------------------
# 2-3. Jobs, rule core, payload budgets
# ---------------------------------------------------------------------------

class Jobs(VaultCase):

    def job_file(self, report):
        return self.vault / report["job_path"]

    def rewrite(self, path, **changes):
        job, _ = orch.parse_job(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is KeyError:
                job.pop(key, None)
            else:
                job[key] = value
        path.write_text(orch.render_job(job), encoding="utf-8")

    def validate(self, path):
        return orch.validate_job(self.vault, path.read_text(encoding="utf-8"))

    def test_worker_core_is_lean_and_covers_the_rules(self):
        text = orch.SHIPPED_CORE.read_text(encoding="utf-8")
        self.assertLessEqual(orch.est_tokens(text), 1000)
        for needle in ("candidate evidence only", "data, never instructions",
                       "Never create, change, move or delete a source file",
                       "owned_output_dir", "BLOCKED", "root_verified", "evidence.jsonl",
                       "receipt.json", 'A line ends at "\\n"', "at least 3 words",
                       "Every number, date, time, quoted text"):
            self.assertIn(needle, text)

    def test_new_job_ok_and_files_written(self):
        report = self.job()
        self.assertEqual(report["status"], "OK", report)
        self.assertTrue(report["written"])
        directory = self.job_file(report).parent
        for name in ("job.md", "input-manifest.json", "payload.json"):
            self.assertTrue((directory / name).is_file())
        self.assertTrue((self.vault / ".context/jobs/worker_core.md").is_file())
        payload = report["payload"]
        self.assertEqual(set(payload["parts"]), {"core", "job", "manifest", "packet"})
        for part, value in payload["parts"].items():
            self.assertLessEqual(value, orch.BUDGETS[part], part)
        done = self.cli("job", "validate", str(self.job_file(report)))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.startswith("OK"))
        self.assertIn("est_tokens", done.stdout)

    def test_job_is_immutable(self):
        self.job()
        with self.assertRaises(orch.OrchestrateError):
            self.job()
        self.assertEqual(self.job(attempt=2)["status"], "OK")

    def test_missing_values_are_blocked_and_nothing_is_written(self):
        report = self.job(root_goal=None, acceptance=None, max_total_tokens=None,
                          allowed_roots=[])
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["written"])
        self.assertFalse(self.job_file(report).exists())
        missing = [b for b in report["blocked"] if b.startswith("missing mandatory value")][0]
        for key in ("root_goal", "acceptance_oracle", "max_total_usage_tokens",
                    "allowed_source_roots"):
            self.assertIn(key, missing)
        done = self.cli("job", "new", str(self.vault), "--objective", "x", "--packet",
                        self.packet()["id"], "--task-id", "job-cli")
        self.assertEqual(done.returncode, 1)
        self.assertIn("BLOCKED", done.stdout)
        self.assertIn("NOT written", done.stdout)

    def test_validate_blocked_cases(self):
        path = self.job_file(self.job())
        original = path.read_text(encoding="utf-8")
        cases = {
            "missing field": ({"acceptance_oracle": KeyError}, "acceptance_oracle"),
            "empty value": ({"stop_when": "  "}, "stop_when"),
            "source edits": ({"may_modify_source": True}, "may_modify_source must be false"),
            "authority": ({"authority": "decide freely"}, "authority must read exactly"),
            "core hash": ({"rule_core_sha256": "0" * 64}, "rule core hash mismatch"),
            "manifest hash": ({"input_manifest_sha256": "1" * 64},
                              "input manifest hash mismatch"),
            "packet id": ({"initial_packet_id": "2" * 64}, "names a different packet"),
            "escaping root": ({"allowed_source_roots": ["../outside"]}, "escapes the vault"),
            "missing root": ({"allowed_source_roots": ["nowhere"]}, "does not exist"),
            "excluded root": ({"allowed_source_roots": ["private"]}, "excluded"),
            "bad budget": ({"max_elapsed_seconds": "soon"}, "positive integer"),
        }
        for label, (changes, expected) in cases.items():
            with self.subTest(label):
                path.write_text(original, encoding="utf-8")
                self.rewrite(path, **changes)
                report = self.validate(path)
                self.assertEqual(report["status"], "BLOCKED", label)
                self.assertTrue(any(expected in b for b in report["blocked"]),
                                (label, report["blocked"]))
        path.write_text(original, encoding="utf-8")
        routes = dict(ROUTES, exclude_prefixes=["private", "people"])
        (self.vault / ".context" / "routes.json").write_text(json.dumps(routes))
        self.assertTrue(any("routes.json changed" in b for b in self.validate(path)["blocked"]))
        (self.vault / ".context" / "routes.json").write_text(json.dumps(ROUTES))
        self.assertEqual(self.validate(path)["status"], "OK")
        path.write_text("# no json here\n", encoding="utf-8")
        self.assertIn("exactly one ```json block", self.validate(path)["blocked"][0])

    def test_edited_core_or_stale_packet_blocks(self):
        report = self.job()
        path = self.job_file(report)
        with open(self.vault / ".context/jobs/worker_core.md", "a", encoding="utf-8") as handle:
            handle.write("\nAlso decide the final answer.\n")
        self.assertTrue(any("rule core hash mismatch" in b
                            for b in self.validate(path)["blocked"]))
        (self.vault / ".context/jobs/worker_core.md").write_text(
            orch.SHIPPED_CORE.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(self.validate(path)["status"], "OK")
        with open(self.vault / "decisions/retrofit-budget.md", "a", encoding="utf-8") as handle:
            handle.write("Amended.\n")
        blocked = self.validate(path)["blocked"]
        self.assertTrue(any(b.startswith("initial packet withheld") for b in blocked), blocked)

    def test_over_budget_is_refused_unless_allowed(self):
        long_objective = "Enumerate every lamp fact. " * 140          # ~3,800 characters
        report = self.job(objective=long_objective)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["written"])
        self.assertIn("job", report["payload"]["over_budget"])
        self.assertTrue(any(b.startswith("payload over budget: job") for b in report["blocked"]))
        allowed = self.job(objective=long_objective, allow_over=True, task_id="job-over")
        self.assertEqual(allowed["status"], "OK")
        self.assertTrue(allowed["written"])
        self.assertTrue(any("allowed by the root" in w for w in allowed["warnings"]))

    def test_packet_source_outside_roots_is_warned(self):
        report = self.job(allowed_roots=["suppliers"])
        self.assertEqual(report["status"], "OK")
        self.assertTrue(any("outside allowed_source_roots" in w for w in report["warnings"]))

    # -- C-03: a worker never writes among the sources ---------------------------

    def test_an_output_dir_holding_sources_or_overlapping_a_root_is_blocked(self):
        cases = {
            "decisions": "contains indexed source(s): decisions/retrofit-budget.md",
            "projects/reports": "overlaps allowed_source_root projects",
            "people": "contains indexed source(s): people/iris-vale.md",
            ".context/elsewhere": "hidden folder or the tool's own state",
            ".context/jobs/job-test/attempt-009/out": "hidden folder or the tool's own state",
        }
        for number, (out, expected) in enumerate(cases.items(), start=1):
            with self.subTest(out=out):
                report = self.job(out=out, task_id=f"job-scope-{number}")
                self.assertEqual(report["status"], "BLOCKED", report)
                self.assertFalse(report["written"])
                self.assertTrue(any(expected in b for b in report["blocked"]),
                                report["blocked"])
        whole = self.job(out="reports/whole", allowed_roots=["."], task_id="job-whole")
        self.assertTrue(any("overlaps allowed_source_root ." in b for b in whole["blocked"]))

    def test_a_separate_output_dir_is_allowed_with_an_indexing_warning(self):
        report = self.job(out="reports/job-a", task_id="job-apart")
        self.assertEqual(report["status"], "OK", report)
        self.assertTrue(any("will be indexed as sources" in w for w in report["warnings"]))
        default = self.job(allowed_roots=["."], task_id="job-default")
        self.assertEqual(default["status"], "OK", default)


# ---------------------------------------------------------------------------
# 4. Handback check, receipt, sampling
# ---------------------------------------------------------------------------

class HandbackBase(VaultCase):

    def setUp(self):
        super().setUp()
        self.report = self.job()
        self.job_path = self.vault / self.report["job_path"]
        self.job_data, _ = orch.parse_job(self.job_path.read_text(encoding="utf-8"))
        self.out = self.vault / self.job_data["owned_output_dir"]
        self.out.mkdir(parents=True)

    def sha(self, name):
        return hashlib.sha256((self.vault / name).read_bytes()).hexdigest()

    def record(self, rid, name, start, end, span, **extra):
        return {"id": rid, "observation": f"observation {rid}", "source_path": name,
                "source_sha256": self.sha(name) if (self.vault / name).is_file() else "0" * 64,
                "line_start": start, "line_end": end, "span": span,
                "method": "read_source", "uncertainty": "none noted", **extra}

    def good(self):
        return [
            self.record("E1", "decisions/retrofit-budget.md", 3, 3, "a budget of 48,000 credits"),
            self.record("E2", "suppliers/lumen-works.md", 3, 3, "lead time of eleven weeks"),
            self.record("E3", "projects/beacon-retrofit.md", 3, 3, "replaces the lamp assembly"),
        ]

    def hand_back(self, records, **receipt):
        with open(self.out / "evidence.jsonl", "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        body = {"schema": orch.SCHEMA_RECEIPT, "task_id": "job-test", "attempt": 1,
                "state": "READY", "counts": {"records": len(records), "scanned": 3,
                                             "excluded": 0, "failed": 0},
                "handoff": "handoff.md", "blocker": None}
        body.update(receipt)
        (self.out / "receipt.json").write_text(json.dumps(body), encoding="utf-8")

    def check(self, **kw):
        return orch.check_handback(self.out, job_file=self.job_path, **kw)

    def failures(self, report):
        return {r["id"]: " ".join(r["reasons"]) for r in report["results"]
                if not r["mechanically_checked"]}


class Handback(HandbackBase):

    def test_good_handback_passes(self):
        self.hand_back(self.good())
        report = self.check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["mechanically_checked"], 3)
        done = self.cli("handback", "check", str(self.out), "--job", str(self.job_path))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("mechanically_checked 3, failed 0", done.stdout)
        self.assertIn("does not make the observation true", done.stdout)

    def test_catches_wrong_sha_missing_span_out_of_root_and_fabrication(self):
        records = self.good() + [
            self.record("W1", "suppliers/lumen-works.md", 3, 3, "lead time of eleven weeks",
                        source_sha256="a" * 64),
            self.record("W2", "decisions/retrofit-budget.md", 1, 1, "a budget of 48,000 credits"),
            self.record("W3", "people/iris-vale.md", 3, 3, "Chair of the harbor board"),
            self.record("W4", "decisions/retrofit-budget.md", 3, 3,
                        "a budget of 480,000 credits"),                   # fabricated quote
            self.record("W5", "private/secret.md", 3, 3, "password"),
            self.record("W6", "../escape.md", 1, 1, "x"),
            self.record("W7", "suppliers/missing.md", 1, 1, "x"),
            self.record("W8", "suppliers/lumen-works.md", 3, 3, "lead time of eleven weeks",
                        root_verified=True),
            self.record("W9", "suppliers/lumen-works.md", 5, 3, "lead time"),
            {"id": "W10", "observation": "no source"},
        ]
        self.hand_back(records)
        report = self.check()
        self.assertFalse(report["ok"])
        failed = self.failures(report)
        self.assertEqual(set(failed), {f"W{n}" for n in range(1, 11)})
        self.assertIn("does not match the current file", failed["W1"])
        self.assertIn("span not found in lines 1-1 (found elsewhere", failed["W2"])
        self.assertIn("outside allowed_source_roots", failed["W3"])
        self.assertIn("span not found in lines 3-3 (found nowhere", failed["W4"])
        self.assertIn("refused", failed["W5"])
        self.assertIn("refused", failed["W6"])
        self.assertIn("source file missing", failed["W7"])
        self.assertIn("root_verified", failed["W8"])
        self.assertIn("1 <= start <= end", failed["W9"])
        self.assertIn("missing field", failed["W10"])
        self.assertEqual(report["mechanically_checked"], 3)

    def test_changed_source_after_handback_fails(self):
        self.hand_back(self.good())
        with open(self.vault / "suppliers/lumen-works.md", "a", encoding="utf-8") as handle:
            handle.write("Revised.\n")
        self.assertIn("E2", self.failures(self.check()))

    def test_duplicate_ids_fail(self):
        records = self.good()
        records[1]["id"] = "E1"
        self.hand_back(records)
        failed = self.failures(self.check())
        self.assertIn("duplicate id", failed["E1"])

    def test_receipt_rules(self):
        self.hand_back(self.good(), counts={"records": 9})
        self.assertTrue(any("counts.records" in p for p in self.check()["problems"]))
        self.hand_back(self.good(), state="PARTIAL")
        self.assertTrue(any("must name its blocker" in p for p in self.check()["problems"]))
        self.hand_back(self.good(), task_id="another-job")
        self.assertTrue(any("do not match the job" in p for p in self.check()["problems"]))
        self.hand_back(self.good(), blocker="x" * 700)
        report = self.check()
        self.assertGreater(report["receipt"]["est_tokens"], orch.RECEIPT_MAX_TOKENS)
        self.assertTrue(any("over 160" in p for p in report["problems"]))
        self.assertFalse(report["ok"])
        (self.out / "receipt.json").unlink()
        self.assertIn("receipt.json missing", self.check()["problems"])

    def test_a_valid_receipt_fits_the_cap(self):
        self.hand_back(self.good())
        text = (self.out / "receipt.json").read_text(encoding="utf-8")
        self.assertLessEqual(orch.est_tokens(text), orch.RECEIPT_MAX_TOKENS)

    def test_sampling_draws_only_checked_records_and_replays_from_its_seed(self):
        records = [self.record(f"E{n}", "suppliers/lumen-works.md", 3, 3,
                               "a lead time of eleven weeks") for n in range(1, 13)]
        records.append(self.record("BAD", "suppliers/lumen-works.md", 3, 3,
                                   "a lead time of twelve weeks"))
        self.hand_back(records)
        one = self.check(k=4, seed="root-draw-1")
        two = self.check(k=4, seed="root-draw-1")
        self.assertEqual(one["sample"]["ids"], two["sample"]["ids"])
        self.assertEqual(len(one["sample"]["ids"]), 4)
        self.assertNotIn("BAD", one["sample"]["ids"])
        self.assertEqual([r["id"] for r in one["sample"]["records"]], one["sample"]["ids"])
        other = self.check(k=4, seed="root-draw-2")
        self.assertEqual(len(other["sample"]["ids"]), 4)
        self.assertEqual(self.check(k=50, seed="s")["sample"]["k"], 12)
        self.assertEqual(self.check(k="auto", seed="s")["sample"]["k"], 9)   # N=12, D=2
        unseeded = self.check(k=3)
        self.assertIn("fresh random seed", unseeded["sample"]["seed_source"])
        self.assertRegex(unseeded["sample"]["seed"], r"^[0-9a-f]{32}$")
        self.assertEqual(unseeded["evidence_sha256"],
                         hashlib.sha256((self.out / "evidence.jsonl").read_bytes()).hexdigest())
        self.assertNotEqual(unseeded["sample"]["seed"], self.check(k=3)["sample"]["seed"])
        replayed = self.check(k=3, seed=unseeded["sample"]["seed"])
        self.assertEqual(replayed["sample"]["ids"], unseeded["sample"]["ids"])
        self.assertEqual(orch.sample(["a", "b", "c"], 2, "x"), orch.sample(["c", "b", "a"], 2, "x"))

    def test_zero_defect_sample_sizes_match_the_hypergeometric_table(self):
        self.assertEqual(orch.min_sample(12, 2), 9)
        self.assertEqual(orch.min_sample(20, 2), 16)
        self.assertEqual(orch.min_sample(100, 10), 25)
        self.assertEqual(orch.min_sample(100, 5), 45)

    def test_scope_is_required(self):
        self.hand_back(self.good())
        with self.assertRaises(orch.OrchestrateError):
            orch.check_handback(Path(self.temp.name))
        report = orch.check_handback(self.out, vault=self.vault, allowed_roots=["suppliers"])
        self.assertEqual(set(self.failures(report)), {"E1", "E3"})

    # -- C-27: evidence.jsonl is sized before it is read --------------------------

    def test_an_oversized_or_linked_evidence_file_is_not_read(self):
        small = self.job(max_evidence_bytes=1000, task_id="job-small")
        path = self.vault / small["job_path"]
        job, _ = orch.parse_job(path.read_text(encoding="utf-8"))
        out = self.vault / job["owned_output_dir"]
        out.mkdir(parents=True)
        (out / "evidence.jsonl").write_bytes(b"x" * 5_000_000)
        tracemalloc.start()
        report = orch.check_handback(out, job_file=path)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertLess(peak, 1_000_000, "the oversized file was read")
        self.assertIn("evidence.jsonl is 5000000 bytes, over the 1000 cap; not read",
                      report["problems"])
        self.assertIsNone(report["evidence_sha256"])
        (out / "evidence.jsonl").unlink()
        (out / "evidence.jsonl").symlink_to(self.vault / "suppliers" / "lumen-works.md")
        report = orch.check_handback(out, job_file=path)
        self.assertTrue(any("not a regular file" in p for p in report["problems"]))


# ---------------------------------------------------------------------------
# C-12 / C-13 / improvement 4.1: false rejections diagnosed, fabricated claims caught
# ---------------------------------------------------------------------------

BUDGET_TEXT = ("# Retrofit budget\n\n"
               "The harbor board approved a budget of 48,000 credits for the beacon retrofit.\n"
               "Approval came from the chair on the spring session.\n")


class ClaimSpanGate(HandbackBase):

    def add(self, name, text):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return name

    def claim(self, rid, name, start, end, span, observation):
        return dict(self.record(rid, name, start, end, span), observation=observation)

    def test_trivial_spans_and_fabricated_observations_fail(self):
        name = self.add("decisions/budget.md", BUDGET_TEXT)
        self.hand_back([
            self.claim("B1", name, 3, 3, "a", "The board approved 480,000 credits."),
            self.claim("B2", name, 1, 4, " ", "The chair vetoed the retrofit."),
            self.claim("B3", name, 1, 4, "credits", "Budget is 1,000,000 credits."),
            self.claim("OK1", name, 3, 3, "approved a budget of 48,000 credits",
                       "The board approved 48,000 credits."),
            self.claim("OK2", name, 3, 4, "for the beacon retrofit.\nApproval came",
                       "The retrofit budget was approved."),
        ])
        report = self.check()
        failed = self.failures(report)
        self.assertEqual(set(failed), {"B1", "B2", "B3"})
        self.assertIn("too short to anchor a claim", failed["B1"])
        self.assertIn('observation asserts "480,000"; not in span', failed["B1"])
        self.assertIn('observation asserts "1,000,000"; not in span', failed["B3"])
        self.assertIn("does not reach both line_start 1 and line_end 4", failed["B3"])
        by_id = {r["id"]: r for r in report["results"]}
        self.assertTrue(by_id["OK1"]["observation_anchored"])
        self.assertFalse(by_id["OK2"]["observation_anchored"])
        self.assertEqual(report["observation_unanchored"], 1)
        wide = self.claim("W", name, 1, 30, "harbor board approved", "The board approved it.")
        self.assertIn("a record cites at most 21",
                      " ".join(orch.check_record(self.vault, wide, ["decisions"], [], None,
                                                 {})[1]))

    def test_near_misses_say_what_to_fix(self):
        crlf = self.add("decisions/crlf.md", BUDGET_TEXT.replace("\n", "\r\n"))
        nfd = self.add("decisions/nfd.md", unicodedata.normalize(
            "NFD", "# Caf\u00e9 budget\n\nThe caf\u00e9 approved 12 credits.\n"))
        form = self.add("decisions/ff.md", f"# Budget\n{FORM_FEED}\n"
                                           "The harbor board approved a budget of 48,000 credits.\n")
        budget = self.add("decisions/budget.md", BUDGET_TEXT)
        self.hand_back([
            self.claim("F1", budget, 3, 4, "for the beacon retrofit. Approval came",
                       "The retrofit was approved."),
            self.claim("F2", crlf, 3, 4, "beacon retrofit.\nApproval came",
                       "The retrofit was approved."),
            self.claim("F3", nfd, 3, 3, unicodedata.normalize("NFC", "The caf\u00e9 approved"),
                       "The cafe approved it."),
            self.claim("F4", form, 3, 3, "approved a budget of 48,000 credits",
                       "The board approved 48,000 credits."),
            self.claim("P1", crlf, 3, 4, "beacon retrofit.\r\nApproval came",
                       "The retrofit was approved."),
        ])
        report = self.check()
        by_id = {r["id"]: r for r in report["results"]}
        self.assertIn("whitespace or line breaks", by_id["F1"]["normalized_match"])
        self.assertIn("line endings", by_id["F2"]["normalized_match"])
        self.assertIn("Unicode normalisation", by_id["F3"]["normalized_match"])
        for rid in ("F1", "F2", "F3"):
            self.assertFalse(by_id[rid]["mechanically_checked"])
            self.assertIn("only after normalising", " ".join(by_id[rid]["reasons"]))
        self.assertTrue(by_id["F4"]["mechanically_checked"], by_id["F4"]["reasons"])
        self.assertTrue(by_id["P1"]["mechanically_checked"], by_id["P1"]["reasons"])

    def test_a_record_holding_a_raw_line_separator_parses(self):
        name = self.add("decisions/sep.md", f"# Sep\n\nleft{LINE_SEP}right side of it\n")
        record = self.claim("U1", name, 3, 3, "right side of it", f"left{LINE_SEP}right")
        (self.out / "evidence.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n",
                                                 encoding="utf-8")
        (self.out / "receipt.json").write_text(json.dumps(
            {"schema": orch.SCHEMA_RECEIPT, "task_id": "job-test", "attempt": 1,
             "state": "READY", "counts": {"records": 1}, "handoff": "h", "blocker": None}))
        report = self.check()
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["records"], 1)
        self.assertTrue(report["results"][0]["mechanically_checked"], report["results"][0])

    def test_the_synthetic_claim_set_rates(self):
        rates = claim_gate_rates()
        self.assertEqual(rates["genuine"], 50)
        self.assertEqual(rates["swapped"], 50)
        self.assertEqual(rates["caught"], 45)            # every hard-token swap
        self.assertEqual(rates["missed_soft"], 5)        # ordinary-word swaps pass: a limit
        self.assertEqual(rates["false_rejects"], 5)      # the five risky paraphrases
        self.assertEqual(rates["false_reject_ids"], ["G26a", "G27a", "G28a", "G29a", "G30a"])


# 30 fictional facts: (sentence, span, genuine observations, swapped observations).
# 1-20 carry hard tokens; 21-25 carry none (a swap there changes ordinary words, which
# the gate cannot see); 26-30 are genuine paraphrases in another number or name form.
CLAIM_FACTS = [
    ("The harbor board approved a budget of 48,000 credits for the beacon retrofit.",
     "approved a budget of 48,000 credits",
     ["The board approved 48,000 credits.", "A budget of 48000 credits was approved."],
     ["The board approved 480,000 credits.", "A budget of 84,000 credits was approved."]),
    ("Lumen Works supplies the lamp assembly with a lead time of eleven weeks.",
     "Lumen Works supplies the lamp assembly",
     ["Lumen Works is the lamp supplier.", "The lamp assembly comes from Lumen Works."],
     ["Harbor Works is the lamp supplier.", "The lamp assembly comes from Lumen Industries."]),
    ("The pier inspection is scheduled for 2026-03-12 at 09:30.",
     "scheduled for 2026-03-12 at 09:30",
     ["Inspection on 2026-03-12 at 09:30.", "The inspection happens at 09:30 on 2026-03-12."],
     ["Inspection on 2026-03-21 at 09:30.", "The inspection happens at 19:30 on 2026-03-12."]),
    ("Call `build_index` before `load_config` when the vault changes.",
     "Call `build_index` before `load_config`",
     ["build_index runs before load_config.", "Run `build_index` first, then `load_config`."],
     ["build_graph runs before load_config.", "Run `build_index` first, then `read_config`."]),
    ('The release notes say "retries stop after three attempts" for workers.',
     'say "retries stop after three attempts"',
     ['The notes say "retries stop after three attempts".',
      "Retries stop after three attempts, per the notes."],
     ['The notes say "retries stop after five attempts".', 'The notes say "retries never stop".']),
    ("Full specifications are published at https://example.org/specs/lamp-v2 for review.",
     "published at https://example.org/specs/lamp-v2",
     ["Specs are at https://example.org/specs/lamp-v2.",
      "See https://example.org/specs/lamp-v2 for the specification."],
     ["Specs are at https://example.org/specs/lamp-v3.",
      "See https://example.com/specs/lamp-v2 for the specification."]),
    ("Survey coverage reached 87% of the north pier in the spring pass.",
     "coverage reached 87% of the north pier",
     ["Coverage was 87%.", "87 percent of the north pier was covered."],
     ["Coverage was 78%.", "Coverage was 97%."]),
    ("Iris Vale chairs the harbor board since the spring session.",
     "Iris Vale chairs the harbor board",
     ["The board is chaired by Iris Vale.", "Iris Vale is the chair."],
     ["The board is chaired by Iris Vane.", "Mara Vale is the chair."]),
    ("The warranty covers 24 months from the installation date.",
     "warranty covers 24 months",
     ["The warranty lasts 24 months.", "Warranty: 24 months."],
     ["The warranty lasts 36 months.", "Warranty: 12 months."]),
    ("Each lamp draws 3.5 kW at full brightness.",
     "Each lamp draws 3.5 kW",
     ["A lamp uses 3.5 kW.", "Power draw per lamp is 3.5 kW."],
     ["A lamp uses 5.3 kW.", "Power draw per lamp is 35 kW."]),
    ("Northwind Freight delivers the parts to dock 7 every Tuesday.",
     "Northwind Freight delivers the parts to dock 7",
     ["Parts arrive at dock 7 via Northwind Freight.",
      "Northwind Freight handles delivery to dock 7."],
     ["Parts arrive at dock 9 via Northwind Freight.",
      "Southwind Freight handles delivery to dock 7."]),
    ("The config key is `lamp.max_lumens` and it defaults to 1,200.",
     "`lamp.max_lumens` and it defaults to 1,200",
     ["lamp.max_lumens defaults to 1,200.", "The default of `lamp.max_lumens` is 1200."],
     ["lamp.max_lumens defaults to 2,100.", "The default of `lamp.min_lumens` is 1200."]),
    ("The contract was signed on March 3, 2026 by both parties.",
     "signed on March 3, 2026",
     ["Signed March 3, 2026.", "The contract dates from 3 March 2026."],
     ["Signed March 13, 2026.", "The contract dates from 3 May 2026."]),
    ("The tender closes at 17:00 on the last working day.",
     "tender closes at 17:00",
     ["Tenders close at 17:00.", "The tender deadline is 17:00."],
     ["Tenders close at 15:00.", "The tender deadline is 17:30."]),
    ("Harbor Master Office approves berth changes within 5 days.",
     "Harbor Master Office approves berth changes within 5 days",
     ["Berth changes need Harbor Master Office approval, within 5 days.",
      "The Harbor Master Office decides berth changes in 5 days."],
     ["Berth changes need Port Authority approval, within 5 days.",
      "The Harbor Master Office decides berth changes in 10 days."]),
    ("The beacon flashes every 4 seconds with a range of 12 nautical miles.",
     "flashes every 4 seconds with a range of 12 nautical miles",
     ["It flashes every 4 seconds, visible to 12 nautical miles.",
      "Range 12 nautical miles; period 4 seconds."],
     ["It flashes every 6 seconds, visible to 12 nautical miles.",
      "Range 21 nautical miles; period 4 seconds."]),
    ("Crew members must log hours in `crew_hours.csv` each week.",
     "log hours in `crew_hours.csv`",
     ["Hours go into crew_hours.csv.", "Weekly hours are logged in `crew_hours.csv`."],
     ["Hours go into crew_shifts.csv.", "Weekly hours are logged in `crew_log.csv`."]),
    ("The retrofit budget line is 12,500 credits for lighting and 3,000 credits for cabling.",
     "12,500 credits for lighting and 3,000 credits for cabling",
     ["Lighting gets 12,500 credits and cabling 3,000.",
      "Cabling: 3,000 credits; lighting: 12,500 credits."],
     ["Lighting gets 15,200 credits and cabling 3,000.",
      "Cabling: 30,000 credits; lighting: 12,500 credits."]),
    ("Version 2.4 of the harbor app adds tide alerts.",
     "Version 2.4 of the harbor app adds tide alerts",
     ["The harbor app 2.4 adds tide alerts.", "Tide alerts arrive in version 2.4."],
     ["The harbor app 2.5 adds tide alerts.", "Tide alerts arrive in version 4.2."]),
    ("Silver Gull Marine repaired the east pier crane in October.",
     "Silver Gull Marine repaired the east pier crane",
     ["The east pier crane was repaired by Silver Gull Marine.",
      "Silver Gull Marine fixed the crane."],
     ["The east pier crane was repaired by Grey Gull Marine.",
      "Silver Tern Marine fixed the crane."]),
    ("The retrofit covers the north pier only.", "The retrofit covers the north pier only",
     ["Only the north pier is covered."], ["Only the south pier is covered."]),
    ("The lamps are replaced before the storm season.", "replaced before the storm season",
     ["Replacement happens before storm season."], ["Replacement happens after storm season."]),
    ("Night inspections are allowed with a permit.",
     "Night inspections are allowed with a permit",
     ["A permit allows night inspections."], ["Night inspections are forbidden."]),
    ("The old cables stay in place until the audit.",
     "The old cables stay in place until the audit",
     ["Old cables remain until the audit."], ["Old cables are removed before the audit."]),
    ("Volunteers help with the paint work on weekends.", "Volunteers help with the paint work",
     ["Volunteers do paint work."], ["Contractors do the paint work."]),
    ("The budget is 48,000 credits.", "The budget is 48,000 credits",
     ["The budget is about 48 thousand credits."], ["The budget is 58,000 credits."]),
    ("Delivery takes eleven weeks.", "Delivery takes eleven weeks",
     ["Delivery takes 11 weeks."], ["Delivery takes 12 weeks."]),
    ("The meeting is on 12 March 2026.", "The meeting is on 12 March 2026",
     ["Meeting: 2026-03-12."], ["The meeting is on 13 March 2026."]),
    ("Iris Vale approved the plan.", "Iris Vale approved the plan",
     ["Chair Vale approved the plan."], ["Iris Hale approved the plan."]),
    ("Coverage reached 87% last spring.", "Coverage reached 87% last spring",
     ["Coverage reached 0.87 last spring."], ["Coverage reached 78% last spring."]),
]


def claim_gate_rates() -> dict:
    """Run the 100-record synthetic set (50 genuine, 50 swapped) through handback check.

    Printed by `python3 -c "import sys; sys.path.insert(0, 'tests'); import
    test_orchestrate as t; print(t.claim_gate_rates())"` from the repository root.
    """
    with tempfile.TemporaryDirectory() as temp:
        vault = make_vault(Path(temp), {}, index=False)
        records = []
        kinds = {}
        for number, (sentence, span, genuine, swapped) in enumerate(CLAIM_FACTS, start=1):
            name = f"facts/f{number:02d}.md"
            path = vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# Fact {number}\n\n{sentence}\n", encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            for label, observations in (("G", genuine), ("X", swapped)):
                for suffix, observation in zip("ab", observations):
                    rid = f"{label}{number:02d}{suffix}"
                    kinds[rid] = ("genuine" if label == "G" else
                                  "soft" if 21 <= number <= 25 else "hard")
                    records.append({"id": rid, "observation": observation, "source_path": name,
                                    "source_sha256": digest, "line_start": 3, "line_end": 3,
                                    "span": span, "method": "read", "uncertainty": "none"})
        out = vault / "returns"
        out.mkdir()
        (out / "evidence.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records),
                                            encoding="utf-8")
        (out / "receipt.json").write_text(json.dumps(
            {"schema": orch.SCHEMA_RECEIPT, "task_id": "claims", "attempt": 1,
             "state": "READY", "counts": {"records": len(records)}, "handoff": "h",
             "blocker": None}), encoding="utf-8")
        report = orch.check_handback(out, vault=vault, allowed_roots=["facts"])
    passed = {r["id"]: r["mechanically_checked"] for r in report["results"]}
    genuine = [rid for rid, kind in kinds.items() if kind == "genuine"]
    swapped = [rid for rid, kind in kinds.items() if kind != "genuine"]
    return {"genuine": len(genuine), "swapped": len(swapped),
            "caught": sum(not passed[rid] for rid in swapped),
            "missed_soft": sum(passed[rid] for rid in swapped if kinds[rid] == "soft"),
            "missed_hard": sum(passed[rid] for rid in swapped if kinds[rid] == "hard"),
            "false_rejects": sum(not passed[rid] for rid in genuine),
            "false_reject_ids": sorted(rid for rid in genuine if not passed[rid])}


# ---------------------------------------------------------------------------
# C-14 / improvement 4.2: the worker cannot steer which records the root reads
# ---------------------------------------------------------------------------

class AuditSeed(HandbackBase):

    def test_blank_lines_do_not_steer_the_default_sample(self):
        records = [self.record(f"E{n}", "suppliers/lumen-works.md", 3, 3,
                               "a lead time of eleven weeks") for n in range(1, 12)]
        # Mechanically sound, wrong in a way the gate cannot see: no hard token.
        records.append(dict(self.record("BAD", "suppliers/lumen-works.md", 3, 3,
                                        "supplies the lamp assembly"),
                            observation="the supplier never delivers on time"))
        self.hand_back(records)
        body = (self.out / "evidence.jsonl").read_bytes()
        included = 0
        for blank in range(100):
            (self.out / "evidence.jsonl").write_bytes(body + b"\n" * blank)
            report = self.check(k="auto")
            self.assertTrue(report["ok"])
            self.assertEqual(report["sample"]["k"], 9)          # 9 of 12
            included += "BAD" in report["sample"]["ids"]
        # Expected 75 of 100 (k/N = 0.75); the bounds are about +-4.5 standard deviations.
        self.assertGreaterEqual(included, 55)
        self.assertLessEqual(included, 95)
        fixed = {tuple(self.check(k="auto", seed="root-seed")["sample"]["ids"])
                 for _ in range(3)}
        self.assertEqual(len(fixed), 1)


# ---------------------------------------------------------------------------
# 5. Estimator, 6. handoff
# ---------------------------------------------------------------------------

class Estimator(unittest.TestCase):

    def test_arithmetic_and_verdicts(self):
        result = orch.estimate(root_read=100000, reread=0.1, price_ratio=0.2, startup=5000,
                               verify=3000)
        # worker 0.2 * (5000 + 100000) = 21000; reread 10000; verify 3000
        self.assertEqual(result["terms"]["worker"], 21000)
        self.assertEqual(result["delegated"], 34000)
        self.assertEqual(result["saving"], 66000)
        self.assertEqual(result["verdict"], "DELEGATE")
        self.assertAlmostEqual(result["input_only_breakeven_reread_fraction"], 0.8)
        heavy = orch.estimate(root_read=100000, reread=0.85, price_ratio=0.2, startup=5000,
                              verify=3000)
        self.assertEqual(heavy["delegated"], 109000)
        self.assertEqual(heavy["verdict"], "DO IT YOURSELF")
        # Positive but thinner than the buffer: still do it yourself.
        thin = orch.estimate(root_read=10000, reread=0.5, price_ratio=0.2, startup=1000,
                             verify=1000)
        self.assertEqual(thin["delegated"], 0.2 * 11000 + 1000 + 5000)
        self.assertEqual(thin["verdict"], "DO IT YOURSELF")
        full = orch.estimate(root_read=100000, reread=0.1, price_ratio=0.2, startup=5000,
                             verify=3000, dispatch=500, worker_output=2000,
                             output_multiplier=5, integration=700, retry_rate=0.5)
        self.assertAlmostEqual(full["terms"]["worker"], 1.5 * 0.2 * (5000 + 100000 + 10000))
        self.assertAlmostEqual(full["delegated"], 500 + 34500 + 3000 + 10000 + 700)
        with self.assertRaises(orch.OrchestrateError):
            orch.estimate(root_read=1, reread=1.5, price_ratio=0.2, startup=0, verify=0)

    def test_cli_prints_verdict_arithmetic_and_checklist(self):
        done = subprocess.run([sys.executable, "-m", "context_layer.cli", "job", "estimate",
                               "--root-read-tokens", "40000", "--reread-fraction", "0.9",
                               "--worker-price-ratio", "0.2", "--startup-tokens", "5000",
                               "--verify-tokens", "2000"], cwd=REPO, capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("DO IT YOURSELF"))
        self.assertIn("estimate", done.stdout)
        self.assertIn("80%", done.stdout)
        self.assertIn("Authorship (mandatory)", done.stdout)


class Handoff(HandbackBase):

    def test_handoff_is_small_complete_and_refuses_overflow(self):
        self.hand_back(self.good())
        (self.out / "coverage.json").write_text(json.dumps(
            {"planned": ["a", "b"], "scanned": ["a", "b"], "excluded": [], "failed": [],
             "unprocessed": []}), encoding="utf-8")
        check = self.check(k=2, seed="s")
        result = orch.write_handoff(self.job_path, state="READY",
                                    done="Located budget and lead time records.",
                                    next_action="root reproduces E1 and E2 at source",
                                    not_established=["whether the budget is sufficient"],
                                    check=check)
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertLessEqual(orch.est_tokens(text), orch.HANDOFF_MAX_TOKENS)
        for needle in ("support-handoff/v1", "does NOT establish: whether the budget",
                       "not root approval", "next permitted action", "rule core",
                       "evidence.jsonl sha256", "input state: unchanged", "scanned 2",
                       "3 mechanically checked", "ledger head: no ledger yet"):
            self.assertIn(needle, text)
        with self.assertRaises(orch.OrchestrateError):
            orch.write_handoff(self.job_path, state="READY", done="x " * 2000, next_action="n",
                               not_established=["y"], out=self.out / "big.md")
        self.assertFalse((self.out / "big.md").exists())
        with self.assertRaises(orch.OrchestrateError):
            orch.write_handoff(self.job_path, state="BLOCKED", done="d", next_action="n",
                               not_established=["y"])
        with open(self.vault / "decisions/retrofit-budget.md", "a", encoding="utf-8") as handle:
            handle.write("Revised.\n")
        stale = orch.write_handoff(self.job_path, state="PARTIAL", done="d", next_action="n",
                                   not_established=["y"], blocker="source changed",
                                   out=self.out / "h2.md")
        self.assertTrue(stale["input_state"].startswith("INVALID"))

    def test_handback_record_is_a_ledger_line_the_handoff_names(self):
        self.hand_back(self.good())
        done = self.cli("handback", "check", str(self.out), "--job", str(self.job_path),
                        "--sample", "2", "--record", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        entries, problems = orch.read_ledger(self.vault)
        self.assertEqual(problems, [])
        line = entries[-1]["record"]
        self.assertEqual(line["event"], "handback")
        self.assertEqual(line["seed"], report["sample"]["seed"])
        self.assertEqual(line["sampled"], report["sample"]["ids"])
        self.assertEqual(line["verdict"], "ok")
        self.assertEqual(report["ledger"]["sha256"], entries[-1]["sha256"])
        # The same bytes and the recorded seed reproduce the recorded check.
        again = orch.check_handback(self.out, job_file=self.job_path, k=2, seed=line["seed"])
        self.assertEqual(orch.check_digest(again), line["check_sha256"])
        written = self.cli("handoff", "write", "--job", str(self.job_path), "--state", "READY",
                           "--done", "d", "--next", "n", "--not-established", "x")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertIn(f"ledger head line 1 sha256 {entries[-1]['sha256']}", written.stdout)
        handoff = (self.out / "handoff.md").read_text(encoding="utf-8")
        self.assertIn(f"ledger head: line 1 sha256 {entries[-1]['sha256']}", handoff)


# ---------------------------------------------------------------------------
# C-29: orchestration output carries no absolute path without --verbose
# ---------------------------------------------------------------------------

class NoAbsolutePaths(HandbackBase):

    def test_output_carries_no_absolute_path(self):
        self.hand_back(self.good())
        root = str(self.vault.parent)
        packet = self.packet()["id"]
        runs = [self.cli("packet", "build", str(self.vault), "--prompt", "lamp supplier"),
                self.cli("packet", "show", str(self.vault), packet),
                self.cli("packet", "show", str(self.vault.parent / "missing"), packet),
                self.cli("job", "validate", str(self.job_path)),
                self.cli("job", "new", str(self.vault), "--objective", "x", "--packet", packet,
                         "--task-id", "job-paths"),
                self.cli("handback", "check", str(self.out), "--job", str(self.job_path)),
                self.cli("handback", "check", str(self.out), "--job", str(self.job_path),
                         "--json"),
                self.cli("handoff", "write", "--job", str(self.job_path), "--state", "READY",
                         "--done", "d", "--next", "n", "--not-established", "x")]
        for done in runs:
            with self.subTest(argv=done.args[3:6]):
                self.assertNotIn(root, done.stdout + done.stderr)


# ---------------------------------------------------------------------------
# Dispatch through `tasks` (always runs: C-31)
# ---------------------------------------------------------------------------

WORKER = '''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
records = json.loads(@RECORDS@)
(out / "evidence.jsonl").write_text("".join(json.dumps(r) + "\\n" for r in records))
(out / "receipt.json").write_text(json.dumps({"schema": "support-receipt/v1",
    "task_id": "@TASK@", "attempt": @ATTEMPT@, "state": "READY",
    "counts": {"records": len(records), "scanned": 1, "excluded": 0, "failed": 0},
    "handoff": "handoff.md", "blocker": None}))
for name in @TOUCH@:
    with open(out / name, "a") as handle:
        handle.write("edited by the worker\\n")
print(json.dumps({"result": "wrote records", "is_error": False, "usage": {"input_tokens": 1,
    "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    "total_cost_usd": 0.0, "duration_ms": 1, "num_turns": 1}))
'''


class TasksIntegration(HandbackBase):

    def worker(self, records, attempt, task="job-test", touch=()):
        script = Path(self.temp.name) / f"worker-{task}-{attempt}.py"
        script.write_text(WORKER.replace("@RECORDS@", repr(json.dumps(records)))
                          .replace("@ATTEMPT@", str(attempt)).replace("@TASK@", task)
                          .replace("@TOUCH@", repr(list(touch))), encoding="utf-8")
        if os.name != "nt": script.chmod(0o755)
        return str(script)

    def tasks(self, script, *argv):
        env = dict(isolated_home_env(os.environ, str(self.vault.parent)), CONTEXT_LAYER_FAKE_BACKEND=script)
        return subprocess.run([sys.executable, "-m", "context_layer.cli", "tasks", *argv],
                              cwd=REPO, capture_output=True, text=True, env=env)

    def dispatch(self, job_path, script):
        made = self.tasks(script, "new", str(self.vault), "--job", str(job_path), "--json")
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        task_id = json.loads(made.stdout)["id"]
        ran = self.tasks(script, "run", str(self.vault), task_id)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        return task_id, self.tasks(script, "verify", str(self.vault), task_id)

    def test_the_integration_is_wired(self):
        from context_layer import tasks
        self.assertTrue(callable(getattr(tasks.orchestrate, "task_job", None)))
        self.assertTrue(callable(getattr(tasks.orchestrate, "task_handback", None)))

    def test_job_dispatch_verified_and_fabrication_rejected(self):
        good = self.good()[:2]
        task_id, verified = self.dispatch(self.job_path, self.worker(good, 1))
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        prompt = (self.vault / ".context/tasks" / task_id / "prompt.txt").read_text()
        self.assertIn("Worker rule core", prompt)
        self.assertIn("support-job/v1", prompt)
        packet = json.loads((self.vault / ".context/tasks" / task_id / "packet.json")
                            .read_text())
        self.assertEqual(packet["shared_packet_id"], self.job_data["initial_packet_id"])

        second = self.job(attempt=2)
        fake = dict(good[0], id="E9", span="a budget of 480,000 credits")
        _, rejected = self.dispatch(self.vault / second["job_path"],
                                    self.worker(good + [fake], 2))
        self.assertEqual(rejected.returncode, 1, rejected.stdout)
        self.assertIn("handback record E9", rejected.stdout)

    def test_new_with_packet_only(self):
        packet_id = self.job_data["initial_packet_id"]
        made = self.tasks("unused", "new", str(self.vault), "--goal", "budget", "--packet",
                          packet_id, "--json")
        self.assertEqual(made.returncode, 0, made.stderr)
        task_id = json.loads(made.stdout)["id"]
        packet = json.loads((self.vault / ".context/tasks" / task_id / "packet.json")
                            .read_text())
        self.assertEqual(packet["method"], "shared-packet")
        with open(self.vault / "decisions/retrofit-budget.md", "a", encoding="utf-8") as handle:
            handle.write("Changed.\n")
        stale = self.tasks("unused", "new", str(self.vault), "--goal", "budget", "--packet",
                           packet_id)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("withheld", stale.stderr)

    # -- C-03 / C-18 -------------------------------------------------------------

    def test_the_jobs_time_limit_bounds_the_task(self):
        limited = self.job(max_seconds=60, task_id="job-limited")
        made = self.tasks("unused", "new", str(self.vault), "--job",
                          str(self.vault / limited["job_path"]), "--json")
        self.assertEqual(made.returncode, 0, made.stderr)
        payload = json.loads(made.stdout)
        self.assertLessEqual(payload["timeout_s"], 60)
        task = json.loads((self.vault / ".context/tasks" / payload["id"] / "task.json")
                          .read_text())
        assert_private_path(self, self.vault / ".context/tasks" / payload["id"] / "task.json")
        self.assertEqual(task["max_elapsed_s"], 60)
        self.assertEqual(task["max_total_usage_tokens"], 20000)
        self.assertTrue(any("checked between attempts" in note for note in payload["notes"]))

    def test_a_file_root_is_its_own_source_glob(self):
        packet = self.packet(prompt="Lumen Works lamp supplier lead time")["id"]
        report = self.job(packet_id=packet, allowed_roots=["suppliers/lumen-works.md"],
                          task_id="job-file")
        self.assertEqual(report["status"], "OK", report)
        made = self.tasks("unused", "new", str(self.vault), "--job",
                          str(self.vault / report["job_path"]), "--json")
        self.assertEqual(made.returncode, 0, made.stderr)
        payload = json.loads(made.stdout)
        self.assertEqual(payload["evidence_sources"], ["suppliers/lumen-works.md"])

    def test_verify_rejects_a_modified_source(self):
        report = self.job(out="reports/job-m", task_id="job-mod")
        self.assertEqual(report["status"], "OK", report)
        job_path = self.vault / report["job_path"]
        script = self.worker(self.good()[:1], 1, task="job-mod", touch=["prior.md"])
        made = self.tasks(script, "new", str(self.vault), "--job", str(job_path), "--json")
        self.assertEqual(made.returncode, 0, made.stderr)
        task_id = json.loads(made.stdout)["id"]
        # A note appears in the output directory and is indexed after dispatch.
        (self.vault / "reports" / "job-m" / "prior.md").write_text("# Prior\n\nkept\n")
        self.assertEqual(self.cli("index", str(self.vault)).returncode, 0)
        self.assertEqual(self.tasks(script, "run", str(self.vault), task_id).returncode, 0)
        verified = self.tasks(script, "verify", str(self.vault), task_id)
        self.assertEqual(verified.returncode, 1, verified.stdout)
        self.assertIn("modified an indexed source: reports/job-m/prior.md", verified.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
