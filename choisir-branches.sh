#!/usr/bin/env bash
# Mettre à jour des dépôts en CHOISISSANT la branche de chacun — fini les « git pull » qui ne
# récupèrent rien parce que le dépôt était resté sur une vieille branche.
#
#   bash choisir-branches.sh [--auto] DÉPÔT...
#
# Pour chaque dépôt : sa branche actuelle, son état (travail non publié, branche déjà fusionnée…),
# les nouveautés de main. Choix recommandé (★) : main si rien n'est en cours, sinon rester sur sa
# branche (rien ne se perd). Entrée = les choix ★ ; « c » = choisir dépôt par dépôt.
# --auto (ou pas de clavier) : les choix ★ sans rien demander.
# Rien n'est jamais écrasé : un dépôt avec des modifications non commitées ne change pas de branche.
set -u
AUTO=0; [ "${1:-}" = "--auto" ] && { AUTO=1; shift; }
[ -t 0 ] || [ -n "${CHOIX_INTERACTIF:-}" ] || AUTO=1
V="\033[32m"; R="\033[31m"; J="\033[33m"; G="\033[1m"; D="\033[2m"; N="\033[0m"
[ -t 1 ] || { V=""; R=""; J=""; G=""; D=""; N=""; }

declare -a REPOS NOMS ACTUELLE CIBLE ETAT
i=0
for r in "$@"; do
  [ -d "$r/.git" ] || continue
  git -C "$r" fetch -q --prune origin 2>/dev/null
  cur=$(git -C "$r" branch --show-current)
  sales=$(git -C "$r" status --porcelain --untracked-files=no | wc -l)
  base=main; git -C "$r" rev-parse -q --verify origin/main >/dev/null || base=master
  if [ "$cur" = "$base" ]; then
    retard=$(git -C "$r" rev-list --count HEAD..origin/$base 2>/dev/null || echo 0)
  else
    retard=$(git -C "$r" rev-list --count "$base..origin/$base" 2>/dev/null || git -C "$r" rev-list --count "HEAD..origin/$base" 2>/dev/null || echo 0)
  fi
  etat=""; cible="$base"
  if [ "$sales" -gt 0 ]; then
    etat="${R}$sales fichier(s) modifié(s) non commité(s)${N}"; cible="$cur"
  elif [ -n "$cur" ] && [ "$cur" != "$base" ]; then
    perso=$(git -C "$r" rev-list --count "$cur" --not --remotes 2>/dev/null || echo 0)
    fus=0
    arbre=$(git -C "$r" merge-tree --write-tree "origin/$base" "$cur" 2>/dev/null | head -1)
    [ -n "$arbre" ] && [ "$arbre" = "$(git -C "$r" rev-parse "origin/$base^{tree}" 2>/dev/null)" ] && fus=1
    if [ "$fus" = 0 ] && [ "$perso" -gt 0 ] && command -v gh >/dev/null; then
      slug=$(git -C "$r" remote get-url origin | sed -E 's#.*github\.com[:/]##; s#\.git$##')
      tip=$(git -C "$r" rev-parse "$cur")
      for oid in $(gh pr list -R "$slug" --head "$cur" --state merged --json headRefOid -q '.[].headRefOid' 2>/dev/null); do
        { [ "$oid" = "$tip" ] || git -C "$r" merge-base --is-ancestor "$tip" "$oid" 2>/dev/null; } && fus=1 && break
      done
    fi
    if [ "$fus" = 1 ]; then etat="${D}branche finie (déjà dans $base)${N}"
    elif [ "$perso" -gt 0 ]; then etat="${J}$perso commit(s) jamais poussé(s) sur cette branche${N}"; cible="$cur"
    elif git -C "$r" rev-parse -q --verify "origin/$cur" >/dev/null; then etat="branche en cours (sur GitHub)"; cible="$cur"
    else etat="${D}branche locale sans travail propre${N}"; fi
  fi
  [ -z "$cur" ] && { etat="${R}HEAD détaché${N}"; cible="$base"; }
  REPOS[i]="$r"; NOMS[i]="$(basename "$r")"; ACTUELLE[i]="$cur"; CIBLE[i]="$cible"
  ETAT[i]="$etat$( [ "$retard" -gt 0 ] 2>/dev/null && echo " · ${G}$base a $retard nouveauté(s)${N}")"
  i=$((i+1))
