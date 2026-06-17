SCRIPTS := scripts

# Optional: scope start / stop / restart / status / logs to one worker's
# containers. Examples:  make stop W=s2   make restart W=s3   make status W=s1
W ?=

.PHONY: install download-models download-flux download-flux-cluster \
        start stop restart restart-server status logs worker-agent ui-worker help \
        web-install web-build web web-dev tailscale \
        launchd-install launchd-uninstall \
        lint lint-fix lint-web ensure-ruff

## Install everything: local deps, models, workers, config.yaml, AND the web UI
## (backend deps + React build). First run seeds config.yaml; set workers
## non-interactively: make install WORKERS="s1 s2 s3"
install:
	@WORKERS="$(WORKERS)" bash $(SCRIPTS)/install.sh

## Download LTX 2.3 and ACE-Step models only (skips already-present files). No FLUX.
download-models:
	@SKIP_FLUX=1 bash $(SCRIPTS)/download_models.sh

## Download FLUX.1-schnell models locally (~13 GB).
download-flux:
	@bash $(SCRIPTS)/download_models.sh

## Download FLUX.1-schnell models to the first cluster node, then rsync to all workers.
download-flux-cluster:
	@bash $(SCRIPTS)/download_flux_cluster.sh

## Start every worker's containers + the web app + UI worker(s).  Add W=<host> for one host.
start:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh start "$(W)"; \
	else \
	    bash $(SCRIPTS)/start.sh; \
	fi

## Stop the web app, UI worker(s), and every worker's containers.  Add W=<host> for one host.
stop:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh stop "$(W)"; \
	else \
	    bash $(SCRIPTS)/stop.sh; \
	fi

## Stop then start.  Add W=<host> to restart one host's containers only (app keeps running).
restart:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh restart "$(W)"; \
	else \
	    $(MAKE) --no-print-directory stop && $(MAKE) --no-print-directory start; \
	fi

## Install the web server as a macOS LaunchAgent — auto-starts on login and
## auto-restarts on crash. Also run by 'make install'. After this, make
## start/stop/restart/restart-server use launchd automatically.
launchd-install:
	@bash $(SCRIPTS)/launchd.sh install

## Remove the LaunchAgent service (reverts to the manual nohup approach).
launchd-uninstall:
	@bash $(SCRIPTS)/launchd.sh uninstall

## Restart only the web app (workers keep running — use after UI/code changes).
restart-server:
	@bash $(SCRIPTS)/stop_server.sh
	@bash $(SCRIPTS)/start_server.sh

## Show health of the app + every worker's containers.  Add W=<host> to check one host only.
status:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh status "$(W)"; \
	else \
	    bash $(SCRIPTS)/status.sh; \
	fi

## Tail one host's worker container logs over SSH.  Requires W=<host>.
logs:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh logs "$(W)"; \
	else \
	    echo "Usage: make logs W=<host>"; exit 1; \
	fi

## Run one durable worker agent. Override KIND and ENDPOINT, e.g. make worker-agent KIND=comfy ENDPOINT=http://s1:8188
worker-agent:
	@KIND="$${KIND:-comfy}"; \
	ENDPOINT="$${ENDPOINT:-http://localhost:8188}"; \
	.venv/bin/python worker_agent.py --kind "$$KIND" --endpoint "$$ENDPOINT"

## Start/stop the controller-side cover agent (cover-image regeneration).
## Started automatically by 'make start'; use this to (re)start it on its own.
## Usage: make ui-worker [ACT=start|stop|status]
ui-worker:
	@bash $(SCRIPTS)/ui_worker.sh "$${ACT:-start}"

# ── Web UI (React + FastAPI) — the app's only front-end ──
FRONTEND := webapp/frontend
WEB_PORT := 8001

## Install web UI deps: FastAPI backend (into .venv) + the React frontend (npm).
web-install:
	.venv/bin/pip install -r webapp/backend/requirements.txt
	cd $(FRONTEND) && npm install

## Build the React frontend to webapp/frontend/dist.
web-build:
	cd $(FRONTEND) && npm run build

## Build the SPA and serve the modern web UI + API from one process (localhost:8001).
web: web-build
	.venv/bin/python -m uvicorn webapp.backend.main:app --host 127.0.0.1 --port $(WEB_PORT)

## Dev mode: FastAPI (autoreload) + Vite dev server with /api proxy (localhost:5174). Ctrl-C stops both.
web-dev:
	@.venv/bin/python -m uvicorn webapp.backend.main:app --port $(WEB_PORT) --reload & \
	  cd $(FRONTEND) && npm run dev; \
	  kill %1 2>/dev/null || true

