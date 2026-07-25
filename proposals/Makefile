SHELL=/bin/bash

.DEFAULT_GOAL=_help

.PHONY: _help
_help:
	@printf '%s\n' 'Targets: format lint pdf (set f=... )'

define require_f
	@if [[ -z "$(f)" ]]; then echo "error: set f=FILE" >&2; exit 2; fi
endef

.PHONY: format
format:
	$(require_f)
	prettier --prose-wrap always --print-width 80 --write -- $(f)

.PHONY: lint
lint:
	$(require_f)
	markdownlint -- $(f)

.PHONY: pdf
pdf:
	$(require_f)
	pandoc $(f) -o $(f).pdf
