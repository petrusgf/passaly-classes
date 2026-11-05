#!/usr/bin/env bash
# (Re)starts the class server with every private settings file that exists.
set -eu
cd ~/class_tutor
envs=""
for f in meta.env classes-mem.env google.env; do [ -f "$f" ] && envs="$envs --env-file $f"; done
docker rm -f classes >/dev/null 2>&1 || true
docker run -d --name classes --network answer-net --restart unless-stopped \
  -v classes_data:/data $envs class-tutor >/dev/null
sleep 3
curl -s https://passaly.com/classes/api/tutor; echo
