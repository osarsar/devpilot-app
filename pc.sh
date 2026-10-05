#!/bin/bash
# DevPilot — l'assistant de CE PC. Il regarde où en est le PC, propose le bon mode, et le fait.
#
#   Nouveau PC (rien d'installé) :
#     bash <(curl -fsSL https://raw.githubusercontent.com/osarsar/devpilot-app/main/pc.sh)
#   PC déjà installé :
#     devpilot pc
#
# Modes : installer · première mise à niveau · mettre à jour · publier mes modifications ·
#         préparer ce PC pour publier · voir l'état · lancer DevPilot
set -u
APP="${DEVPILOT_APP:-$HOME/devpilot/.devpilot/app}"
DEPOT="https://github.com/osarsar/devpilot-app.git"
INTERDITS="$HOME/.config/devpilot/interdits.txt"
V="\033[32m"; R="\033[31m"; J="\033[33m"; G="\033[1m"; D="\033[2m"; N="\033[0m"
[ -t 1 ] || { V=""; R=""; J=""; G=""; D=""; N=""; }

# les réponses viennent de l'entrée standard (le clavier avec « bash <(curl …) » ou « devpilot pc »)
exec 3<&0
demander() { local r; printf "%b" "$1" >&2; IFS= read -r r <&3 || r=""; echo "$r"; }
oui()      { local r; r=$(demander "$1 ${D}[O/n]${N} "); [[ ! "$r" =~ ^[nN] ]]; }
oui_non()  { local r; r=$(demander "$1 ${D}[o/N]${N} "); [[ "$r" =~ ^[oOyY] ]]; }      # défaut : NON
ok()       { echo -e "${V}✓${N} $*"; }
bof()      { echo -e "${J}!${N} $*"; }
ko()       { echo -e "${R}✗${N} $*"; }
titre()    { echo; echo -e "${G}$*${N}"; }

# ── l'état du PC ─────────────────────────────────────────────────────────────
etat() {
  INSTALLE=0; GIT=0; ANCIEN=0; BR=""; SALES=0; NOUV=0; RETARD="?"; AVANCE=0; ICI=""; LA=""
  [ -f "$APP/dashboard.py" ] && INSTALLE=1
  [ "$INSTALLE" = 1 ] && git -C "$APP" rev-parse --git-dir >/dev/null 2>&1 && GIT=1
  if [ "$GIT" = 1 ]; then
    BR=$(git -C "$APP" branch --show-current)
    SALES=$(git -C "$APP" status --porcelain --untracked-files=no | wc -l)      # code suivi, modifié
    NOUV=$(git -C "$APP" ls-files --others --exclude-standard | wc -l)            # fichiers jamais suivis
    if timeout 20 git -C "$APP" fetch -q origin main 2>/dev/null; then
      RETARD=$(git -C "$APP" rev-list --count HEAD..origin/main)
      AVANCE=$(git -C "$APP" rev-list --count origin/main..HEAD)
      LA=$(git -C "$APP" log -1 --format='%h %s' origin/main)
    fi
    ICI=$(git -C "$APP" log -1 --format='%h %s')
    [ -f "$APP/publish.sh" ] && grep -q publish "$HOME/.local/bin/devpilot" 2>/dev/null || ANCIEN=1
  fi
  GH=0; command -v gh >/dev/null && GH=1
  GHOK=0; [ "$GH" = 1 ] && gh auth status >/dev/null 2>&1 && GHOK=1
  SSHOK=0; timeout 15 ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com </dev/null 2>&1 | grep -q "successfully authenticated" && SSHOK=1
  LISTE=0; [ -f "$INTERDITS" ] && LISTE=1
  TOURNE=0; curl -s -o /dev/null --max-time 2 http://127.0.0.1:5555/ && TOURNE=1
  PRET=0; [ "$GHOK$SSHOK$LISTE" = 111 ] && PRET=1
}

