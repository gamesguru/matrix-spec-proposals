SHELL := /bin/sh

.DEFAULT_GOAL=_help

.PHONY: _help
_help:
	@printf '%s\n' 'Targets: format lint pdf (set f=... )'

define require_f
	@if [ -z "$(f)" ]; then echo "error: set f=FILE" >&2; exit 2; fi; \
	if [ -z "$(FILES)" ]; then echo "error: file not found or invalid: $(f)" >&2; exit 2; fi
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


GIT_COMMIT ?= $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")
SOURCE_DATE_EPOCH ?= $(shell git show -s --format=%ct HEAD 2>/dev/null || date +%s)
DATE ?= $(shell perl -MPOSIX=strftime -e 'print strftime("%F", gmtime(shift))' $(SOURCE_DATE_EPOCH))
QPDF ?= qpdf
GPG ?= gpg
QDF_NORMALIZER ?= ../tools/normalize_pdf_qdf.pl

.PHONY: pdf
pdf:
	$(require_f)
	set -e; \
	title="$$(sed -n '/^# /{s/^# //p;q;}' "$(f)")"; \
	raw_pdf="$(f).tmp.raw.pdf"; \
	qdf_pdf="$(f).tmp.qdf.pdf"; \
	norm_pdf="$(f).tmp.norm.qdf.pdf"; \
	SOURCE_DATE_EPOCH="$(SOURCE_DATE_EPOCH)" FORCE_SOURCE_DATE=1 \
	pandoc "$(f)" -o "$$raw_pdf" \
	  --pdf-engine=xelatex \
	  -V geometry:margin=0.65in \
	  -V colorlinks=true \
	  -V urlcolor=blue \
	  -V linkcolor=blue \
	  -V pagestyle=empty \
	  -M title="$$title" \
	  -M subject="Compiled by Shane [$(DATE)]" \
	  -M keywords="commit $(GIT_COMMIT)"; \
	"$(QPDF)" --qdf --object-streams=disable "$$raw_pdf" "$$qdf_pdf"; \
	perl "$(QDF_NORMALIZER)" "$$qdf_pdf" "$$norm_pdf"; \
	"$(QPDF)" --deterministic-id --linearize "$$norm_pdf" "$(f).pdf"; \
	rm -f "$$raw_pdf" "$$qdf_pdf" "$$norm_pdf"

.PHONY: sign
sign:
	$(require_f)
	"$(GPG)" --batch --yes --armor --detach-sign --output "$(f).pdf.asc" "$(f).pdf"
