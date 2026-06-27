.PHONY: manifests release-check test setup-hooks

manifests:
	python3 scripts/generate_skill_manifest.py --all

release-check: manifests
	bash scripts/release-check.sh

test:
	bash scripts/run-tests.sh

setup-hooks:
	bash scripts/setup-hooks.sh
