# One obvious way to run things. Every target is standard-library Python;
# nothing here downloads anything except `setup`, which installs this package
# itself (it has no runtime dependencies).

PYTHON ?= python3
VENV   ?= .venv
BIN     = $(VENV)/bin

# The code and the suites read and write UTF-8; without UTF-8 mode a non-UTF-8
# locale (Latin-1, say) makes several suites fail. Every target runs in UTF-8
# mode; `make test PYTHONUTF8=0` turns it off to reproduce a locale problem.
export PYTHONUTF8 = 1

.DEFAULT_GOAL := help
.PHONY: help setup test demo lint network-guard plugin plugin-test bench bench-check dist clean

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup:  ## Create .venv and install this package in editable mode
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e .
	@echo
	@echo "Done. Activate with:  source $(VENV)/bin/activate"
	@echo "Then:                 context-layer --help"

test:  ## Run all router, scanner, integrity, hardening and evaluation regressions
	$(PYTHON) router/build_index.py --vault router/example-vault
	$(PYTHON) router/test_context_router.py
	$(PYTHON) tests/test_vault_scan.py
	$(PYTHON) tests/test_integrity.py
	$(PYTHON) tests/test_incremental_index.py
	$(PYTHON) tests/test_name_fields.py
	$(PYTHON) tests/test_evidence_contract.py
	$(PYTHON) tests/test_boundaries.py
	$(PYTHON) tests/test_write_boundary.py
	$(PYTHON) tests/test_comparison.py
	$(PYTHON) tests/test_eval_adapters.py
	$(PYTHON) tests/test_release_tooling.py
	$(PYTHON) tests/test_doc_claims.py
	$(PYTHON) tests/test_docs_links.py
	$(PYTHON) tests/test_encoding.py
	$(PYTHON) tests/test_mcp_install.py
	$(PYTHON) tests/test_memory.py
	$(PYTHON) tests/test_tasks.py
	$(PYTHON) tests/test_health.py
	$(PYTHON) tests/test_rules.py
	$(PYTHON) tests/test_brain.py
	$(PYTHON) tests/test_orchestrate.py
	$(PYTHON) tests/test_live_compare.py
	$(PYTHON) tests/test_harden.py
	$(PYTHON) tests/test_doctor.py
	$(PYTHON) tests/test_brief.py
	$(PYTHON) tests/test_session_evidence.py
	$(PYTHON) tests/test_session_show.py
	$(PYTHON) tests/test_jev_contracts.py
	$(PYTHON) tests/test_jev_client.py
	$(PYTHON) tests/test_jev.py
	$(PYTHON) tests/test_jev_answer.py
	$(PYTHON) tests/test_jev_memory.py
	$(PYTHON) tests/test_dev_jev.py
	$(PYTHON) tests/test_synapse.py
	$(PYTHON) tests/test_coactivation.py

demo:  ## End-to-end: init + index + one packet, on a throwaway copy of the fixture vault
	@rm -rf .demo-vault
	@cp -R eval/fixtures/docs .demo-vault
	$(PYTHON) -m context_layer.cli init .demo-vault
	$(PYTHON) -m context_layer.cli index .demo-vault
	$(PYTHON) -m context_layer.cli search .demo-vault \
	  --prompt "What does the style guide say about summaries?"
	@echo
	@echo "Demo vault left at .demo-vault/ so you can inspect .context/routes.json."
	@echo "Remove it with: make clean"

LINT_PATHS = context_layer router eval tests scripts bench obsidian-plugin/build.py

lint:  ## Byte-compile every Python file and run pyflakes (required in CI, optional locally)
	$(PYTHON) -m compileall -q $(LINT_PATHS)
	@if $(PYTHON) -c "import pyflakes" 2>/dev/null; then \
	  $(PYTHON) -m pyflakes $(LINT_PATHS); \
	elif [ -n "$$CI" ]; then \
	  echo "pyflakes is not installed, and CI requires it: pip install pyflakes"; \
	  exit 1; \
	else \
	  echo "pyflakes not installed; ran compileall only."; \
	  echo "For the full check: pip install pyflakes && make lint"; \
	fi

network-guard:  ## Fail if any module but context_layer/jev_client.py can reach the network
	$(PYTHON) scripts/check_network_surface.py

plugin:  ## Build the Obsidian plugin (plain JS, no npm) from obsidian-plugin/
	$(PYTHON) obsidian-plugin/build.py

plugin-test:  ## Run the Obsidian plugin's JS tests with Node
	node obsidian-plugin/tests/run-all.js

bench:  ## Run the offline retrieval benchmark on the fictional bench vault
	$(PYTHON) bench/run_offline.py

bench-check:  ## Seal check and the scorer/runner tests (two sealed cases, plumbing only)
	$(PYTHON) bench/seal.py check
	$(PYTHON) -m unittest bench/test_scorer.py bench/test_runner.py

dist:  ## Build the wheel and a reproducible sdist (needs `pip install build`)
	rm -rf dist
	SOURCE_DATE_EPOCH=$$(git log -1 --format=%ct) $(PYTHON) -m build
	SOURCE_DATE_EPOCH=$$(git log -1 --format=%ct) $(PYTHON) scripts/normalize_sdist.py dist/*.tar.gz

clean:  ## Remove build output, caches, the demo vault and generated indexes
	rm -rf .demo-vault build dist *.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf router/example-vault/.context-runs
