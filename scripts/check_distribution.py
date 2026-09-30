#!/usr/bin/env python3
"""Install and smoke-test a built context-layer wheel outside its checkout.

    python3 scripts/check_distribution.py dist/context_layer-X.Y.Z-py3-none-any.whl [--no-sdist]

Five passes over one throwaway directory; the first failure stops the run with
"FAILED: ..." and a non-zero exit:

  contents       the wheel carries the runtime data it needs (the worker rule
                 core, the vault and starter-brain templates, every router
                 module) and its licence files under `*.dist-info/licenses/`;
                 the sdist beside it carries LICENSE, the retained upstream
                 notice, THIRD_PARTY.md, CREDITS.md, SECURITY.md, the docs they
                 point at (privacy, cli, host integration, ...) and templates/.
                 A missing sdist fails the check, unless --no-sdist says that
                 only the wheel is being checked.
  distribution   install the wheel into a fresh venv outside the checkout, then
                 version / first-run errors / init / index / search / route /
                 evaluation / comparison.
  0.2 walk       the components against that vault: status, shared memory,
                 one bounded task on the `fake` backend, host install and
                 uninstall, index (and link graph) rollback.
  0.3 walk       on fresh vaults: brain init and rules check; index and
                 search --method synaptic with the activation.json shape;
                 packet build, job new, and handback check catching a planted
                 fabricated quote; the MCP server's initialize and tools/list
                 over pipes; the Claude Code prompt hook on stdin.
  upgrade        a --force-reinstall of the same wheel, after which the
                 vault's .context state is still readable.

Nothing here reaches the network: pip installs with --no-index from the local
wheel. Nothing here touches the operator's home or settings: every child runs
with HOME inside the temporary directory, the check fails if anything wrote
there, and the host config is written into a temporary project directory that
is thrown away with it.
"""
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, shlex, subprocess, sys, tarfile, tempfile, venv, zipfile

TEST_HOME = None  # set once the temporary directory exists; every child inherits it
PATH_PREFIX = None  # the fresh venv's bin, so children see the installed console script
RUNNER_TOOLCHAIN_DIRS = {".rustup"}  # created by rustup itself, never by this package

def run(cmd, *, cwd=None, env=None, expect=(0,), path=None, stdin=None):
    e = dict(env or os.environ)
    e.pop("CONTEXT_LAYER_HOME", None); e.pop("PYTHONPATH", None)
    if TEST_HOME is not None:
        e["HOME"] = str(TEST_HOME)
        e["USERPROFILE"] = str(TEST_HOME)
        drive, tail = os.path.splitdrive(str(TEST_HOME))
        e["HOMEDRIVE"], e["HOMEPATH"] = drive, tail
    if path is not None: e["PATH"] = path
    elif PATH_PREFIX is not None: e["PATH"] = PATH_PREFIX + os.pathsep + e.get("PATH", "")
    p = subprocess.run(cmd, cwd=cwd, env=e, text=True, encoding="utf-8", capture_output=True, input=stdin,
                       timeout=600)
    if p.returncode not in expect:
        raise SystemExit(f"FAILED ({p.returncode}, wanted {expect}): {' '.join(map(str, cmd))}\n{p.stdout}\n{p.stderr}")
    return p.stdout

def check(condition, message):
    if not condition: raise SystemExit(f"FAILED: {message}")


