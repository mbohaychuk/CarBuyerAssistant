# CarBuyerAssistant — developer convenience targets.
#
# Most work happens through `uv` directly (run, test, lint). The Makefile
# exists to hold the dashboard CSS build pipeline, which is the only
# non-Python build step in the project.

CSS_IN  = src/carbuyer/apps/dashboard/static/css/tailwind.css
CSS_OUT = src/carbuyer/apps/dashboard/static/css/app.css
TW      = bin/tailwindcss

.PHONY: css css-watch tailwind-install

# Compile the dashboard CSS once, minified. Run after editing tailwind.css,
# the @theme block, any component CSS file, or any template (Tailwind scans
# templates for utility classes via @source).
css: $(TW)
	$(TW) -i $(CSS_IN) -o $(CSS_OUT) --minify

# Long-running watcher for active development. Pair with `uv run python -m
# carbuyer.apps.dashboard` in another terminal; FastAPI re-reads templates
# per request but CSS only refreshes on recompile.
css-watch: $(TW)
	$(TW) -i $(CSS_IN) -o $(CSS_OUT) --watch

# Download the standalone binary if missing. Required after a fresh clone.
$(TW):
	bash scripts/install-tailwind.sh

tailwind-install:
	bash scripts/install-tailwind.sh
