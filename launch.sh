#!/bin/bash
# DevPilot Launcher — kill old instance, start fresh
APP_DIR="$HOME/devpilot/.devpilot/app"
LOG="$HOME/devpilot/.devpilot/data/dashboard.log"
# stop the old instance politely first: it then stops the consoles and
# terminals it started (no port left busy); SIGKILL only if it hangs
/usr/bin/fuser -k -TERM 5555/tcp 2>/dev/null
for i in $(seq 1 20); do /usr/bin/fuser 5555/tcp >/dev/null 2>&1 || break; sleep 0.25; done
/usr/bin/fuser -k 5555/tcp 2>/dev/null
cd "$APP_DIR"
PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" dashboard.py >>"$LOG" 2>&1
