SHELL := /bin/sh

.DEFAULT_GOAL=_help

.PHONY: _help
_help:
	@printf '%s\n' 'Targets: format lint pdf (set f=... )'

define require_f
	@if [ -z "$(f)" ]; then echo "error: set f=FILE" >&2; exit 2; fi
endef

FILES := $(filter-out %.pdf,$(wildcard $(f)))
FILES_Q := $(foreach file,$(FILES),"$(file)")

.PHONY: format
format:
	$(require_f)
	prettier --prose-wrap always --print-width 80 --write -- $(FILES_Q)

.PHONY: lint
lint:
	$(require_f)
	markdownlint -- $(FILES_Q)


GIT_COMMIT ?= $(shell git rev-parse --short=12 HEAD)
GIT_BRANCH ?= $(shell git branch --show-current)
TITLE ?= MSC0500: Adaptive Set Reconciliation via PinSketch
DATE ?= $(shell date --iso)

.PHONY: pdf
pdf:
	$(require_f)
	pandoc "$(f)" -o "$(f).pdf" \
	  --pdf-engine=xelatex \
	  -V geometry:margin=0.65in \
	  -V colorlinks=true \
	  -V urlcolor=blue \
	  -V linkcolor=blue \
	  -V pagestyle=empty \
	  -M title="$(TITLE)" \
	  -M subject="Compiled by Shane [$(DATE)]" \
	  -M keywords="commit $(GIT_COMMIT) (branch: $(GIT_BRANCH))"

# 	pandoc "$(f)" -o "$(f).pdf" \
# 		--pdf-engine=xelatex \
# 		-V geometry:margin=0.65in \
# 		-V colorlinks=true \
# 		-V urlcolor=blue \
# 		-V linkcolor=blue \
# 		-V pagestyle=empty
# 	|| \
# 	pandoc "$(f)" -o "$(f).pdf" \
# 		--pdf-engine=lualatex \
# 		-V geometry:margin=0.65in \
# 		-V colorlinks=true \
# 		-V urlcolor=blue \
# 		-V linkcolor=blue \
# 		-V pagestyle=empty
