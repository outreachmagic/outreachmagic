PYTHON ?= python3
export PYTHON

.PHONY: manifests schema-reference release-check test lint setup-hooks

manifests: schema-reference
	$(PYTHON) scripts/generate_skill_manifest.py --all

schema-reference:
	$(PYTHON) skills/outreachmagic/scripts/generate_schema_reference.py

release-check: manifests
	bash scripts/release-check.sh

test:
	bash scripts/run-tests.sh

lint:
	$(PYTHON) -m ruff check --select F821 skills/outreachmagic/scripts/

setup-hooks:
	bash scripts/setup-hooks.sh
