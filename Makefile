CONF    ?= cluster.conf
SCRIPTS := scripts

.PHONY: install download-models download-flux download-flux-cluster start stop restart restart-server status worker-agent help

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

## Start ComfyUI on all workers and the Gradio app locally.
start:
	@bash $(SCRIPTS)/start.sh $(CONF)

## Stop the Gradio app and ComfyUI on all workers.
stop:
	@bash $(SCRIPTS)/stop.sh $(CONF)

## Stop everything, then start everything.
restart: stop start

## Restart only the Gradio app (workers keep running — use after UI/code changes).
restart-server:
	@bash $(SCRIPTS)/stop_server.sh
	@bash $(SCRIPTS)/start_server.sh

## Show health of the app and every ComfyUI worker.
status:
	@bash $(SCRIPTS)/status.sh $(CONF)

## Run one durable worker agent. Override KIND and ENDPOINT, e.g. make worker-agent KIND=comfy ENDPOINT=http://s1:8188
worker-agent:
	@KIND="$${KIND:-comfy}"; \
	ENDPOINT="$${ENDPOINT:-http://localhost:8188}"; \
	.venv/bin/python worker_agent.py --kind "$$KIND" --endpoint "$$ENDPOINT"

help:
	@echo "Usage: make [install|download-models|start|stop|status|worker-agent]"
	@echo ""
	@echo "  install         Install deps locally; download all models (LTX+ACE+FLUX); install workers in $(CONF)"
	@echo "  download-models Download LTX 2.3 + ACE-Step models only (skips existing, no FLUX)"
	@echo "  download-flux          Download FLUX.1-schnell models locally (~13 GB)"
	@echo "  download-flux-cluster  Download FLUX models to first cluster node, rsync to all workers"
	@echo "  start           Start ComfyUI on all workers, then launch the Gradio app"
	@echo "  stop            Stop the Gradio app and ComfyUI on all workers"
	@echo "  restart         Stop everything, then start everything"
	@echo "  restart-server  Restart only the Gradio app (workers keep running)"
	@echo "  status          Check health of the app and every ComfyUI worker"
	@echo "  worker-agent    Run one durable worker agent (KIND=comfy|tts|local ENDPOINT=...)"
	@echo ""
	@echo "  CONF=$(CONF)  (override with  make install CONF=other.conf)"
