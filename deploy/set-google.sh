#!/usr/bin/env bash
# Gives the class server "Sign in with Google" and Google Meet transcripts.
# Asks for the OAuth client (Google Cloud > Google Auth platform > Clients);
# the secret is typed hidden and stored only in ~/class_tutor/google.env
# (readable by this user alone).
set -eu
cd ~/class_tutor
read -p "Google OAuth client ID: " ID
read -s -p "Google OAuth client secret: " SECRET; echo
case "$ID" in *.apps.googleusercontent.com) ;; *) echo "That is not a client ID; nothing changed."; exit 1;; esac
umask 077
printf 'GOOGLE_CLIENT_ID=%s\nGOOGLE_CLIENT_SECRET=%s\nGOOGLE_REDIRECT_URI=https://passaly.com/classes/api/google/callback\n' \
  "$ID" "$SECRET" > google.env
~/run-classes.sh
