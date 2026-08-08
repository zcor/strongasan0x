#!/bin/zsh
# Run Strong as an 0x management commands with this project's Mini runtime.
#
# Project-specific variables in ~/.zshenv must never override this .env. This
# wrapper is intentionally for the Mini checkout, where the shared venv lives.
# python-decouple reads .env itself; do not shell-source it because secrets may
# contain characters with shell meaning.
set -euo pipefail

project_root="${0:A:h:h}"
runtime_python="/Users/gerrithall/dev/ox/0xfitness/garmin_project/venv/bin/python"
runtime_env="${project_root}/.env"

if [[ ! -x "${runtime_python}" ]]; then
  print -u2 "Strong as an 0x runtime Python is unavailable: ${runtime_python}"
  exit 1
fi
if [[ ! -r "${runtime_env}" ]]; then
  print -u2 "Strong as an 0x runtime configuration is unavailable: ${runtime_env}"
  exit 1
fi

for variable in \
  X_CONSUMER_KEY X_CONSUMER_SECRET X_ACCESS_TOKEN X_ACCESS_TOKEN_SECRET \
  TELEGRAM_BOT_TOKEN TELEGRAM_BOT_USERNAME TELEGRAM_ATTESTATION_CHANNEL_ID; do
  unset "${variable}" || true
done

exec "${runtime_python}" "${project_root}/manage.py" "$@"
