#!/bin/bash
# DevPilot — One-line setup
# Usage: curl -sL https://raw.githubusercontent.com/osarsar/devpilot-app/main/setup.sh | bash

set -e
git clone https://github.com/osarsar/devpilot-app.git ~/devpilot/.devpilot/app
bash ~/devpilot/.devpilot/app/install.sh
