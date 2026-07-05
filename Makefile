PYTHON ?= python3
export PYTHON

.PHONY: manifests release-check test lint setup-hooks

manifests:
	$(PYTHON) scripts/generate_skill_manifest.py --all

release-check: manifests
	bash scripts/release-check.sh

test:
	bash scripts/run-tests.sh

lint:
	$(PYTHON) -m ruff check --select F821 skills/outreachmagic/scripts/

setup-hooks:
	bash scripts/setup-hooks.sh
