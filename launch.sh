#!/bin/bash
# DevPilot Launcher — kill old instance, start fresh
fuser -k 5555/tcp 2>/dev/null
sleep 0.5
cd /home/osarsar/devpilot/.devpilot/app
exec python3 dashboard.py
