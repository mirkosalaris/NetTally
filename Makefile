.PHONY: test lint format typecheck install

test:
	python3 -m unittest discover -s tests

install:
	./install.sh

# Ruff: fast Python linter (lint rules) + formatter (reflow/spacing).
lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

# Mypy: static type checking. Only analyzes annotated code by default, so the
# type regime is gradual: it checks what's typed and leaves untyped defs alone.
typecheck:
	mypy collector.py db.py config.py app_folder.py report.py html_generator.py tests