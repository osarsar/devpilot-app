#!/bin/bash
# DevPilot — Installation Script
# Installs DevPilot to ~/devpilot/.devpilot/

set -e

DEVPILOT_ROOT="$HOME/devpilot"
INSTALL_DIR="$DEVPILOT_ROOT/.devpilot"
APP_DIR="$INSTALL_DIR/app"
DATA_DIR="$INSTALL_DIR/data"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║       DevPilot — Installation        ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# Create structure
echo "  [1/6] Creation de la structure..."
mkdir -p "$APP_DIR/templates" "$APP_DIR/static" "$DATA_DIR"

# Copy app files (if running from source dir)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/app/dashboard.py" ]; then
    echo "  [2/6] Copie des fichiers..."
    cp "$SCRIPT_DIR/app/"*.py "$APP_DIR/"
    cp "$SCRIPT_DIR/app/templates/"* "$APP_DIR/templates/" 2>/dev/null || true
    cp -r "$SCRIPT_DIR/app/static/"* "$APP_DIR/static/" 2>/dev/null || true
    cp "$SCRIPT_DIR/app/icon.svg" "$APP_DIR/" 2>/dev/null || true
else
    echo "  [2/6] Fichiers deja en place"
fi

# Make scripts executable
chmod +x "$APP_DIR/dashboard.py" "$APP_DIR/watcher.py" 2>/dev/null || true

# CLI symlinks
echo "  [3/6] Creation des commandes CLI..."
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/devpilot" << 'SCRIPT'
#!/bin/bash
cd "$HOME/devpilot/.devpilot/app" && python3 dashboard.py "$@"
SCRIPT
chmod +x "$HOME/.local/bin/devpilot"

cat > "$HOME/.local/bin/devpilot-watcher" << 'SCRIPT'
#!/bin/bash
cd "$HOME/devpilot/.devpilot/app" && python3 watcher.py "$@"
SCRIPT
chmod +x "$HOME/.local/bin/devpilot-watcher"

# PATH
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

# Python deps
echo "  [4/6] Installation des dependances Python..."
pip install flask flask-sock psutil watchdog --break-system-packages -q 2>/dev/null || pip install flask flask-sock psutil watchdog -q 2>/dev/null

# Systemd watcher service
echo "  [5/6] Configuration du service watcher..."
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/devpilot-watcher.service" << EOF
[Unit]
Description=DevPilot Watcher — File, Docker & Port monitor
After=network.target docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $APP_DIR/watcher.py
WorkingDirectory=$APP_DIR
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable devpilot-watcher.service
systemctl --user restart devpilot-watcher.service

# Initialize database
echo "  [6/6] Initialisation de la base de donnees..."
cd "$APP_DIR"
PYTHONPATH="$APP_DIR" python3 -c "import db; db.init_db(); print('  DB OK')"

# Desktop shortcut (uses launch.sh for clean port handling)
chmod +x "$APP_DIR/launch.sh"
cat > "$HOME/.local/share/applications/devpilot.desktop" << EOF
[Desktop Entry]
Name=DevPilot
Comment=Dev Project Manager
Exec=$APP_DIR/launch.sh
Icon=$APP_DIR/icon.svg
Terminal=false
Type=Application
Categories=Development;
Keywords=dev;project;manager;
EOF
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║      DevPilot installe !             ║"
echo "  ╚══════════════════════════════════════╝"
echo ""
echo "  Structure:"
echo "    ~/devpilot/                  Tes projets"
echo "    ~/devpilot/.devpilot/        Installation"
echo ""
echo "  Commandes:"
echo "    devpilot                     Lancer le dashboard"
echo "    devpilot-watcher             Lancer le watcher"
echo ""
echo "  Dashboard: http://localhost:5555"
echo ""