## Expose the web app to your other devices over Tailscale — tailnet-only HTTPS,
## no rebind (tailscaled proxies localhost:8001). Open the printed https URL on a
## device connected to your tailnet (e.g. your phone). Off: tailscale serve reset.
tailscale:
	@TS=$$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale); \
	if [ ! -x "$$TS" ]; then \
	    echo "Tailscale CLI not found. Install from https://tailscale.com/download, then run 'tailscale up'."; \
	    exit 1; \
	fi; \
	"$$TS" serve --bg $(WEB_PORT) && { \
	    echo ""; \
	    echo "Reachable from your Tailscale devices at the https URL shown above."; \
	    echo "Turn it off with:  tailscale serve reset"; \
	}

# ── Linting / dead-code ──
RUFF := .venv/bin/ruff
ensure-ruff:
	@$(RUFF) --version >/dev/null 2>&1 || .venv/bin/pip install -q -r requirements-dev.txt

## Lint Python with ruff (pyflakes + syntax errors). Auto-installs ruff on first run.
lint: ensure-ruff
	@$(RUFF) check .

## Auto-fix the Python lint issues ruff can fix safely (e.g. unused imports).
lint-fix: ensure-ruff
	@$(RUFF) check --fix .

## Hunt for dead frontend code (unused files / exports / deps) with knip.
## Needs the frontend deps — run 'make web-install' first.
lint-web:
	@cd $(FRONTEND) && npx --yes knip

help:
	@echo "Usage: make <target> [W=<worker>]"
	@echo ""
	@echo "  install         Install everything: deps, models, workers, web UI (backend+React build)"
	@echo "                  (first run seeds config.yaml — set hosts: make install WORKERS=\"s1 s2 s3\")"
	@echo "  download-models Download LTX 2.3 + ACE-Step models only (skips existing, no FLUX)"
	@echo "  download-flux          Download FLUX.1-schnell models locally (~13 GB)"
	@echo "  download-flux-cluster  Download FLUX models to first cluster node, rsync to all workers"
	@echo ""
	@echo "  start           Start every worker's containers + the web app + UI worker(s)"
	@echo "  stop            Stop the web app, UI worker(s), and every worker's containers"
	@echo "  restart         Stop everything, then start everything"
	@echo "  restart-server  Restart only the web app (workers keep running)"
	@echo "  status          Check health of the app, UI worker(s), and every worker container"
	@echo "  launchd-install   Install web server as a macOS LaunchAgent (auto-start/restart)"
	@echo "  launchd-uninstall Remove the LaunchAgent (reverts to manual nohup)"
	@echo ""
	@echo "  start/stop/restart/status/logs accept  W=<host>  to target one host's containers:"
	@echo "    make stop    W=s2       # stop s2's containers"
	@echo "    make restart W=s2       # restart s2's containers"
	@echo "    make status  W=s2       # check just s2"
	@echo "    make logs    W=s2       # tail s2's container logs"
	@echo ""
	@echo "  worker-agent    Run one durable worker agent (KIND=comfy|tts|local|ui ENDPOINT=...)"
	@echo "  ui-worker       Start/stop UI worker(s) for cover regen (ACT=start|stop|status,"
	@echo "                  endpoints from config.yaml ui_workers — started by 'make start' too)"
	@echo ""
	@echo "Workers run as Docker containers (see docker/README.md). 'make install' builds +"
	@echo "deploys them; start/stop/restart/status/logs manage them over SSH."
	@echo ""
	@echo "Web UI (React + FastAPI):"
	@echo "  web-install     Install web deps (FastAPI backend + React frontend)"
	@echo "  web             Build the SPA and serve UI + API (localhost:8001)"
	@echo "  web-dev         Dev mode: API + Vite dev server (localhost:5174)"
	@echo "  web-build       Build the React frontend to webapp/frontend/dist"
	@echo "  tailscale       Expose the web app to your Tailscale devices (tailnet-only HTTPS)"
	@echo ""
	@echo "Linting / dead-code:"
	@echo "  lint            Lint Python with ruff (unused imports/vars, undefined names)"
	@echo "  lint-fix        Auto-fix the lint issues ruff can fix safely"
	@echo "  lint-web        Hunt dead frontend code (unused files/exports/deps) with knip"
	@echo ""
	@echo "  Worker lists (comfy/tts/ui) live in ~/.config/video-generator/config.yaml"
	@echo "  — edit them in the Settings screen, or directly in that file."