def check_sdist(wheel, version, required=True):
    """The notices the licences require must be in the source distribution.

    The wheel carries no patch, so the only notice it owes is this project's own
    MIT text, which setuptools puts in `dist-info/licenses/LICENSE`. The sdist
    carries the upstream diffs, so it must also carry the Avenox notice. A
    missing sdist is a failure: a release that dropped a notice must not pass
    because the file that would show it was not there. `--no-sdist` (required
    False) is the explicit way to check a wheel alone.
    """
    sdist = wheel.parent / f"context_layer-{version}.tar.gz"
    if not sdist.is_file():
        if required:
            raise SystemExit(f"FAILED: no sdist at {sdist.name} beside the wheel; build both "
                             "with `python -m build`, or pass --no-sdist to check the wheel alone")
        print(f"note: --no-sdist: the notice check of {sdist.name} was not run", file=sys.stderr)
        return "not checked (--no-sdist)"
    with tarfile.open(sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    for required in SDIST_REQUIRED:
        check(required in names, f"sdist is missing {required}")
    check(any(name.startswith("templates/") and name.endswith(".md") for name in names),
          "sdist is missing templates/")
    check(not any(name.endswith((".sqlite", ".sqlite.prev")) or name.endswith("index-manifest.json")
                  for name in names), "the sdist carries derived index state")
    return "complete"


SDIST_REQUIRED = (
    "LICENSE", "README.md", "SCOPE.md", "CHANGELOG.md", "THIRD_PARTY.md", "CREDITS.md",
    "SECURITY.md", "retrieval-patches/LICENSE.upstream.txt", "retrieval-patches/README.md",
    "retrieval-patches/hook-visible-error.example.sh",
    "docs/host-integration.md", "docs/memory.md", "docs/tasks.md", "docs/source-lifecycle.md",
    "docs/privacy.md", "docs/cli.md", "docs/synapse.md", "docs/subagents.md",
    "docs/github-context.md",
    "context_layer/data/worker_core.md", "router/index_format.py", "router/source_policy.py",
)
WHEEL_REQUIRED = (
    "context_layer/data/worker_core.md", "context_layer/templates/vault/AGENTS.md",
    "context_layer/templates/vault/CLAUDE.md", "context_layer/router/source_policy.py",
    "context_layer/router/index_format.py", "context_layer/router/build_index.py",
    "context_layer/eval/retrieve.py",
    "context_layer/github_context.py", "context_layer/github_client.py",
    "context_layer/github_sources.py", "context_layer/github_cache.py",
    "context_layer/platform_support.py",
)
# pyproject `license-files`; setuptools copies each under *.dist-info/licenses/.
WHEEL_LICENSES = ("LICENSE", "THIRD_PARTY.md", "retrieval-patches/LICENSE.upstream.txt")


def check_wheel(wheel):
    """The wheel carries its runtime data and every licence file pyproject names."""
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for required in WHEEL_REQUIRED:
        check(required in names, f"wheel is missing {required}")
    check(any(name.startswith("context_layer/templates/starter-brain/") for name in names),
          "wheel is missing the starter-brain templates")
    licenses = {name.split(".dist-info/licenses/", 1)[1] for name in names
                if ".dist-info/licenses/" in name}
    for required in WHEEL_LICENSES:
        check(required in licenses, f"wheel dist-info/licenses/ is missing {required}")
    check(not any(name.endswith((".sqlite", ".sqlite.prev")) for name in names),
          "the wheel carries derived index state")
    return sorted(licenses)


FAKE_AGENT = '''#!{python}
"""Throwaway sub-agent for the smoke check: no model, no network, no vault."""
import json, os, pathlib, sys
prompt = sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "answer.md").write_text("cited %d sources\\n" % prompt.count("source_sha256"))
print(json.dumps({{"result": "wrote answer.md", "is_error": False,
                  "usage": {{"input_tokens": 100, "output_tokens": 20,
                            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
                  "total_cost_usd": 0.0, "duration_ms": 1, "num_turns": 1}}))
'''


def release_walk(cli, py, root, vault):
    """The 0.2 components, run against the installed console script only."""
    facts = {}

    # -- status: ok, then stale once a source the index does not cover appears --
    report = json.loads(run([str(cli), "status", str(vault), "--json"], cwd=root))
    check(report["overall"] == "ok", f"fresh index is not ok: {report['reasons']}")
    check(report["index"]["manifest"], "the build wrote no index-manifest.json")
    (vault / "notes").mkdir(exist_ok=True)
    (vault / "notes" / "budget.md").write_text("# Budget\n\nThe quarterly budget is confidential.\n",
                                               encoding="utf-8", newline="\n")
    report = json.loads(run([str(cli), "status", str(vault), "--json"], cwd=root, expect=(1,)))
    check(report["overall"] == "stale" and "notes/budget.md" in report["added"],
          f"an added source was not reported: {report}")
    run([str(cli), "index", str(vault)], cwd=root)                  # rebuild; keeps a .prev
    report = json.loads(run([str(cli), "status", str(vault), "--json"], cwd=root))
    check(report["overall"] == "ok" and report["index"]["rollback_available"],
          f"rebuild left no rollback point: {report}")
    facts["status"] = "ok -> stale on an added source -> ok after rebuild"

    # -- shared memory: record, deduplicate, resume, verify --
    style_sha = hashlib.sha256((vault / "style.md").read_bytes()).hexdigest()
    add = [str(cli), "memory", "add", str(vault), "--kind", "decision", "--state", "approved",
           "--text", "Search defaults to FTS.", "--source", "style.md", "--json"]
    first = json.loads(run(add, cwd=root))
    check(first["duplicate"] is False and first["sources"][0]["sha256"] == style_sha,
          f"the first record is wrong: {first}")
    again = json.loads(run(add, cwd=root))
    check(again["duplicate"] is True and again["id"] == first["id"],
          "the same record was appended twice")
    records = (vault / ".context" / "memory" / "records.jsonl").read_text(encoding="utf-8").strip().splitlines()
    check(len(records) == 1, f"{len(records)} lines for one recorded decision")
    packet = json.loads(run([str(cli), "memory", "resume", str(vault), "--json"], cwd=root))
    check(packet["records"][0]["id"] == first["id"] and packet["stale"] == [],
          f"resume did not return the record: {packet}")
    check("/" not in packet["vault"], f"resume printed a path: {packet['vault']}")
    run([str(cli), "memory", "verify", str(vault)], cwd=root)
    facts["memory"] = f"recorded {first['id']}, deduplicated, resumed, verified"

    # -- bounded task: the fake backend is a script this function writes --
    agent = root / "fake-agent.py"
    agent.write_text(FAKE_AGENT.format(python=py), encoding="utf-8", newline="\n")
    agent.chmod(0o755)
    environment = dict(os.environ, CONTEXT_LAYER_FAKE_BACKEND=str(agent))
    task = json.loads(run([str(cli), "tasks", "new", str(vault), "--goal", "style",
                           "--source", "style.md", "--backend", "fake", "--json"],
                          cwd=root, env=environment))
    task_id = task["id"]
    task_dir = vault / ".context" / "tasks" / task_id
    delivered = json.loads((task_dir / "packet.json").read_text(encoding="utf-8"))
    sources = {item["source_path"] for item in delivered.get("evidence", [])}
    check(sources <= {"style.md"}, f"the packet left the task's sources: {sources}")
    run([str(cli), "tasks", "run", str(vault), task_id], cwd=root, env=environment)
    state = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))["state"]
    check(state == "pending_review", f"a finished run is {state}, not pending_review")
    verdict = json.loads(run([str(cli), "tasks", "verify", str(vault), task_id, "--json"],
                             cwd=root, env=environment))
    verdict = verdict[0] if isinstance(verdict, list) else verdict
    check(verdict["state"] == "verified", f"verify said {verdict}")
    check((task_dir / "out" / "answer.md").is_file(), "the agent wrote no output file")
    facts["task"] = f"{task_id}: pending_review -> verified, output hashed"

    # -- host install and uninstall, in a throwaway project directory --
    project = root / "project"; project.mkdir()
    printed = run([str(cli), "install", "print", "generic", "--vault", str(vault)], cwd=root)
    entry = json.loads(printed)["mcpServers"]["context-layer"]
    # install sets PYTHONUTF8=1 for every host command (E-17); nothing else, and never PYTHONPATH
    check(entry.get("env", {}) == {"PYTHONUTF8": "1"} and "PYTHONPATH" not in printed,
          f"the installed console script exports an unexpected env: {entry}")
    check(pathlib.Path(entry["command"]).name in {"context-layer", "context-layer.exe"},
          f"the printed command is not the console script: {entry['command']}")
    check(json.loads(run([str(cli), "install", "generic", "--vault", str(vault)], cwd=root)) ==
          json.loads(printed), "`install generic` and `install print generic` disagree")
    # The contrast: called from a PATH that cannot resolve the console script, the
    # printed config falls back to `-m context_layer.cli` plus the PYTHONPATH that
    # makes it importable. That branch must still name the installed package.
    empty_path = root / "empty-path"
    empty_path.mkdir()
    hidden = json.loads(run([str(cli), "install", "print", "generic", "--vault", str(vault)],
                            cwd=root, path=str(empty_path)))["mcpServers"]["context-layer"]
    check(hidden.get("args", [])[:2] == ["-m", "context_layer.cli"]
          and "PYTHONPATH" in hidden.get("env", {}), f"the fallback launcher changed shape: {hidden}")
    run([str(cli), "install", "claude-code", "--vault", str(vault),
         "--project", str(project), "--hook"], cwd=root)            # dry run
    check(not list(project.iterdir()), "the dry run wrote something")
    run([str(cli), "install", "claude-code", "--vault", str(vault),
         "--project", str(project), "--hook", "--apply"], cwd=root)
    mcp = project / ".mcp.json"; settings = project / ".claude" / "settings.json"
    check(mcp.is_file() and settings.is_file(), "install --apply wrote no config")
    check("context-layer" in json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"], "no MCP entry was written")
    check("context-layer" in settings.read_text(encoding="utf-8"), "no prompt hook was written")
    run([str(cli), "uninstall", "claude-code", "--vault", str(vault),
         "--project", str(project), "--apply"], cwd=root)           # removes both, unasked
    leftovers = [path for path in (mcp, settings)
                 if path.is_file() and "context-layer" in path.read_text(encoding="utf-8")]
    check(not leftovers, f"uninstall left our keys in {leftovers}")
    facts["host"] = "generic printed without env; claude-code applied, then removed"

    # -- rollback, then back to a current index --
    live = vault / ".context" / "index.sqlite"
    graph = vault / ".context" / "graph.sqlite"
    check((vault / ".context" / "graph.sqlite.prev").is_file(),
          "the rebuild kept no graph.sqlite.prev")
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    graph_before = hashlib.sha256(graph.read_bytes()).hexdigest()
    run([str(cli), "rollback", str(vault), "--dry-run"], cwd=root)
    check(hashlib.sha256(live.read_bytes()).hexdigest() == before, "the dry run swapped the index")
    run([str(cli), "rollback", str(vault)], cwd=root)
    check(hashlib.sha256(live.read_bytes()).hexdigest() != before, "rollback restored nothing")
    check(hashlib.sha256(graph.read_bytes()).hexdigest() != graph_before,
          "rollback left the link graph of the newer build")
    report = json.loads(run([str(cli), "status", str(vault), "--json"], cwd=root, expect=(1,)))
    check(report["overall"] != "ok", "the restored older index is reported as healthy")
    run([str(cli), "index", str(vault)], cwd=root)
    facts["rollback"] = ("dry run inert; restore swapped the index and link graph, and "
                         "status said so")
    return facts


LINKED_NOTES = {
    "projects/lantern.md": "# Lantern Retrofit\n\nThe lantern retrofit has a budget of 48,000 "
                           "credits.\nLead: [[Iris Vale]]\n",
    "people/Iris Vale.md": "# Iris Vale\n\nIris Vale is on leave until 1 May.\n",
}


def walk_03(cli, root):
    """The 0.3 surface, from the installed console script only, on fresh vaults."""
    facts = {}

    # -- starter brain: the templates ship in the wheel, and the two rule files agree --
    brain = root / "brain"
    report = json.loads(run([str(cli), "brain", "init", str(brain), "--apply", "--json"], cwd=root))
    check(report.get("applied") is True, f"brain init --apply did not apply: {report}")
    check((brain / "CLAUDE.md").is_file() and (brain / "AGENTS.md").is_file(),
          "brain init wrote no rule files")
    checked = run([str(cli), "rules", "check", str(brain)], cwd=root)
    check("byte-identical" in checked, f"rules check did not confirm the rule files: {checked}")
    facts["brain"] = f"brain init --apply ({len(report.get('directories', []))} folders), rules check ok"

    # -- synaptic search over an explicit link, and the activation trace it writes --
    vault = root / "linked"
    for name, text in LINKED_NOTES.items():
        (vault / name).parent.mkdir(parents=True, exist_ok=True)
        (vault / name).write_text(text, encoding="utf-8", newline="\n")
    # Two notes are too few to infer a route: init writes routes.json and exits 1.
    run([str(cli), "init", str(vault)], cwd=root, expect=(0, 1))
    check((vault / ".context" / "routes.json").is_file(), "init wrote no routes.json")
    run([str(cli), "index", str(vault)], cwd=root)
    packet = json.loads(run([str(cli), "search", str(vault), "--prompt", "lantern retrofit lead",
                             "--method", "synaptic"], cwd=root))
    paths = [item["source_path"] for item in packet.get("evidence", [])]
    check(packet.get("operation_status") == "ok" and "people/Iris Vale.md" in paths,
          f"synaptic search did not follow the link to the linked note: {paths}")
    for item in packet["evidence"]:
        body = (vault / item["source_path"]).read_text(encoding="utf-8")
        check(item["content"] in body, f"synaptic content is not verbatim: {item['source_path']}")
        check(item["source_sha256"] == hashlib.sha256((vault / item["source_path"]).read_bytes())
              .hexdigest(), f"synaptic hash mismatch: {item['source_path']}")
    trace = json.loads((vault / ".context" / "activation.json").read_text(encoding="utf-8"))
    for key, kind in (("version", int), ("run_id", str), ("method", str), ("nodes", list),
                      ("edges", list), ("packet", dict)):
        check(isinstance(trace.get(key), kind), f"activation.json {key} is not {kind.__name__}: {trace}")
    check(trace["query"] is None, "activation.json stored the query text without --record-query")
    check(all({"path", "hop", "selected"} <= set(node) for node in trace["nodes"]),
          f"activation.json nodes changed shape: {trace['nodes'][:1]}")
    check(any(edge.get("to") == "people/Iris Vale.md" and "anchor" in edge for edge in trace["edges"]),
          f"activation.json has no anchored edge to the linked note: {trace['edges']}")
    facts["synaptic"] = f"{len(paths)} passages, {len(trace['nodes'])} trace nodes, query not stored"

    # -- a shared packet, a bounded job, and a hand-back with one fabricated quote --
    built = json.loads(run([str(cli), "packet", "build", str(vault), "--prompt", "lantern retrofit budget",
                            "--json"], cwd=root))
    job = json.loads(run([str(cli), "job", "new", str(vault), "--objective",
                          "Quote the lantern retrofit budget.", "--packet", built["id"],
                          "--root-goal", "Decide whether the retrofit is funded.",
                          "--allowed-root", "projects", "--acceptance",
                          "one record quotes the budget verbatim", "--stop-when",
                          "the budget has a record", "--task-id", "walk-1",
                          "--max-total-tokens", "20000", "--max-seconds", "600", "--json"], cwd=root))
    check(job.get("status") == "OK" and job.get("written") is True, f"job new was not written: {job}")
    job_path = vault / job["job_path"]
    out = job_path.parent / "out"
    out.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256((vault / "projects/lantern.md").read_bytes()).hexdigest()

    def record(rid, span):
        return {"id": rid, "observation": "the retrofit budget", "source_path": "projects/lantern.md",
                "source_sha256": sha, "line_start": 3, "line_end": 3, "span": span,
                "method": "read_source", "uncertainty": "none noted"}
    records = [record("E1", "a budget of 48,000 credits"), record("F1", "a budget of 480,000 credits")]
    (out / "evidence.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8", newline="\n")
    (out / "receipt.json").write_text(json.dumps({
        "schema": "support-receipt/v1", "task_id": "walk-1", "attempt": 1, "state": "READY",
        "counts": {"records": 2, "scanned": 1, "excluded": 0, "failed": 0},
        "handoff": "handoff.md", "blocker": None}), encoding="utf-8", newline="\n")
    verdict = json.loads(run([str(cli), "handback", "check", str(out), "--job", str(job_path), "--json"],
                             cwd=root, expect=(1,)))
    failed = {r["id"] for r in verdict.get("results", []) if not r.get("mechanically_checked")}
    check(failed == {"F1"} and verdict.get("mechanically_checked") == 1,
          f"handback check did not catch exactly the fabricated quote: {verdict.get('results')}")
    facts["handback"] = "packet build, job new OK, handback check caught the fabricated quote (exit 1)"

    # -- the MCP server over real pipes --
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "check_distribution", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "github_context", "arguments": {"prompt": "project setup"}}},
    ]
    served = run([str(cli), "mcp", "--vault", str(vault)], cwd=root,
                 stdin="".join(json.dumps(m) + "\n" for m in messages))
    replies = {reply.get("id"): reply for reply in map(json.loads, served.splitlines())}
    check(isinstance(replies.get(1, {}).get("result", {}).get("protocolVersion"), str),
          f"MCP initialize answered without a protocolVersion: {replies.get(1)}")
    tools = [tool["name"] for tool in replies.get(2, {}).get("result", {}).get("tools", [])]
    check({"search_vault", "read_source", "github_context"} <= set(tools),
          f"MCP tools/list is missing tools: {tools}")
    facts["mcp"] = f"initialize ok, tools/list: {len(tools)} tools"
    remote_result = replies.get(3, {}).get("result", {})
    check(not remote_result.get("isError", True), "unconfigured GitHub MCP call failed")
    check(json.loads(remote_result["content"][0]["text"])["status"] == "OFF",
          "GitHub MCP did not remain off without configuration")
    remote_cli = json.loads(run([str(cli), "github-context", str(vault),
                                 "--prompt", "project setup"], cwd=root))
    check(remote_cli["status"] == "OFF" and remote_cli["evidence"] == [],
          "GitHub CLI did not remain off without configuration")
    facts["github_context"] = "installed CLI and MCP: OFF without owner configuration"

    # -- the Claude Code prompt hook on stdin --
    hooked = json.loads(run([str(cli), "hook", "claude-code", "--vault", str(vault)], cwd=root,
                            stdin=json.dumps({"hook_event_name": "UserPromptSubmit",
                                              "prompt": "lantern retrofit budget"})))
    context = hooked.get("hookSpecificOutput", {}).get("additionalContext", "")
    check(hooked.get("hookSpecificOutput", {}).get("hookEventName") == "UserPromptSubmit"
          and "48,000 credits" in context and "data, not instructions" in context,
          f"the prompt hook did not inject the framed evidence: {context[:200]}")
    facts["hook"] = f"UserPromptSubmit context injected ({len(context)} characters)"
    return facts


