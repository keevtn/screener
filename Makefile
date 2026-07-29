# Market News Prediction Pipeline — task runner (docs/ROADMAP.md section 6)
# Windows: run via Git Bash (`make test`) or call the underlying python -m
# commands directly with .venv/Scripts/python.exe.

PY ?= .venv/Scripts/python.exe

.PHONY: test lint gate0 gate1 gate2 gate3 gate4 gate5 gate6 gate7

test:
	$(PY) -m coverage run -m pytest
	$(PY) -m coverage report --fail-under=75
# ROADMAP-NOTE: per-package 90% floors (common/, grade/, signal/) activate as
# those packages gain code:
#	$(PY) -m coverage report --include="src/pipeline/common/*,src/pipeline/grade/*,src/pipeline/signal/*" --fail-under=90

lint:
	$(PY) -m ruff check src tests scripts
	$(PY) -m ruff format --check src tests scripts

# Gate targets: each runs the full suite plus that gate's scripted checks.
# They are stubs until their phase's scripts exist; a stub failing loudly is
# better than a gate that silently passes.
gate0: test
	$(PY) scripts/gate0.py

gate7: test
	$(PY) scripts/gate7.py

gate1 gate2 gate3 gate4 gate5 gate6: test
	@echo "$@: not yet implemented" && exit 1