afficher() {
  titre "DevPilot sur $(hostname -s)"
  if [ "$INSTALLE" = 0 ]; then
    bof "DevPilot n'est pas installé sur ce PC"; return
  fi
  [ "$GIT" = 1 ] || { ko "$APP existe mais n'est pas un dépôt git"; return; }
  echo -e "  ce PC   : ${ICI}  ${D}(branche $BR)${N}"
  [ -n "$LA" ] && echo -e "  GitHub  : ${LA}" || bof "GitHub injoignable (réseau ?)"
  [ "$RETARD" = "?" ] || { [ "$RETARD" -gt 0 ] && bof "$RETARD nouveauté(s) sur GitHub pas encore ici" || ok "à jour avec GitHub"; }
  [ "$SALES" -gt 0 ] && bof "$SALES fichier(s) modifié(s) ici, pas encore publiés"
  [ "$NOUV" -gt 0 ] && echo -e "  ${D}$NOUV fichier(s) nouveau(x) ici, jamais suivis — ils restent sur ce PC sauf si tu choisis de les publier${N}"
  [ "$AVANCE" -gt 0 ] && bof "$AVANCE commit(s) ici, pas encore publiés"
  [ "$ANCIEN" = 1 ] && bof "ancienne installation : il manque « devpilot publish / update »"
  [ "$PRET" = 1 ] && ok "prêt à publier (gh, clé SSH, liste des mots interdits)" \
                  || echo -e "  ${D}publier depuis ce PC : pas encore préparé (gh $( [ $GHOK = 1 ] && echo ok || echo non ) · clé SSH $( [ $SSHOK = 1 ] && echo ok || echo non ) · mots interdits $( [ $LISTE = 1 ] && echo ok || echo non ))${N}"
  [ "$TOURNE" = 1 ] && ok "DevPilot tourne : http://localhost:5555" || echo -e "  ${D}DevPilot n'est pas lancé${N}"
}

# ── les modes ───────────────────────────────────────────────────────────────
installer() {
  titre "Installation de DevPilot"
  local manque=""
  command -v git >/dev/null || manque="$manque git"
  command -v python3 >/dev/null || manque="$manque python3"
  python3 -c "import ensurepip" 2>/dev/null || manque="$manque python3-venv"
  command -v curl >/dev/null || manque="$manque curl"
  if [ -n "$manque" ]; then
    bof "il manque :$manque"
    oui "Les installer (sudo apt install -y$manque) ?" && sudo apt install -y $manque || { ko "installe-les puis relance"; return 1; }
  fi
  [ -e "$APP" ] && { ko "$APP existe déjà — choisis plutôt « mettre à jour »"; return 1; }
  git clone -q "$DEPOT" "$APP" && ok "code récupéré" || { ko "clone impossible (réseau ?)"; return 1; }
  bash "$APP/install.sh" || { ko "installation interrompue — voir ci-dessus"; return 1; }
  bash "$APP/update.sh" --sans-redemarrer >/dev/null 2>&1   # la commande « devpilot » complète
  ok "DevPilot installé"
  oui "Le lancer maintenant ?" && lancer
}

mise_a_niveau() {
  titre "Première mise à niveau"
  if [ "$SALES" -gt 0 ]; then
    git -C "$APP" status --short | head -10
    ko "des fichiers sont modifiés ici (ci-dessus) : rien n'est écrasé.
  Garde-les de côté :  git -C $APP stash   puis relance cet assistant"
    return 1
  fi
  git -C "$APP" switch -q main 2>/dev/null || git -C "$APP" switch -q -c main origin/main || return 1
  git -C "$APP" pull -q --ff-only origin main || { ko "main locale a divergé : git -C $APP log --oneline origin/main..main"; return 1; }
  bash "$APP/update.sh"
}

mettre_a_jour() { titre "Mise à jour"; bash "$APP/update.sh"; }

publier() {
  titre "Publier mes modifications"
  if [ "$PRET" = 0 ]; then
    bof "ce PC n'est pas encore prêt à publier"
    oui "Le préparer maintenant ?" && preparer || return 1
    etat </dev/null
    [ "$PRET" = 1 ] || return 1
  fi
  local opt=""
  if [ "$NOUV" -gt 0 ]; then
    echo "  Fichiers NOUVEAUX sur ce PC (jamais suivis) :"
    git -C "$APP" ls-files --others --exclude-standard | sed 's/^/    /' | head -20
    if oui_non "  Les publier aussi ? (sauvegardes, essais, fichiers de ce PC : NON — le dépôt est public)"; then opt="--avec-nouveaux"; else opt="--sans-nouveaux"; fi
    if [ "$opt" = "--sans-nouveaux" ] && [ "$SALES" = 0 ] && [ "$AVANCE" = 0 ]; then
      ok "rien d'autre à publier — ces fichiers restent sur ce PC"; return 0
    fi
  fi
  [ "$SALES" -gt 0 ] && { echo "  Modifications à publier :"; git -C "$APP" status --short --untracked-files=no | sed 's/^/    /' | head -15; }
  local msg
  msg=$(demander "Qu'as-tu changé ? (une phrase) : ")
  [ -n "$msg" ] || { ko "il faut une phrase"; return 1; }
  oui "Lancer les tests avant (2-3 min, recommandé) ?" || opt="$opt --sans-tests"
  bash "$APP/publish.sh" "$msg" $opt
}

