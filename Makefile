.PHONY: run lint fix

run:
	python3 catai.py

lint:
	ruff check catai.py

fix:
	ruff check --fix catai.py
