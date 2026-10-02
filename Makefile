.PHONY: demo test lint

demo:  ## Run the full incident demo
	python -m dataops_agent

test:
	pytest -q

lint:
	ruff check .
