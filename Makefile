.PHONY: data core forecast test lint
data:
	python scripts/fetch_m5.py
	python -m stockroom.data.pipeline
forecast:
	python -m stockroom.forecast.train
core:
	python -m stockroom.data.pipeline --core
test:
	pytest -q
lint:
	ruff check src tests && ruff format --check src tests
