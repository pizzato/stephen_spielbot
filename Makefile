CONF    ?= cluster.conf
SCRIPTS := scripts

.PHONY: install start stop status

## Install dependencies locally and on all cluster workers.
install:
	@bash $(SCRIPTS)/install.sh $(CONF)

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
	@echo "Usage: make [install|start|stop|status]"
	@echo ""
	@echo "  install   Install deps locally; install ComfyUI + F5-TTS on each worker in $(CONF)"
	@echo "  start     Start ComfyUI on all workers, then launch the Gradio app"
	@echo "  stop      Stop the Gradio app and ComfyUI on all workers"
	@echo "  status    Check health of the app and every ComfyUI worker"
	@echo ""
	@echo "  CONF=$(CONF)  (override with make start CONF=other.conf)"
