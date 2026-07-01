# ──────────────────────────────────────────────────────────────
#  Resume build & preview
#  Requires: tectonic  (install once: sudo pacman -S tectonic)
#  PDF viewer: okular (already installed)
# ──────────────────────────────────────────────────────────────

RESUME   := varakumar_resume
TEX      := $(RESUME).tex
PDF      := $(RESUME).pdf
VIEWER   := okular
TECTONIC := tectonic

.PHONY: all build preview clean check

all: build

## Compile  →  varakumar_resume.pdf
build: $(PDF)

$(PDF): $(TEX)
	@echo "→ Compiling $(TEX) ..."
	$(TECTONIC) $(TEX)
	@echo "✓ Done: $(PDF)"

## Build then open in okular (live-reload on save if okular stays open)
preview: build
	@echo "→ Opening $(PDF) in $(VIEWER) ..."
	$(VIEWER) $(PDF) &

## Delete compiled output
clean:
	@rm -f $(PDF) $(RESUME).aux $(RESUME).log $(RESUME).out \
	               $(RESUME).toc $(RESUME).synctex.gz
	@echo "✓ Cleaned."

## Sanity-check: make sure tectonic is installed
check:
	@command -v $(TECTONIC) >/dev/null 2>&1 || \
	  { echo "✗  tectonic not found."; \
	    echo "   Run:  sudo pacman -S tectonic"; exit 1; }
	@echo "✓ tectonic is installed: $$($(TECTONIC) --version)"
	@command -v $(VIEWER) >/dev/null 2>&1 && \
	  echo "✓ viewer  is installed: $(VIEWER)" || \
	  echo "  (optional) install a viewer:  sudo pacman -S okular"