def upgrade(cli, py, wheel, root, vault, task_id):
    """Reinstalling over a live install must not make the vault unreadable."""
    run([str(py), "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall",
         str(wheel)], cwd=root)
    version = run([str(cli), "--version"], cwd=root).strip()
    report = json.loads(run([str(cli), "status", str(vault), "--json"], cwd=root))
    check(report["overall"] == "ok", f"the vault is not ok after the upgrade: {report['reasons']}")
    run([str(cli), "memory", "verify", str(vault)], cwd=root)
    packet = json.loads(run([str(cli), "memory", "resume", str(vault), "--json"], cwd=root))
    check(packet["records"] and packet["stale"] == [], f"memory did not survive: {packet}")
    listing = run([str(cli), "tasks", "list", str(vault), "--json"], cwd=root)
    tasks = json.loads(listing)
    tasks = tasks["tasks"] if isinstance(tasks, dict) else tasks
    check(any(t["id"] == task_id and t["state"] == "verified" for t in tasks),
          f"the verified task did not survive: {listing}")
    search = json.loads(run([str(cli), "search", str(vault), "--prompt", "style"], cwd=root))
    check(search["operation_status"] == "ok" and search["evidence"], "search broke after the upgrade")
    return version


def walk_github(cli, root):
    """Exercise installed source/cache controls without any network access."""
    vault = root / "github-controls"
    vault.mkdir()
    def call(*args, expect=(0,)):
        return json.loads(run([str(cli), *args], cwd=root, expect=expect))

    sources = call("github-sources", "list", str(vault))
    check(sources["status"] == "OK" and not sources["enabled"] and not sources["sources"],
          f"GitHub sources were not off by default: {sources}")
    cache = call("github-cache", "status", str(vault))
    check(cache["status"] == "OK" and not cache["enabled"] and cache["entries"] == 0,
          f"GitHub cache was not off by default: {cache}")
    preview = call("github-cache", "enable", str(vault))
    check(preview["status"] == "DRY_RUN" and not (vault / ".context").exists(),
          "GitHub cache dry run wrote vault state")
    check(call("github-cache", "enable", str(vault), "--apply")["status"] == "OK",
          "GitHub cache enable failed")
    config = vault / ".context" / "github.json"
    config.write_text(json.dumps({"version": 1, "enabled": True, "sources": [
        {"id": "docs", "repo": "example/project", "commit": "1" * 40,
         "paths": ["README.md"], "keywords": ["setup"]}]}), encoding="utf-8", newline="\n")
    packet = call("github-context", str(vault), "--prompt", "setup", "--source", "docs",
                  "--offline", expect=(1,))
    check(packet["status"] == "ERROR" and "cache_miss" in packet["errors"],
          f"offline GitHub cache miss did not fail explicitly: {packet}")
    before = config.read_bytes()
    preview = call("github-sources", "remove", str(vault), "--id", "docs")
    check(preview["status"] == "DRY_RUN" and config.read_bytes() == before,
          "GitHub source removal dry run mutated the allowlist")
    removed = call("github-sources", "remove", str(vault), "--id", "docs", "--apply")
    check(removed["status"] == "OK" and not removed["config"]["sources"]
          and config.with_suffix(".json.bak").read_bytes() == before,
          "GitHub source removal did not preserve a backup")
    check(call("github-cache", "disable", str(vault), "--apply")["status"] == "OK",
          "GitHub cache disable failed")
    purged = call("github-cache", "purge", str(vault), "--apply")
    check(purged["status"] == "OK" and purged["purged"] == 0,
          "GitHub empty-cache purge failed")
    return "default off; dry runs inert; offline miss explicit; source backup; cache controls"


