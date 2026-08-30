#!/usr/bin/env bash
# Shared helper: fingerprint the worker build context.
#
# `make install` writes this fingerprint to ~/spielbot-worker/.build-stamp on
# each host after a successful build. `make start` compares it against the repo
# and re-deploys any host whose image no longer matches — without it a worker
# keeps running the image it was installed with, so a custom node added to the
# Dockerfile later never reaches it and every render needing that node fails
# with "the node 'X' is not installed on this worker".

WORKER_STAMP="spielbot-worker/.build-stamp"   # relative to the host's $HOME

# Print a hash over everything that goes into the worker images: all of docker/
# (Dockerfiles, compose, the node sources this repo ships) plus whatever the
# Dockerfiles COPY from elsewhere in the repo. The COPY sources are discovered
# rather than listed, so adding one does not need an edit here.
# docker/.env is excluded — it is written per host at deploy time.
build_stamp() {
    local root="$1"
    (
        cd "$root" || return 1
        {
            find docker -type f ! -name '.env' | LC_ALL=C sort | tr '\n' '\0' | xargs -0 shasum -a 256
            grep -hE '^COPY[[:space:]]' docker/*/Dockerfile \
                | awk '{for (i = 2; i < NF; i++) if ($i !~ /^--/) print $i}' \
                | LC_ALL=C sort -u \
                | while read -r src; do
                      [[ -f "$src" ]] && shasum -a 256 "$src"
                  done
            # The deploy script writes docker/.env (COMFYUI_REF, base image, GPU
            # mode), so a change to it changes the built image too.
            shasum -a 256 scripts/install_worker_container.sh
        } | shasum -a 256 | awk '{print $1}'
    )
}
