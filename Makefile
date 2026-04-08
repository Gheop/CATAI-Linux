.PHONY: run lint fix build

run:
	python3 -m catai_linux

lint:
	ruff check catai_linux/

fix:
	ruff check --fix catai_linux/

build:
	python3 -m build
