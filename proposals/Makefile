SHELL=/bin/bash

.DEFAULT_GOAL=_help

.PHONY: _help
_help:
	`@printf` '%s\n' 'Targets: format lint pdf'

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