def main(argv=None) -> int:
    global TEST_HOME, PATH_PREFIX
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("wheel", help="The built context_layer-*.whl; its sdist is read beside it.")
    parser.add_argument("--no-sdist", action="store_true",
                        help="Check the wheel alone: do not fail when no sdist sits beside it.")
    args = parser.parse_args(argv)
    wheel = pathlib.Path(args.wheel).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl": raise SystemExit("wheel not found")
    version_tag = wheel.name.split("-")[1]
    with tempfile.TemporaryDirectory(prefix="context-layer-dist-") as td:
        root = pathlib.Path(td); envdir = root / "venv"; vault = root / "vault"; vault.mkdir()
        TEST_HOME = root / "home"; TEST_HOME.mkdir()
        (vault / "style.md").write_text("# Style\n\nKnown style text appears here.\n", encoding="utf-8", newline="\n")
        venv.EnvBuilder(with_pip=True).create(envdir)
        bindir = envdir / ("Scripts" if os.name == "nt" else "bin")
        py = bindir / ("python.exe" if os.name == "nt" else "python")
        cli = bindir / ("context-layer.exe" if os.name == "nt" else "context-layer")
        run([str(py), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], cwd=root)
        PATH_PREFIX = str(bindir)   # what an activated environment looks like
        version = run([str(cli), "--version"], cwd=root)
        check("context-layer" in version, f"--version printed {version.strip()!r}")
        check(version_tag in version, f"{version.strip()} is not the built {version_tag}")
        wheel_licenses = check_wheel(wheel)
        notices = check_sdist(wheel, version_tag, required=not args.no_sdist)
        # First run: search before init/index is one ERROR packet naming the next command.
        early = json.loads(run([str(cli), "search", str(vault), "--prompt", "style"], cwd=root,
                               expect=(1,)))
        check(early["status"] == "ERROR" and "context-layer init" in early["error"],
              f"the first-run search is not one ERROR naming init: {early}")
        run([str(cli), "init", str(vault)], cwd=root)
        run([str(cli), "index", str(vault)], cwd=root)
        search = json.loads(run([str(cli), "search", str(vault), "--prompt", "style"], cwd=root))
        check(search["schema"] == "evidence-delivery-v1" and search["operation_status"] == "ok",
              f"search did not return an ok evidence-delivery-v1 packet: {search}")
        check(search["evidence"][0]["content"] == (vault / "style.md").read_text(encoding="utf-8"),
              "search did not deliver the note's bytes")
        check(search["evidence"][0]["source_sha256"]
              == hashlib.sha256((vault / "style.md").read_bytes()).hexdigest(),
              "search delivered a hash that is not the note's")
        evidence = run([str(cli), "route", str(vault), "--evidence-json", "--no-save", "--prompt", "style"],
                       cwd=root)
        payload = json.loads(evidence)
        check(payload.get("schema") == "evidence-delivery-v1" and payload.get("evidence"),
              f"route --evidence-json delivered no evidence: {payload}")
        check("Known style text appears here." in evidence, "route did not deliver the note's text")
        check("errors" not in payload or not payload["errors"], f"route reported errors: {payload}")
        result = root / "eval.json"
        package_dir = pathlib.Path(run([str(py), "-c", "import context_layer; print(context_layer.__file__)"],
                                       cwd=root).strip()).parent
        eval_script = package_dir / "eval" / "evaluate.py"
        fixture = eval_script.parent / "stimulus-set.example.jsonl"
        demo = eval_script.parent / "fixtures" / "demo_router.py"
        run([str(py), str(eval_script), "--command", shlex.join([str(py), str(demo)]), "--stimuli",
             str(fixture), "--out", str(result)], cwd=root)
        summary = json.loads(result.read_text(encoding="utf-8"))["summary"]
        check(summary["n"] == 12 and summary["router_failures"] == 0,
              f"the bundled evaluation did not run all 12 prompts cleanly: {summary}")
        check(summary["total_expected"] == 17 and summary["total_hit"] == 15,
              f"the bundled evaluation gave {summary['total_hit']}/{summary['total_expected']}, not 15/17")
        check(package_dir.resolve().is_relative_to(envdir.resolve()),
              f"context_layer was imported from {package_dir}, not from the fresh venv")
        # Also exercise the bundled comparison data and installed eval CLI.
        run([str(cli), "eval", "--command", shlex.join([str(py), str(demo)]),
             "--stimuli", str(fixture), "--out", str(root / "cli-eval.json"), "--quiet"], cwd=root)
        comparison = root / "comparison"
        run([str(py), str(package_dir / "eval/compare.py"), "--out", str(comparison)], cwd=root)
        measured = json.loads((comparison / "summary.json").read_text(encoding="utf-8"))
        check(measured["results"]["fts"]["summary"]["total_hit"] == 25,
              f"the bundled comparison's fts arm delivered {measured['results']['fts']['summary']['total_hit']}"
              " groups, not 25")
        walk = release_walk(cli, py, root, vault)
        walk03 = walk_03(cli, root)
        github_walk = walk_github(cli, root)
        task_id = walk["task"].split(":")[0]
        upgraded = upgrade(cli, py, wheel, root, vault, task_id)
        # A CI runner's rustup proxies on PATH create ~/.rustup when a toolchain lookup
        # passes through them; nothing in this package names rustup or cargo. Anything
        # else in the temporary HOME is a write by a context-layer child.
        written = [p for p in TEST_HOME.iterdir() if p.name not in RUNNER_TOOLCHAIN_DIRS]
        check(written == [], f"a child wrote into the temporary HOME: {written}")
        print(json.dumps({"status": "pass", "version": version.strip(), "evaluation_prompts": summary["n"],
                          "fts_delivered_groups": 25, "installed_outside_checkout": True,
                          "sdist_notices_checked": notices, "wheel_licenses": wheel_licenses,
                          "release_walk": walk, "walk_0_3": walk03,
                          "github_controls": github_walk,
                          "upgrade": {"reinstalled": upgraded, "vault_state": "readable"}}, indent=2))
    return 0
if __name__ == "__main__": raise SystemExit(main())
