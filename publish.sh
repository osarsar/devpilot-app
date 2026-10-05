#!/bin/bash
# DevPilot — publier ce que j'ai modifié sur CE PC, pour tous les autres PC.
#
#   devpilot publish "ce que j'ai changé"           tests compris (~3 min)
#   devpilot publish "ce que j'ai changé" --sans-tests
#   Fichiers NOUVEAUX (jamais suivis) : rien ne part sans ton choix explicite —
#     --avec-nouveaux  les publier aussi      --sans-nouveaux  les laisser sur ce PC
#
# Fait, dans l'ordre, et s'arrête au premier problème SANS rien envoyer :
#   1. vérifie : pas de secret (le dépôt est PUBLIC), pas de marques de conflit
#   2. lance les tests
#   3. commite sur une branche (jamais directement sur main)
#   4. se met à jour avec GitHub (si un autre PC a publié entre-temps)
#   5. pousse, ouvre la PR, la fusionne dans main
#   6. remet ce PC sur le nouveau main et redémarre DevPilot
# Sur les autres PC ensuite : devpilot update
set -u
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ok()   { echo "✓ $*"; }
etape(){ echo "→ $*"; }
ko()   { echo "✗ $*" >&2; exit 1; }
cd "$APP_DIR" || ko "dossier introuvable : $APP_DIR"
git rev-parse --git-dir >/dev/null 2>&1 || ko "$APP_DIR n'est pas un dépôt git"

MSG=""; TESTS=1; REDEMARRER=1; NOUVEAUX=""
for a in "$@"; do
  case "$a" in
    --sans-tests) TESTS=0 ;;
    --sans-redemarrer) REDEMARRER=0 ;;
    --avec-nouveaux) NOUVEAUX=avec ;;
    --sans-nouveaux) NOUVEAUX=sans ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) ko "option inconnue : $a" ;;
    *) MSG="$a" ;;
  esac
done
[ -n "$MSG" ] || ko "dis ce que tu as changé :  devpilot publish \"Carte des branches : flèches plus lisibles\""

