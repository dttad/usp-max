# Makefile — usp-max
#
# Single entry point for crawling, extracting, inspecting, and tearing
# down `usp-max` runs. Every recipe that touches the host filesystem goes
# through the same Docker image so the toolchain (zstd, lxml, Rust
# extension, ...) is identical everywhere.
#
# Common commands:
#   make help                  show this help
#   make build                 build the Docker image
#   make crawl URL=...         one-shot crawl
#   make serve                 run the web UI (paste-a-URL)
#   make web-build             build the React SPA (web/dist/)
#   make extract FILE=...      extract one batch file
#   make extract-all OUT=...   extract every batch under OUT/
#   make manifest OUT=...      pretty-print manifest.json
#   make clean OUT=...         wipe output directories
#
# Examples:
#   make crawl URL=https://play.google.com/ OUT=./pg
#   make crawl URL=https://example.com OUT=./ex BATCH=50000 \
#         COMPRESS=zstd TAR=1
#   make serve HOST=0.0.0.0 PORT=8088
#   make extract FILE=./pg/urls-00001.txt.tar.zst OUT=./pg-txt
#   make extract-all OUT=./pg TO=./pg-txt

# ---------------------------------------------------------------------------
# Configuration (overridable on the command line)
# ---------------------------------------------------------------------------
IMAGE       ?= usp-max:1.9.0
URL         ?=
OUT         ?= ./out
TO          ?= ./extracted
BATCH       ?= 10000
COMPRESS    ?= none            # none | gz | zstd
TAR         ?= 0               # 0 | 1
CONCURRENCY  ?= 16
FANOUT_CAP  ?= 200
HOST         ?= 0.0.0.0        # web UI bind address
PORT         ?= 8088           # web UI bind port
PROXY        ?=                # HTTP/HTTPS/SOCKS5 proxy URL for crawl/serve

# Auto-derived constants
ABS_OUT := $(abspath $(OUT))
ABS_TO  := $(abspath $(TO))

# ---------------------------------------------------------------------------
# Self-documentation
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help

help: ## Show this help (default)
	@awk 'BEGIN {FS = ":.*##"; \
		printf "usp-max — high-performance sitemap crawler\n"; \
		printf "\nUsage: make <target> VAR=value\n\nTargets:\n"} \
		/^[a-zA-Z_-]+:.*?##/ \
		{printf "  \033[36m%-15s\033[0m  %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)
	@printf "\nVariables (with defaults):\n"
	@printf "  IMAGE       = \033[33m%s\033[0m\n" "$(IMAGE)"
	@printf "  URL         = \033[33m%s\033[0m  (required for crawl)\n" "$(URL)"
	@printf "  OUT         = \033[33m%s\033[0m\n" "$(OUT)"
	@printf "  TO          = \033[33m%s\033[0m  (extraction dir)\n" "$(TO)"
	@printf "  BATCH       = \033[33m%s\033[0m\n" "$(BATCH)"
	@printf "  COMPRESS    = \033[33m%s\033[0m  (none|gz|zstd)\n" "$(COMPRESS)"
	@printf "  TAR         = \033[33m%s\033[0m  (0|1)\n" "$(TAR)"
	@printf "  CONCURRENCY = \033[33m%s\033[0m\n" "$(CONCURRENCY)"
	@printf "  FANOUT_CAP  = \033[33m%s\033[0m\n" "$(FANOUT_CAP)"
	@printf "  HOST        = \033[33m%s\033[0m\n" "$(HOST)"
	@printf "  PORT        = \033[33m%s\033[0m\n" "$(PORT)"
	@printf "  PROXY       = \033[33m%s\033[0m\n" "$(PROXY)"

# ---------------------------------------------------------------------------
# Docker image
# ---------------------------------------------------------------------------
build: ## Build the Docker image
	docker build -t $(IMAGE) .

rebuild: ## Rebuild the Docker image from scratch (no cache)
	docker build --no-cache -t $(IMAGE) .

# ---------------------------------------------------------------------------
# Crawl — the main entry point
# ---------------------------------------------------------------------------
crawl: ## Crawl $(URL) -> batches under $(OUT). Required: URL=
	@if [ -z "$(URL)" ]; then \
		echo "Error: URL is required. Try:"; \
		echo "  make crawl URL=https://play.google.com/"; \
		echo "  make crawl URL=https://example.com OUT=./ex BATCH=50000 COMPRESS=zstd TAR=1"; \
		exit 1; \
	fi
	@mkdir -p $(ABS_OUT)
	@echo "→ crawl  $(URL)"
	@echo "  out=$(ABS_OUT) batch=$(BATCH) compress=$(COMPRESS) tar=$(TAR) concurrency=$(CONCURRENCY) fanout_cap=$(FANOUT_CAP)"
	docker run --rm --network host \
		-v $(ABS_OUT):/home/uspmax/out \
		$(IMAGE) \
		crawl "$(URL)" \
			-o /home/uspmax/out \
			--batch-size $(BATCH) \
			--concurrency $(CONCURRENCY) \
			--fanout-cap $(FANOUT_CAP) \
			--compress $(COMPRESS) \
			$(if $(TAR),--tar,)

