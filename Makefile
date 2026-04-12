.PHONY: run run-test lint fix build test e2e messages release clean

run:
	python3 -m catai_linux.app

run-test:
	python3 -m catai_linux --test-socket

lint:
	ruff check catai_linux/ tests/

fix:
	ruff check --fix catai_linux/ tests/

test:
	python3 tests/test_modules.py

e2e:
	python3 tests/e2e_test.py

messages:
	@echo "Compiling gettext catalogs..."
	msgfmt catai_linux/locales/fr/LC_MESSAGES/catai.po -o catai_linux/locales/fr/LC_MESSAGES/catai.mo
	msgfmt catai_linux/locales/en/LC_MESSAGES/catai.po -o catai_linux/locales/en/LC_MESSAGES/catai.mo
	msgfmt catai_linux/locales/es/LC_MESSAGES/catai.po -o catai_linux/locales/es/LC_MESSAGES/catai.mo
	@echo "Done."

release:
	python3 -m build
	@echo "Wheel built. Tag + push to trigger PyPI publish."

build:
	python3 -m build

clean:
	rm -rf build/ dist/ *.egg-info catai_linux/__pycache__ tests/__pycache__
	find . -name "*.pyc" -delete