BASE=main
URL=$(git remote get-url origin)
REPO=$(echo "$URL" | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')
[ -n "$REPO" ] && [ "$REPO" != "$URL" ] || ko "dépôt GitHub introuvable (origin = $URL)"
# On LIT par https (public, aucun compte requis) ; on ÉCRIT par SSH (ta clé GitHub).
PUSH_URL="${DEVPILOT_PUSH_URL:-git@github.com:$REPO.git}"
command -v gh >/dev/null || ko "gh (GitHub CLI) absent :  sudo apt install -y gh  puis  gh auth login"
gh auth status >/dev/null 2>&1 || ko "gh n'est pas connecté à ton compte :  gh auth login"

# ── 0. y a-t-il quelque chose à publier ? ───────────────────────────────────
git fetch -q origin "$BASE" || ko "GitHub injoignable (réseau ?) : git -C $APP_DIR fetch origin"
BR=$(git branch --show-current)
[ -n "$BR" ] || ko "HEAD détaché — place-toi sur une branche :  git -C $APP_DIR switch $BASE"
[ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] || [ -f .git/MERGE_HEAD ] && \
  ko "une fusion / un rebase est en cours — termine-le ou :  git -C $APP_DIR merge --abort  /  rebase --abort"
# Fichiers NOUVEAUX : souvent des fichiers de CE PC (sauvegardes, essais, configs locales) —
# le dépôt est PUBLIC : rien de nouveau ne part sans un choix explicite.
NOUV=$(git ls-files --others --exclude-standard)
if [ -n "$NOUV" ] && [ -z "$NOUVEAUX" ]; then
  echo "$NOUV" | sed 's/^/  nouveau : /' | head -20
  ko "fichiers NOUVEAUX ci-dessus (jamais suivis par git). Choisis :
     les publier aussi        : devpilot publish \"$MSG\" --avec-nouveaux
     les laisser sur ce PC    : devpilot publish \"$MSG\" --sans-nouveaux
     (ou ajoute-les à .gitignore s'ils ne doivent jamais partir)"
fi
SALE=$(git status --porcelain --untracked-files=no)
[ "$NOUVEAUX" = avec ] && SALE="$SALE$NOUV"
EN_PLUS=$(git rev-list --count "origin/$BASE..HEAD")
if [ -z "$SALE" ] && [ "$EN_PLUS" = 0 ]; then
  ok "rien à publier : ce PC est identique à GitHub ($(git log -1 --format='%h %s'))"
  exit 0
fi

# ── 1. vérifications (avant tout commit) ───────────────────────────────────
etape "vérifications"
if [ "$NOUVEAUX" = avec ]; then git add -A; else git add -u; fi
DEPART=$(git merge-base HEAD "origin/$BASE")
annuler() { git reset -q; }
# marques de conflit : deux fois elles ont cassé un main (md_console #31, #34)
MARQUES=$(git grep --cached -lE '^(<<<<<<<|>>>>>>>)( |$)' -- . ':!*.md' 2>/dev/null)
[ -z "$MARQUES" ] || { annuler; ko "marques de conflit <<<<<<< / >>>>>>> dans : $MARQUES — corrige-les d'abord"; }
# secrets : le dépôt est PUBLIC — ce qui part est lisible par tous, pour toujours
AJOUTS=$(git diff --cached "$DEPART" -U0 | grep -E '^\+[^+]' || true)
FICHIERS=$(git diff --cached --name-only "$DEPART")
SECRETS=$(echo "$AJOUTS" | grep -nE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|xox[bp]-[A-Za-z0-9-]{10,}' | head -5)
SFIC=$(echo "$FICHIERS" | grep -E '(^|/)(\.env|.*\.pem|.*\.key|id_rsa|id_ed25519|.*\.sqlite3?|.*\.db)$|(^|/)data[^/]*/|avant-restore|\.bak($|[.-])|(^|/)backups?/|autounattend\.xml$' | grep -v '\.example$' | head -5)
# mots interdits PROPRES À TOI (vraies IP, domaines clients…) : jamais dans le dépôt, donc dans un
# fichier local — un par ligne. Ex. :  ~/.config/devpilot/interdits.txt
INTERDITS="${DEVPILOT_INTERDITS:-$HOME/.config/devpilot/interdits.txt}"
PRIVES=""
if [ -f "$INTERDITS" ]; then
  while IFS= read -r mot; do
    mot="${mot%%#*}"; mot="$(echo "$mot" | xargs)"; [ -n "$mot" ] || continue
    echo "$AJOUTS" | grep -qiF -- "$mot" && PRIVES="$PRIVES $mot"
  done < "$INTERDITS"
fi
if [ -n "$SECRETS$SFIC$PRIVES" ]; then
  annuler
  [ -n "$SECRETS" ] && echo "  clé / jeton : $(echo "$SECRETS" | cut -c1-90)"
  [ -n "$SFIC" ] && echo "  fichier sensible : $SFIC"
  [ -n "$PRIVES" ] && echo "  mot interdit (privé) :$PRIVES"
  ko "REFUS — le dépôt est public, rien n'a été envoyé. Retire ces éléments puis relance."
fi
ok "pas de secret, pas de marque de conflit"

# ── 2. tests ────────────────────────────────────────────────────────────────
if [ "$TESTS" = 1 ]; then
  etape "tests (2-3 min ; --sans-tests pour sauter)"
  if ! .venv/bin/python -m pytest -q -x -p no:cacheprovider > /tmp/devpilot-publish-tests.log 2>&1; then
    annuler
    tail -15 /tmp/devpilot-publish-tests.log
    ko "tests en échec — rien n'a été publié (journal : /tmp/devpilot-publish-tests.log)"
  fi
  ok "tests : $(tail -1 /tmp/devpilot-publish-tests.log | sed 's/=//g' | xargs)"
fi

# ── 3. commit sur une branche ───────────────────────────────────────────────
if [ "$BR" = "$BASE" ]; then
  BR="maj/$(date +%Y%m%d-%H%M%S)-$(hostname -s | tr -c 'a-zA-Z0-9-\n' '-')"
  git switch -q -c "$BR" || { annuler; ko "branche $BR impossible"; }
fi
if [ -n "$(git diff --cached --name-only)" ]; then
  git commit -q -m "$MSG" || ko "commit impossible"
fi
ok "commit sur la branche $BR"

# ── 4. à jour avec GitHub (un autre PC a pu publier entre-temps) ───────────
if ! git rebase -q "origin/$BASE" >/dev/null 2>&1; then
  git rebase --abort 2>/dev/null
  ko "ta modification touche les mêmes lignes qu'une publication faite depuis un autre PC.
  Rien n'a été envoyé ; ton travail est intact sur la branche $BR.
  Que faire :  git -C $APP_DIR rebase origin/$BASE   (corrige les fichiers en conflit,
               git add -A, git rebase --continue)  puis relance devpilot publish \"$MSG\""
fi

# ── 5. pousser, PR, fusion ──────────────────────────────────────────────────
etape "envoi sur GitHub"
git push -q --force-with-lease "$PUSH_URL" "HEAD:refs/heads/$BR" 2>/tmp/devpilot-publish-push.log || {
  cat /tmp/devpilot-publish-push.log >&2
  ko "push refusé. Ta clé SSH est-elle sur GitHub ?  ssh -T git@github.com
  (sinon :  ssh-keygen -t ed25519  puis  gh ssh-key add ~/.ssh/id_ed25519.pub)"
}
PR=$(gh pr list -R "$REPO" --head "$BR" --state open --json url -q '.[0].url' 2>/dev/null)
if [ -z "$PR" ]; then
  CORPS=$(printf "%s\n\nPublié depuis %s avec \`devpilot publish\`.\n\n%s" "$MSG" "$(hostname -s)" \
          "$(git log --format='- %s' "origin/$BASE..HEAD")")
  PR=$(gh pr create -R "$REPO" --base "$BASE" --head "$BR" --title "$MSG" --body "$CORPS" 2>&1 | tail -1)
  [[ "$PR" == https://* ]] || ko "PR impossible : $PR"
fi
ok "PR : $PR"
if ! gh pr merge "$PR" -R "$REPO" --squash --delete-branch >/tmp/devpilot-publish-merge.log 2>&1; then
  cat /tmp/devpilot-publish-merge.log >&2
  ko "fusion refusée par GitHub — la PR reste ouverte : $PR (fusionne-la sur GitHub, puis : devpilot update)"
fi
ok "fusionné dans $BASE"

# ── 6. ce PC sur le nouveau main ────────────────────────────────────────────
git fetch -q origin "$BASE"
git switch -q "$BASE" 2>/dev/null || git switch -q -c "$BASE" "origin/$BASE"
git merge -q --ff-only "origin/$BASE" || ko "$BASE local a divergé — git -C $APP_DIR log --oneline origin/$BASE..$BASE"
git branch -q -D "$BR" 2>/dev/null
ok "ce PC est sur $BASE : $(git log -1 --format='%h %s')"
if [ "$REDEMARRER" = 1 ]; then
  ./update.sh >/dev/null 2>&1 && ok "DevPilot redémarré avec cette version" || echo "! redémarre-le :  devpilot update"
fi
echo
echo "  Publié. Sur les autres PC :  devpilot update"
