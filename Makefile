.PHONY: run run-test lint fix build test e2e

run:
	python3 -m catai_linux

run-test:
	python3 -m catai_linux --test-socket

lint:
	ruff check catai_linux/ tests/

fix:
	ruff check --fix catai_linux/ tests/

build:
	python3 -m build

test:
	python3 tests/test_modules.py

e2e:
	python3 tests/e2e_test.py
