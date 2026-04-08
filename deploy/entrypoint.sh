#!/bin/sh
set -e
# Render (and some other hosts) set PORT; our app reads WEBAPP_PORT.
export WEBAPP_PORT="${PORT:-${WEBAPP_PORT:-8080}}"
export WEBAPP_HOST="${WEBAPP_HOST:-0.0.0.0}"
exec python -m bot.main