# Convenience presets
crawl-fast: COMPRESS := none
crawl-fast: TAR := 0
crawl-fast: crawl ## Same as crawl (no compression, plain txt)

crawl-gz: COMPRESS := gz
crawl-gz: TAR := 0
crawl-gz: crawl ## gzipped txt batches

crawl-zstd: COMPRESS := zstd
crawl-zstd: TAR := 0
crawl-zstd: crawl ## zstd-compressed txt batches

crawl-tar-gz: COMPRESS := gz
crawl-tar-gz: TAR := 1
crawl-tar-gz: crawl ## tar.gz batches (good for archives)

crawl-tar-zstd: COMPRESS := zstd
crawl-tar-zstd: TAR := 1
crawl-tar-zstd: crawl ## tar.zst batches (smallest, recommended)

# ---------------------------------------------------------------------------
# Web UI — Starlette + React SPA
# ---------------------------------------------------------------------------
web-build: ## Build the React SPA into web/dist/ (needs node + npm)
	@if [ ! -d web/node_modules ]; then \
		echo "→ web/npm install"; \
		cd web && npm install --no-fund --no-audit; \
	fi
	cd web && npm run build
	@echo "✓ web/dist ready"

web-dev: ## Run the Vite dev server (proxies /api to a separately running serve)
	cd web && npm run dev

serve: ## Run the web UI server (HOST=0.0.0.0 PORT=8088 PROXY=…)
	@mkdir -p $(ABS_OUT)
	docker run --rm --network host \
		-v $(ABS_OUT):/home/uspmax/out \
		$(IMAGE) \
		serve --host $(HOST) --port $(PORT) --output-dir /home/uspmax/out \
			$(if $(PROXY),--proxy $(PROXY),)

# ---------------------------------------------------------------------------
# Extract — also via Docker so we don't need zstd/tar locally
# ---------------------------------------------------------------------------
extract: ## Extract every batch file under $(OUT) into $(TO)
	@if [ -z "$(OUT)" ]; then \
		echo "Error: OUT is required. Try:"; \
		echo "  make extract OUT=./out TO=./extracted"; \
		exit 1; \
	fi
	@mkdir -p $(ABS_TO)
	docker run --rm -v $(abspath $(OUT)):/home/uspmax/in:ro -v $(ABS_TO):/home/uspmax/out $(IMAGE) \
		extract /home/uspmax/in /home/uspmax/out

extract-one: ## Extract a single batch file (FILE=... [TO=./extracted])
	@if [ -z "$(FILE)" ]; then \
		echo "Error: FILE is required. Try:"; \
		echo "  make extract-one FILE=./out/urls-00001.txt.tar.zst TO=./txt"; \
		exit 1; \
	fi
	@mkdir -p $(ABS_TO)
	@tmpdir=$$(mktemp -d); \
	cp $(FILE) $$tmpdir/; \
	docker run --rm \
		-v $$tmpdir:/home/uspmax/in:ro \
		-v $(ABS_TO):/home/uspmax/out \
		$(IMAGE) \
		extract /home/uspmax/in /home/uspmax/out; \
	rm -rf $$tmpdir

extract-all: ## Extract every batch file under $(OUT) into $(TO)
	docker run --rm -v $(abspath $(OUT)):/home/uspmax/in:ro -v $(ABS_TO):/home/uspmax/out $(IMAGE) \
		extract /home/uspmax/in /home/uspmax/out

# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------
ls: ## List all batch files produced by the last crawl (OUT=)
	docker run --rm -v $(abspath $(OUT)):/home/uspmax/out $(IMAGE) ls /home/uspmax/out

wc: ## Count URLs in each batch file under $(OUT) (via docker, streaming)
	docker run --rm -v $(abspath $(OUT)):/home/uspmax/out $(IMAGE) wc /home/uspmax/out

manifest: ## Pretty-print the manifest.json from $(OUT)
	docker run --rm -v $(abspath $(OUT)):/home/uspmax/out $(IMAGE) manifest /home/uspmax/out

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
clean: ## Remove $(OUT) and $(TO)
	rm -rf $(OUT) $(TO)

clean-image: ## Remove the Docker image
	docker rmi -f $(IMAGE) || true

.PHONY: help build rebuild crawl crawl-fast crawl-gz crawl-zstd \
        crawl-tar-gz crawl-tar-zstd extract extract-one extract-all \
        manifest ls wc clean clean-image web-build web-dev serve