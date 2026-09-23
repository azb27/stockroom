.PHONY: data core test lint
data:
	python scripts/fetch_m5.py
	python -m stockroom.data.pipeline
core:
	python -m stockroom.data.pipeline --core
test:
	pytest -q
lint:
	ruff check src tests && ruff format --check src tests
