PYTHON ?= python3
export PYTHON

.PHONY: manifests release-check test setup-hooks

manifests:
	$(PYTHON) scripts/generate_skill_manifest.py --all

release-check: manifests
	bash scripts/release-check.sh

test:
	bash scripts/run-tests.sh

setup-hooks:
	bash scripts/setup-hooks.sh
