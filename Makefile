# Run everything from the project root, without remembering paths or which
# interpreter a script wants. `serve.py` is stdlib and takes the system python;
# everything else needs the venv built by `make venv`.

PY := .venv/bin/python
PORT ?= 8000

.PHONY: serve viewer pyramid venv help

## serve: run the viewer on port 8000 (make serve PORT=8080 to change)
serve:
	./scripts/serve.py --port $(PORT)

## viewer: rebuild the tile pyramid, then serve it
viewer: pyramid serve

## pyramid: re-cut tiles/ into web/tiles/ (after stitch.py, or a new re-render)
pyramid:
	$(PY) scripts/pyramid.py

## venv: create .venv and install requirements.txt
venv:
	python3 -m venv .venv
	$(PY) -m pip install -r requirements.txt

## help: list these targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
