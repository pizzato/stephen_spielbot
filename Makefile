CONF    ?= cluster.conf
SCRIPTS := scripts

# Optional: scope start / stop / restart to a single worker.
# Examples:  make stop W=s2   make restart W=s3   make start W=s1
W ?=

.PHONY: install download-models download-flux download-flux-cluster \
        start stop restart restart-server status worker-agent help

## Install deps locally + download all models (LTX, ACE-Step, FLUX) + install workers.
install:
	@bash $(SCRIPTS)/install.sh $(CONF)

## Download LTX 2.3 and ACE-Step models only (skips already-present files). No FLUX.
download-models:
	@SKIP_FLUX=1 bash $(SCRIPTS)/download_models.sh

## Download FLUX.1-schnell models locally (~13 GB).
download-flux:
	@bash $(SCRIPTS)/download_models.sh

## Download FLUX.1-schnell models to the first cluster node, then rsync to all workers.
download-flux-cluster:
	@bash $(SCRIPTS)/download_flux_cluster.sh $(CONF)

## Start ComfyUI on all workers + the Gradio app.  Add W=<host> to start one worker only.
start:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh start "$(W)"; \
	else \
	    bash $(SCRIPTS)/start.sh $(CONF); \
	fi

## Stop the Gradio app and ComfyUI on all workers.  Add W=<host> to stop one worker only.
stop:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh stop "$(W)"; \
	else \
	    bash $(SCRIPTS)/stop.sh $(CONF); \
	fi

## Stop then start.  Add W=<host> to restart one worker only (app keeps running).
restart:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh restart "$(W)"; \
	else \
	    $(MAKE) --no-print-directory stop && $(MAKE) --no-print-directory start; \
	fi

## Restart only the Gradio app (workers keep running — use after UI/code changes).
restart-server:
	@bash $(SCRIPTS)/stop_server.sh
	@bash $(SCRIPTS)/start_server.sh

## Show health of the app and every worker.  Add W=<host> to check one worker only.
status:
	@if [ -n "$(W)" ]; then \
	    bash $(SCRIPTS)/worker.sh status "$(W)"; \
	else \
	    bash $(SCRIPTS)/status.sh $(CONF); \
	fi

## Run one durable worker agent. Override KIND and ENDPOINT, e.g. make worker-agent KIND=comfy ENDPOINT=http://s1:8188
worker-agent:
	@KIND="$${KIND:-comfy}"; \
	ENDPOINT="$${ENDPOINT:-http://localhost:8188}"; \
	.venv/bin/python worker_agent.py --kind "$$KIND" --endpoint "$$ENDPOINT"

help:
	@echo "Usage: make <target> [W=<worker>] [CONF=<file>]"
	@echo ""
	@echo "  install         Install deps locally; download all models; install workers in CONF"
	@echo "  download-models Download LTX 2.3 + ACE-Step models only (skips existing, no FLUX)"
	@echo "  download-flux          Download FLUX.1-schnell models locally (~13 GB)"
	@echo "  download-flux-cluster  Download FLUX models to first cluster node, rsync to all workers"
	@echo ""
	@echo "  start           Start ComfyUI on all workers + launch the Gradio app"
	@echo "  stop            Stop the Gradio app and ComfyUI on all workers"
	@echo "  restart         Stop everything, then start everything"
	@echo "  restart-server  Restart only the Gradio app (workers keep running)"
	@echo "  status          Check health of the app and every ComfyUI worker"
	@echo ""
	@echo "  start/stop/restart/status all accept  W=<host>  to target one worker:"
	@echo "    make stop    W=s2       # kill ComfyUI on s2"
	@echo "    make start   W=s2       # start ComfyUI on s2 and wait for it"
	@echo "    make restart W=s2       # stop + start ComfyUI on s2"
	@echo "    make status  W=s2       # check just s2"
	@echo ""
	@echo "  worker-agent    Run one durable worker agent (KIND=comfy|tts|local ENDPOINT=...)"
	@echo ""
	@echo "  CONF=$(CONF)  (override with  make install CONF=other.conf)"
