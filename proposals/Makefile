SHELL=/bin/bash

.DEFAULT_GOAL=_help

# TODO: if not $(f) return;

.PHONY: format
format:
	prettier --prose-wrap always --print-width 80 --write $(f)

.PHONY: lint
lint:
	markdownlint $(f)

.PHONY: pdf
pdf:
	pandoc $(f) -o $(f).pdf
