#!/usr/bin/env bash
# (Re)starts the shared memory: ai-memory (github.com/akitaonrails/ai-memory),
# private on the Docker network, no published port. Settings in aimem.env.
set -eu
cd ~/class_tutor
docker rm -f ai-memory >/dev/null 2>&1 || true
docker run -d --name ai-memory --network answer-net --restart unless-stopped \
  -v ai-memory-data:/data --env-file aimem.env \
  akitaonrails/ai-memory:3.2.10 serve --transport http --bind 0.0.0.0:49374 --enable-web >/dev/null
docker ps --filter name=ai-memory --format '{{.Names}} {{.Status}}'
