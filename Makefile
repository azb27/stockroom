.PHONY: data core forecast chat mcp web demo test lint
data:
	python scripts/fetch_m5.py
	python -m stockroom.data.pipeline
forecast:
	python -m stockroom.forecast.train
chat:
	python -m stockroom.agent --steps
web:
	cd web && npm ci && npm run build
demo: web
	stockroom-web
mcp:
	stockroom-mcp --http
core:
	python -m stockroom.data.pipeline --core
test:
	pytest -q
lint:
	ruff check src tests scripts evals && ruff format --check src tests scripts evals
