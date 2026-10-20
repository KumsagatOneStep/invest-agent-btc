install:
	python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'

test:
	.venv/bin/pytest -q

check:
	.venv/bin/ruff check . && .venv/bin/pytest -q

.PHONY: install test check
