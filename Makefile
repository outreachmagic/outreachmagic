.PHONY: manifests release-check test layer1

manifests:
	python3 scripts/generate_skill_manifest.py --all

release-check: manifests
	bash scripts/release-check.sh

test:
	bash scripts/run-tests.sh

layer1:
	bash scripts/dark-factory/run-pytest-gate.sh
