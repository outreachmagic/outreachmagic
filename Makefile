.PHONY: manifests release-check test

manifests:
	python3 scripts/generate_skill_manifest.py --all

release-check: manifests
	bash scripts/release-check.sh

test:
	bash scripts/run-tests.sh