done
[ "$i" -eq 0 ] && { echo "aucun dépôt"; exit 0; }

tableau() {
  echo -e "\n${G}Les dépôts de ce PC${N}  ${D}(★ = ce qui sera récupéré)${N}"
  for k in "${!REPOS[@]}"; do
    printf "  %-14s branche %-28s ★ %-22s %b\n" "${NOMS[k]}" "${ACTUELLE[k]:-?}" "${CIBLE[k]}" "${ETAT[k]}"
  done
}
tableau

if [ "$AUTO" = 0 ]; then
  echo -e "\n  ${G}Entrée${N} = récupérer les ★   ·   ${G}c${N} = choisir dépôt par dépôt   ·   ${G}q${N} = ne rien faire"
  read -r -p "  > " rep
  case "$rep" in
    q|Q) echo "rien n'a été changé"; exit 0 ;;
    c|C)
      for k in "${!REPOS[@]}"; do
        r="${REPOS[k]}"
        mapfile -t BR < <( { echo main; git -C "$r" for-each-ref --sort=-committerdate --format='%(refname:short)' refs/heads;
                             git -C "$r" for-each-ref --sort=-committerdate --format='%(refname:lstrip=3)' refs/remotes/origin; } \
                           | grep -vx HEAD | awk '!vu[$0]++' | head -12)
        echo -e "\n  ${G}${NOMS[k]}${N} — actuellement ${ACTUELLE[k]:-?} · ${ETAT[k]}"
        for j in "${!BR[@]}"; do
          b="${BR[j]}"; marque=""
          [ "$b" = "${CIBLE[k]}" ] && marque=" ★"
          [ "$b" = "${ACTUELLE[k]}" ] && marque="$marque (actuelle)"
          git -C "$r" rev-parse -q --verify "refs/heads/$b" >/dev/null || marque="$marque ${D}(sur GitHub)${N}"
          echo -e "    $((j+1))) $b$marque"
        done
        read -r -p "  numéro [★ ${CIBLE[k]}] : " n
        [ -n "$n" ] && [ "$n" -ge 1 ] 2>/dev/null && [ "$n" -le "${#BR[@]}" ] && CIBLE[k]="${BR[n-1]}"
      done
      tableau ;;
  esac
fi

echo
ECHEC=0
for k in "${!REPOS[@]}"; do
  r="${REPOS[k]}"; n="${NOMS[k]}"; cur="${ACTUELLE[k]}"; c="${CIBLE[k]}"
  avant=$(git -C "$r" rev-parse --short HEAD)
  if [ "$c" != "$cur" ]; then
    if [ -n "$(git -C "$r" status --porcelain --untracked-files=no)" ]; then
      echo -e "  ${R}✗${N} $n : modifications non commitées — reste sur ${cur:-?} (commite ou « git stash », puis relance)"; ECHEC=1; continue
    fi
    if git -C "$r" rev-parse -q --verify "refs/heads/$c" >/dev/null; then git -C "$r" switch -q "$c"
    else git -C "$r" switch -q --track "origin/$c" 2>/dev/null || git -C "$r" switch -q -c "$c" "origin/$c"; fi \
      || { echo -e "  ${R}✗${N} $n : impossible de passer sur $c"; ECHEC=1; continue; }
  fi
  if git -C "$r" rev-parse -q --verify "origin/$c" >/dev/null; then
    git -C "$r" merge -q --ff-only "origin/$c" 2>/dev/null \
      || { echo -e "  ${J}!${N} $n : $c a des commits qui ne sont pas sur GitHub — laissé tel quel (git -C $r log --oneline origin/$c..$c)"; ECHEC=1; continue; }
  fi
  apres=$(git -C "$r" rev-parse --short HEAD)
  if [ "$avant" = "$apres" ] && [ "$c" = "$cur" ]; then echo -e "  ${V}✓${N} $n : $c, déjà à jour ($apres)"
  else echo -e "  ${V}✓${N} $n : ${cur:-?} → ${G}$c${N} $avant → $apres  ${D}$(git -C "$r" log -1 --format=%s | cut -c1-60)${N}"; fi
done
exit $ECHEC
