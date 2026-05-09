# Common project workflows wrapped in handy make targets.

UV ?= uv
PACKAGE ?= nexau

.PHONY: install lint format format-check typecheck mypy mypy-coverage pyright test ci gen-llm-logging-data

install:
	$(UV) sync
	$(UV) run pre-commit install

lint:
	$(UV) run ruff check . --fix

format:
	$(UV) run ruff format

format-check:
	$(UV) run ruff format --check

typecheck: mypy pyright

mypy:
	$(UV) run mypy --config-file pyproject.toml .

mypy-coverage:
	$(UV) run mypy --config-file pyproject.toml . --cobertura-xml-report mypy_reports/type_cobertura --html-report mypy_reports/type_html

pyright:
	$(UV) run pyright

test:
	$(UV) run pytest -n auto --dist loadfile --cov=$(PACKAGE) --cov-report=xml --cov-report=html --cov-report=term --timeout=120 -v --tb=short -m "not live_nightly and not llm"

# Nightly drift-detection run: every test that talks to a live LLM
# (``llm`` marker) plus the explicit cross-provider matrix / token-usage
# matrix etc. (``live_nightly``). Run from .github/workflows/nightly.yml;
# not part of ``make test``.
#
# Why this split: ``llm``-marked tests fail flakily on the PR loop —
# real model calls have non-deterministic tool selection, occasional
# 500s from gateway endpoints, prompt-cache state that needs a warm
# window, etc. None of that should block a merge. They still run
# nightly so model-drift / wire-format change still surfaces, just
# decoupled from PR velocity.
test-nightly:
	$(UV) run pytest -n auto --dist loadfile --cov=$(PACKAGE) --cov-report=xml --cov-report=term --timeout=300 -v --tb=short -m "llm or live_nightly"

gen-llm-logging-data:
	$(UV) run python -m tests.scripts.generate_llm_aggregator_logging_data

ci: lint format-check typecheck test
