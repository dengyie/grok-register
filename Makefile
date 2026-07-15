# ai-register-machine — common developer targets
.PHONY: help test test-unit syntax doctor list core-list example clean

ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
PY   := uv run python

help:
	@echo "ai-register-machine"
	@echo "  make test        offline pytest (root + tests/)"
	@echo "  make test-unit   skeleton + layer unit tests only"
	@echo "  make syntax      bash -n + py_compile critical paths"
	@echo "  make doctor      local secret hygiene (never prints secrets)"
	@echo "  make list        hub help + core list"
	@echo "  make example     run examples/minimal_pipeline.py"
	@echo "  make clean       caches only"

test:
	$(PY) -m pytest -q

test-unit:
	$(PY) -m pytest -q tests/unit test_register_core_layers.py

syntax:
	bash -n register.sh
	bash -n providers/mimo/run-register.sh
	bash -n providers/mimo/smoke.sh
	bash -n providers/_template/run-register.sh
	bash -n scripts/setup_simple.sh
	bash -n scripts/doctor_secrets.sh
	$(PY) -m py_compile register_cli.py register_core/cli.py register_core/pipeline.py \
	  register_core/providers/registry.py register_core/util/secrets.py \
	  register_core/util/process.py register_core/verify/mimo_tts.py \
	  register_core/verify/grok_chat.py providers/mimo/inject_cpa_openai.py

doctor:
	bash scripts/doctor_secrets.sh || test $$? -eq 2

list:
	./register.sh help
	$(PY) -m register_core list

core-list:
	$(PY) -m register_core list

example:
	$(PY) examples/minimal_pipeline.py

clean:
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
