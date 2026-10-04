# DevPilot — rcfile of the "Import project" terminal.
# Loads the usual shell config, then keeps the variables listed in
# DEVPILOT_FOLLOW_CWD (comma separated, e.g. "MD_ROOT") equal to the current
# directory before every command. So a script like reconstruct.sh installs
# where you launch it, by default. An explicit value wins:
#   MD_ROOT=/elsewhere ./reconstruct.sh     (for one command)
#   export MD_ROOT=/elsewhere               (for the rest of the session)

if [ -f "$HOME/.profile" ]; then . "$HOME/.profile"
elif [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"
fi

declare -A __dp_last
__dp_follow() {
  local v cur
  for v in ${DEVPILOT_FOLLOW_CWD//,/ }; do
    cur="${!v-}"
    # set by hand since our last update → leave it alone
    if [ -n "$cur" ] && [ "$cur" != "${__dp_last[$v]-}" ]; then continue; fi
    export "$v=$PWD"
    __dp_last[$v]="$PWD"
  done
}
# DEBUG trap: runs before each command, so `cd x && ./script.sh` sees x
trap '__dp_follow' DEBUG

# git clone in a project folder that only holds DevPilot's own files
# (.devpilot/, its CLAUDE.md):
#  - `git clone <url>` of a repo named like the folder → cloned straight into
#    the folder (not projects/site/site)
#  - `git clone <url> .` → works too (git refuses a non-empty folder: DevPilot's
#    files step aside during the clone and come back after)
# Anything else: plain git.
__dp_only_devpilot() {
  [ -d .devpilot ] && [ -z "$(ls -A | grep -vxE '\.devpilot|CLAUDE\.md|\.claude')" ]
}

__dp_clone_here() {
  local tmp rc
  tmp="$(mktemp -d)"
  mv .devpilot "$tmp/"
  [ -e CLAUDE.md ] && mv CLAUDE.md "$tmp/"
  [ -e .claude ] && mv .claude "$tmp/"
  command git "$@"; rc=$?
  if [ -e .devpilot ]; then cp -rn "$tmp/.devpilot/." .devpilot/; else mv "$tmp/.devpilot" .; fi
  [ -e "$tmp/.claude" ] && { [ -e .claude ] && cp -rn "$tmp/.claude/." .claude/ || mv "$tmp/.claude" .; }
  if [ -e "$tmp/CLAUDE.md" ]; then
    if [ -e CLAUDE.md ]; then mv "$tmp/CLAUDE.md" .devpilot/CLAUDE.devpilot.md   # the repo's own wins
    else mv "$tmp/CLAUDE.md" .; fi
  fi
  rm -rf "$tmp"
  return $rc
}

git() {
  if [ "$1" = clone ] && __dp_only_devpilot; then
    local a pos=() skip=0
    for a in "${@:2}"; do                       # positional args (url [dir]), options skipped
      if [ $skip = 1 ]; then skip=0; continue; fi
      case "$a" in
        -b|--branch|-o|--origin|--depth|-c|--config|--reference|--reference-if-able|-u|--upload-pack|\
        --template|--separate-git-dir|-j|--jobs|--filter|--server-option|--shallow-since|--shallow-exclude|--bundle-uri) skip=1 ;;
        -*) ;;
        *) pos+=("$a") ;;
      esac
    done
    if [ ${#pos[@]} -eq 2 ] && [ "${pos[1]}" = "." ]; then
      __dp_clone_here "$@"; return
    fi
    if [ ${#pos[@]} -eq 1 ]; then
      local name="${pos[0]%/}"; name="${name##*/}"; name="${name##*:}"; name="${name%.git}"
      if [ "$name" = "$(basename "$PWD")" ]; then
        echo -e "\e[2m[DevPilot] « $name » = le projet : clone directement dans ce dossier\e[0m"
        __dp_clone_here "$@" .; return
      fi
    fi
  fi
  command git "$@"
}

if [ -n "${DEVPILOT_TERM_TITLE:-}" ]; then
  echo -e "\e[2m[DevPilot] ${DEVPILOT_TERM_TITLE}\e[0m"
else
  echo -e "\e[2m[DevPilot] Terminal d'import — ${DEVPILOT_FOLLOW_CWD//,/, } = dossier courant\e[0m"
fi
