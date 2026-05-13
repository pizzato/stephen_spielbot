CONF    ?= cluster.conf
SCRIPTS := scripts

.PHONY: install download-models start stop status help

## Install deps locally + download models + install ComfyUI and F5-TTS on all cluster workers.
install:
	@bash $(SCRIPTS)/install.sh $(CONF)

## Download LTX 2.3 and ACE-Step models only (skips already-present files).
download-models:
	@bash $(SCRIPTS)/download_models.sh

## Start ComfyUI on all workers and the Gradio app locally.
start:
	@bash $(SCRIPTS)/start.sh $(CONF)

## Stop the Gradio app and ComfyUI on all workers.
stop:
	@bash $(SCRIPTS)/stop.sh $(CONF)

## Show health of the app and every ComfyUI worker.
status:
	@bash $(SCRIPTS)/status.sh $(CONF)

help:
	@echo "Usage: make [install|download-models|start|stop|status]"
	@echo ""
	@echo "  install         Install deps locally; download models; install workers in $(CONF)"
	@echo "  download-models Download LTX 2.3 + ACE-Step models only (skips existing)"
	@echo "  start           Start ComfyUI on all workers, then launch the Gradio app"
	@echo "  stop            Stop the Gradio app and ComfyUI on all workers"
	@echo "  status          Check health of the app and every ComfyUI worker"
	@echo ""
	@echo "  CONF=$(CONF)  (override with  make install CONF=other.conf)"
