SHELL=/bin/bash

.DEFAULT_GOAL=_help

.PHONY: format
format:
	# TODO: if not $(f) return;
	prettier --prose-wrap always --print-width 80 --write $(f)

.PHONY: lint
lint:
	# TODO: if not $(f) return;
	markdownlint $(f)

.PHONY: pdf
pdf:
	# TODO: make f=/path/to/some.md  # outputs /path/to/some.pdf
