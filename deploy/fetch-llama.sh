#!/usr/bin/env bash
# Downloads Meta Llama 3.2 (gated on Hugging Face) for the Passaly Classes app.
# Asks for a Hugging Face read token; the token is never saved, and goes to
# curl on stdin so it never shows in the process list.
# Each file lands as .part and is published only when its SHA-256 matches
# index.json, so a phone can never fetch a half-written or wrong file.
set -u
cd ~/nginx/certbot/www/classes-models
exec 9>.fetch.lock
flock -n 9 || { echo "Already running in another window."; exit 1; }
read -s -p "Hugging Face token: " T; echo
get() {  # repo file [sha256]
  rm -f "$2.part"
  printf 'header = "Authorization: Bearer %s"\n' "$T" |
    curl -fL --progress-bar -K - -o "$2.part" "https://huggingface.co/litert-community/$1/resolve/main/$2" ||
    { rm -f "$2.part"; echo "  $2 FAILED (license not accepted for $1, or bad token)"; return; }
  if [ -n "${3:-}" ] && [ "$(sha256sum "$2.part" | cut -d' ' -f1)" != "$3" ]; then
    rm -f "$2.part"; echo "  $2 FAILED: checksum does not match"; return
  fi
  mv "$2.part" "$2"; echo "  $2 ok"
}
sha() { python3 -c 'import json,sys; print(next(m["sha256"] for m in json.load(open("index.json"))["models"] if m["file"] == sys.argv[1]))' "$1"; }
for f in llama3_2_1b_mixed_int4_gpu.litertlm llama3_2_3b_mixed_int4_gpu.litertlm; do
  want=$(sha "$f")
  [ -f "$f" ] && [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$want" ] && { echo "  $f already ok"; continue; }
  rm -f "$f"
  case $f in *1b*) r=Llama-3.2-1B;; *) r=Llama-3.2-3B;; esac
  get $r "$f" "$want"
done
get Llama-3.2-1B LICENSE.txt
get Llama-3.2-1B NOTICE.txt
ls -l
