#!/usr/bin/env bash
# Gives the class server Meta's Muse Spark (Meta Model API, OpenAI-compatible).
# Asks for the Model API key; it is stored only in ~/class_tutor/meta.env
# (readable by this user alone) and never echoed.
set -eu
cd ~/class_tutor
read -s -p "Meta Model API key: " K; echo
# One tiny request to the documented Chat Completions endpoint.
code=$(printf 'header = "Authorization: Bearer %s"\n' "$K" |
  curl -s -o /tmp/meta-check.json -w '%{http_code}' -K - -H 'Content-Type: application/json' \
    -d '{"model":"muse-spark-1.3","messages":[{"role":"user","content":"Reply with: ok"}]}' \
    https://api.meta.ai/v1/chat/completions)
[ "$code" = 200 ] || head -c 300 /tmp/meta-check.json; echo; rm -f /tmp/meta-check.json
[ "$code" = 200 ] || { echo "Meta refused the key (HTTP $code); nothing changed."; exit 1; }
umask 077
printf 'TUTOR_BASE_URL=https://api.meta.ai/v1\nTUTOR_MODEL=muse-spark-1.3\nTUTOR_API_KEY=%s\n' "$K" > meta.env
~/run-classes.sh
