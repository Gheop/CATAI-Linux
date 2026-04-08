.PHONY: run lint fix build e2e

run:
	python3 -m catai_linux

lint:
	ruff check catai_linux/

fix:
	ruff check --fix catai_linux/

build:
	python3 -m build

e2e:
	python3 tests/e2e_test.py
