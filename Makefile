CONF    ?= cluster.conf
SCRIPTS := scripts

.PHONY: install download-models download-flux start stop restart restart-server status help

## Install deps locally + download models (LTX + ACE-Step, no FLUX) + install workers.
install:
	@SKIP_FLUX=1 bash $(SCRIPTS)/install.sh $(CONF)

## Download LTX 2.3 and ACE-Step models only (skips already-present files). No FLUX.
download-models:
	@SKIP_FLUX=1 bash $(SCRIPTS)/download_models.sh

## Download FLUX.1-schnell models for scene preview images (~13 GB, requires free disk space).
download-flux:
	@bash $(SCRIPTS)/download_models.sh

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

help:
	@echo "Usage: make [install|download-models|start|stop|status]"
	@echo ""
	@echo "  install         Install deps locally; download LTX+ACE models; install workers in $(CONF)"
	@echo "  download-models Download LTX 2.3 + ACE-Step models only (skips existing, no FLUX)"
	@echo "  download-flux   Download FLUX.1-schnell models for scene previews (~13 GB)"
	@echo "  start           Start ComfyUI on all workers, then launch the Gradio app"
	@echo "  stop            Stop the Gradio app and ComfyUI on all workers"
	@echo "  restart         Stop everything, then start everything"
	@echo "  restart-server  Restart only the Gradio app (workers keep running)"
	@echo "  status          Check health of the app and every ComfyUI worker"
	@echo ""
	@echo "  CONF=$(CONF)  (override with  make install CONF=other.conf)"
