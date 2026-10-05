#!/bin/bash
# DevPilot — se mettre à jour depuis GitHub (branche main), sur n'importe quel PC.
#   devpilot update              → tu choisis la branche (★ main), dépendances, commande « devpilot », redémarrage
#   devpilot update --sans-redemarrer
# Les sessions persistantes (tmux) survivent au redémarrage.
set -u
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCHE=main
REDEMARRER=1
[ "${1:-}" = "--sans-redemarrer" ] && REDEMARRER=0
ok()  { echo "✓ $*"; }
ko()  { echo "✗ $*" >&2; exit 1; }
cd "$APP_DIR" || ko "dossier introuvable : $APP_DIR"
git rev-parse --git-dir >/dev/null 2>&1 || ko "$APP_DIR n'est pas un dépôt git"

# 1. rien de local ne doit se perdre
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no
  ko "modifications locales non publiées (ci-dessus). Que faire :
     publier  : devpilot publish \"ce que j'ai changé\"   (puis devpilot update ailleurs)
     garder   : git -C $APP_DIR stash      puis relance devpilot update
     jeter    : git -C $APP_DIR checkout -- .   (perdu pour de bon)"
fi

# 2. la branche de DevPilot : MONTRÉE et CHOISIE (★ main si rien n'est en cours) — plus de bascule
#    forcée, ni de pull à l'aveugle sur une vieille branche. Sans clavier (publish, tests) : le ★.
git fetch -q origin "$BRANCHE" || ko "GitHub injoignable (réseau ?) : git -C $APP_DIR fetch origin"
AVANT=$(git rev-parse HEAD)
bash "$APP_DIR/choisir-branches.sh" "$APP_DIR" || ko "DevPilot n'a pas pu être mis à jour sur la branche choisie (ci-dessus)"
APRES=$(git rev-parse HEAD)
if [ "$AVANT" = "$APRES" ]; then
  ok "déjà à jour ($(git log -1 --format='%h %s'))"
else
  echo "Nouveautés :"; git log --oneline --no-decorate "$AVANT..$APRES" 2>/dev/null | sed 's/^/  /' | head -20
  ok "à jour : $(git log -1 --format='%h %s')"
fi

# 3. dépendances
[ -x .venv/bin/python ] || python3 -m venv .venv || ko "python3 -m venv impossible (sudo apt install -y python3-venv)"
.venv/bin/pip install -q -r requirements.txt || ko "pip install a échoué : $APP_DIR/.venv/bin/pip install -r requirements.txt"
ok "dépendances"

# 4. la commande « devpilot » (avec « update ») sur ce PC
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/devpilot" << 'SCRIPT'
#!/bin/bash
APP_DIR="$HOME/devpilot/.devpilot/app"
case "${1:-}" in
  update|maj)        shift; exec "$APP_DIR/update.sh" "$@" ;;
  publish|publier)   shift; exec "$APP_DIR/publish.sh" "$@" ;;
  pc|assistant)      shift; exec "$APP_DIR/pc.sh" "$@" ;;
  effacer)           shift; PY="$APP_DIR/.venv/bin/python"; [ -x "$PY" ] || PY=python3
                     exec "$PY" "$APP_DIR/effacer.py" "$@" ;;
  version)           git -C "$APP_DIR" fetch -q origin main 2>/dev/null
                     echo "ce PC   : $(git -C "$APP_DIR" log -1 --format='%h %s (%cr)')"
                     echo "GitHub  : $(git -C "$APP_DIR" log -1 --format='%h %s (%cr)' origin/main)"
                     exit 0 ;;
  help|-h|--help)    echo "devpilot                 lancer DevPilot"
                     echo "devpilot pc              l'assistant : il dit quoi faire sur ce PC et le fait"
                     echo "devpilot publish \"msg\"   publier mes modifications (tests, PR, fusion) pour tous les PC"
                     echo "devpilot update          récupérer la dernière version (tu choisis la branche, ★ main)"
                     echo "devpilot version         ce PC comparé à GitHub"
                     echo "devpilot effacer         effacer de ce PC tout ce qui touche à DevPilot et aux projets (--simulation d'abord)"; exit 0 ;;
  "") ;;
  *)                 echo "✗ commande inconnue : devpilot $1"
                     echo "  les commandes : devpilot help — une commande récente manque ? devpilot update"; exit 2 ;;
esac
PY="$APP_DIR/.venv/bin/python"; [ -x "$PY" ] || PY=python3
cd "$APP_DIR" && exec "$PY" dashboard.py "$@"
SCRIPT
chmod +x "$HOME/.local/bin/devpilot"

# 5. redémarrage
if [ "$REDEMARRER" = 1 ]; then
  setsid ./launch.sh >/dev/null 2>&1 < /dev/null &
  for _ in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:5555/ && break; sleep 0.5; done
  curl -s -o /dev/null http://127.0.0.1:5555/ && ok "DevPilot redémarré : http://127.0.0.1:5555 (recharge la page)" \
    || ko "DevPilot ne répond pas : tail -n 30 ~/devpilot/.devpilot/data/dashboard.log"
fi
