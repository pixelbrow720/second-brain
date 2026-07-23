.PHONY: contracts docs lint test check

contracts:
	./scripts/check-contracts.sh

docs:
	PYTHONPATH="$(CURDIR)/src" python3 -m second_brain.documentation_checks

lint:
	./scripts/lint.sh

test:
	./scripts/test.sh

check: contracts docs lint test