preparer() {
  titre "Préparer ce PC pour publier"
  if [ "$GH" = 0 ]; then
    oui "Installer GitHub CLI (sudo apt install -y gh) ?" && sudo apt install -y gh || return 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    echo "  Connexion à ton compte GitHub (suis les instructions) :"
    gh auth login <&3 || return 1
  fi
  ok "gh connecté"
  if ! timeout 15 ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com </dev/null 2>&1 | grep -q "successfully authenticated"; then
    local cle="$HOME/.ssh/id_ed25519"
    [ -f "$cle" ] || { ssh-keygen -q -t ed25519 -N "" -C "$(whoami)@$(hostname -s)" -f "$cle" && ok "clé SSH créée"; }
    gh ssh-key add "$cle.pub" --title "$(hostname -s)" >/dev/null 2>&1 && ok "clé SSH ajoutée à GitHub"
    sleep 2
    timeout 15 ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com </dev/null 2>&1 | grep -q "successfully authenticated" \
      && ok "clé SSH acceptée par GitHub" || { ko "GitHub refuse encore la clé : ssh -T git@github.com"; return 1; }
  else
    ok "clé SSH acceptée par GitHub"
  fi
  if [ ! -f "$INTERDITS" ]; then
    echo "  Mots qui ne doivent JAMAIS partir sur GitHub (dépôt public) : vraies IP des serveurs,"
    echo "  domaines clients… Un par ligne, ligne vide pour finir (copie la liste d'un autre PC) :"
    mkdir -p "$(dirname "$INTERDITS")"
    echo "# Mots interdits sur le dépôt public DevPilot (un par ligne) — lu par devpilot publish" > "$INTERDITS"
    while :; do local m; m=$(demander "  > "); [ -n "$m" ] || break; echo "$m" >> "$INTERDITS"; done
    chmod 600 "$INTERDITS"; ok "liste enregistrée : $INTERDITS ($(grep -vc '^#' "$INTERDITS") mot(s))"
  else
    ok "liste des mots interdits présente ($(grep -v '^#' "$INTERDITS" | grep -c .) mot(s))"
  fi
  ok "ce PC peut publier :  devpilot publish \"…\""
}

lancer() {
  ( setsid bash "$APP/launch.sh" >/dev/null 2>&1 < /dev/null & )
  for _ in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:5555/ && break; sleep 0.5; done
  curl -s -o /dev/null http://127.0.0.1:5555/ && ok "DevPilot : http://localhost:5555" || ko "DevPilot ne répond pas : tail ~/devpilot/.devpilot/data/dashboard.log"
  command -v xdg-open >/dev/null && xdg-open http://localhost:5555 >/dev/null 2>&1 &
}

# ── menu ────────────────────────────────────────────────────────────────────
etat </dev/null
afficher
if [ "$INSTALLE" = 0 ]; then
  oui "Installer DevPilot sur ce PC ?" && installer; exit $?
fi
[ "$GIT" = 1 ] || exit 1

# ce que l'assistant recommande, selon l'état
if   [ "$ANCIEN" = 1 ];                               then REC=1
elif [ "$SALES" -gt 0 ] || [ "$AVANCE" -gt 0 ];       then REC=3
elif [ "$RETARD" != "?" ] && [ "$RETARD" -gt 0 ];     then REC=2
elif [ "$TOURNE" = 0 ];                               then REC=6
else                                                       REC=5
fi
titre "Que faire ?"
lib() { [ "$1" = "$REC" ] && echo -e "  ${G}$1) $2   ← recommandé${N}" || echo "  $1) $2"; }
lib 1 "Première mise à niveau (ancienne installation)"
lib 2 "Mettre à jour (récupérer ce qui a été publié)"
lib 3 "Publier mes modifications (pour les autres PC)"
lib 4 "Préparer ce PC pour publier (gh, clé SSH, mots interdits)"
lib 5 "Voir l'état seulement"
lib 6 "Lancer DevPilot"
echo "  q) Quitter"
C=$(demander "Ton choix [$REC] : "); C=${C:-$REC}
case "$C" in
  1) mise_a_niveau ;;
  2) mettre_a_jour ;;
  3) publier ;;
  4) preparer ;;
  5) : ;;
  6) lancer ;;
  q|Q) exit 0 ;;
  *) ko "choix inconnu : $C"; exit 1 ;;
esac
