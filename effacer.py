#!/usr/bin/env python3
"""DevPilot — effacer de CE PC tout ce qui touche à DevPilot et à tes projets.

    devpilot effacer               inventaire → questions → confirmation tapée → effacement
    devpilot effacer --simulation  la même chose, mais RIEN n'est effacé (ce qui serait fait)

Le principe : tout est déjà dans le cloud (GitHub, coffre, sauvegardes) — ce PC doit pouvoir
redevenir vierge, puis être reconstruit (devpilot pc, et l'assistant de chaque projet).

Sécurité :
  - inventaire d'abord, RIEN n'est effacé avant la confirmation finale tapée ;
  - le travail qui n'est QUE sur ce PC (fichiers non commités, commits jamais poussés, stash)
    est signalé dépôt par dépôt, et ce projet n'est jamais coché par défaut ;
  - sauvegardes proposées AVANT (données DevPilot chiffrées, et celles des projets) ;
  - jamais rien hors de ton dossier personnel, jamais un dossier racine (~/.ssh, ~/.config…) en
    entier ; un lien est retiré sans suivre sa cible ; les secrets sont écrasés avant d'être effacés ;
  - un projet peut déclarer ce qu'il a installé ailleurs sur le PC : fichier `effacer-poste.sh`
    à la racine d'un de ses dépôts (protocole plus bas) — DevPilot le pose en question, comme le reste.

Protocole `effacer-poste.sh` (dans un dépôt d'un projet) :
  bash effacer-poste.sh liste        → une ligne JSON par élément :
      {"id": "...", "titre": "...", "detail": "...", "chemins": ["~/..."], "secret": false,
       "action": false, "defaut": true, "avant": false}
      action=true → DevPilot appelle « bash effacer-poste.sh faire <id> » (avant les effacements
      si avant=true : sauvegarde, révocation d'une clé…) ; chemins → effacés par DevPilot.
  bash effacer-poste.sh verifier     → avertissements (une ligne chacun), affichés à l'inventaire
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("HOME") or Path.home()).resolve()
DEVPILOT = HOME / "devpilot"
ESPACE = DEVPILOT / ".devpilot"
PROJETS = DEVPILOT / "projects"
SIMULATION = False

# jamais effacés EN ENTIER (on peut effacer quelque chose DEDANS)
PROTEGES = {HOME, HOME / ".ssh", HOME / ".config", HOME / ".local", HOME / ".local" / "bin", HOME / ".local" / "share",
            HOME / ".local" / "share" / "applications", HOME / ".config" / "systemd", HOME / ".config" / "systemd" / "user",
            HOME / "Desktop", HOME / "Bureau", HOME / "Documents", HOME / "Downloads", HOME / "Téléchargements",
            HOME / ".cache", HOME / ".gnupg"}

V, R, J, G, D, N = ("\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("",) * 6


# ── outils ──────────────────────────────────────────────────────────────────

def sh(args, cwd=None, timeout=120, entree=None):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=entree)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def chemin(p) -> Path:
    """~/x → absolu, NORMALISÉ sans suivre le dernier lien (on efface le lien, pas sa cible)."""
    p = Path(os.path.expanduser(str(p)))
    return Path(os.path.normpath(p if p.is_absolute() else HOME / p))


def autorise(p: Path) -> str:
    """'' si on peut effacer p, sinon la raison."""
    if not str(p).startswith(str(HOME) + os.sep):
        return "hors de ton dossier personnel"
    if p in PROTEGES:
        return "dossier racine protégé"
    if ".." in p.parts:
        return "chemin suspect"
    # le dossier qui CONTIENT p doit lui-même être sous HOME, liens résolus (pas d'évasion par un lien)
    try:
        if not p.parent.resolve().is_relative_to(HOME):
            return "son dossier parent sort de ton dossier personnel"
    except OSError:
        return "dossier parent illisible"
    return ""


def taille(paths):
    total = 0
    for p in paths:
        if p.is_symlink() or not p.exists():
            continue
        rc, out, _ = sh(["du", "-sb", str(p)], timeout=300)
        try:
            total += int(out.split()[0])
        except (ValueError, IndexError):
            pass
    return total


def humain(n):
    for u in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} To"


def demander(question, defaut=False):
    suffixe = "[O/n]" if defaut else "[o/N]"
    try:
        r = input(f"{question} {D}{suffixe}{N} ").strip().lower()
    except EOFError:
        r = ""
    return defaut if not r else r[0] in "oy"


def effacer(p: Path, secret=False, journal=None):
    raison = autorise(p)
    if raison:
        journal.append(f"✗ {p} : refusé ({raison})"); return
    if not (p.exists() or p.is_symlink()):
        return
    if SIMULATION:
        journal.append(f"· effacerait {p}"); return
    try:
        if p.is_symlink():
            p.unlink()
        elif p.is_dir():
            if secret:
                for f in p.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        sh(["shred", "-u", str(f)])
            shutil.rmtree(p)
        else:
            if secret:
                sh(["shred", "-u", str(p)])
            if p.exists():
                p.unlink()
        journal.append(f"✓ {p}")
    except OSError as e:
        journal.append(f"✗ {p} : {e}")


# ── inventaire ──────────────────────────────────────────────────────────────

@dataclass
class Element:
    id: str
    titre: str
    detail: str = ""
    chemins: list = field(default_factory=list)
    secret: bool = False
    defaut: bool = True
    avant: bool = False
    risque: str = ""           # travail perdu si on efface : jamais coché par défaut, toujours demandé
    action: object = None      # callable(journal) → effectué AVANT d'effacer les chemins
    choisi: bool = False


def depots(racine: Path, prof=4):
    out = []
    if (racine / ".git").exists():
        out.append(racine)
    def walk(d, n):
        if n > prof:
            return
        try:
            for e in sorted(d.iterdir()):
                if e.is_dir() and not e.is_symlink() and e.name not in ("node_modules", ".venv", "venv", ".git", "dist", "build", ".devpilot"):
                    if (e / ".git").exists():
                        out.append(e)
                    walk(e, n + 1)
        except OSError:
            pass
    walk(racine, 1)
    return out


def travail_local(repo: Path):
    """Ce qui n'existe QUE sur ce PC dans ce dépôt : [phrases]. Une branche dont le contenu est
    déjà dans main (fusion classique ou squash) n'est pas du travail perdu."""
    pertes = []
    _, porc, _ = sh(["git", "-C", str(repo), "status", "--porcelain"])
    n = len([l for l in porc.splitlines() if l.strip()])
    if n:
        pertes.append(f"{n} fichier(s) modifié(s) ou nouveau(x) non commité(s)")
    _, stash, _ = sh(["git", "-C", str(repo), "stash", "list"])
    if stash:
        pertes.append(f"{len(stash.splitlines())} stash")
    _, base, _ = sh(["git", "-C", str(repo), "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"])
    base = base or "origin/main"
    _, url, _ = sh(["git", "-C", str(repo), "remote", "get-url", "origin"])
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url or "")
    slug = m.group(1) if m else ""
    _, brs, _ = sh(["git", "-C", str(repo), "for-each-ref", "refs/heads", "--format=%(refname:short)"])
    for b in brs.splitlines():
        rc, n, _ = sh(["git", "-C", str(repo), "rev-list", "--count", b, "--not", "--remotes"])
        if rc != 0 or n in ("", "0"):
            continue
        if deja_fusionnee(repo, slug, b, base):
            continue
        pertes.append(f"branche « {b} » : {n} commit(s) jamais poussé(s)")
    rc, _, _ = sh(["git", "-C", str(repo), "remote", "get-url", "origin"])
    if rc != 0:
        pertes.append("aucun dépôt GitHub (origin) : TOUT le code n'est que sur ce PC")
    return pertes


def deja_fusionnee(repo: Path, slug: str, b: str, base: str) -> bool:
    """Le travail de cette branche est-il déjà sauvé dans main ?
    1. une PR FUSIONNÉE sur GitHub contenait exactement ce travail : la pointe de la branche est
       (ou précède) le dernier commit de cette PR — un commit ajouté APRÈS la fusion reste à perdre ;
    2. sinon, fusionner la branche dans main ne changerait rien (fusion classique, ou squash récent)."""
    _, tip, _ = sh(["git", "-C", str(repo), "rev-parse", b])
    if slug and shutil.which("gh"):
        rc, out, _ = sh(["gh", "pr", "list", "-R", slug, "--head", b, "--state", "merged", "--limit", "20",
                         "--json", "headRefOid", "-q", ".[].headRefOid"], timeout=30)
        for oid in out.split() if rc == 0 else []:
            if oid == tip or sh(["git", "-C", str(repo), "merge-base", "--is-ancestor", tip, oid])[0] == 0:
                return True
    rc, arbre, _ = sh(["git", "-C", str(repo), "merge-tree", "--write-tree", base, b])
    _, arbre_base, _ = sh(["git", "-C", str(repo), "rev-parse", f"{base}^{{tree}}"])
    return rc == 0 and bool(arbre.split()) and arbre.split()[0] == arbre_base


def noms_compose(racine: Path):
    """Les projets compose déclarés dans le dossier (pour retrouver volumes/réseaux/images même
    quand aucun conteneur n'existe plus)."""
    noms = set()
    for f in list(racine.rglob("docker-compose*.yml")) + list(racine.rglob("compose*.yml")):
        if "node_modules" in f.parts:
            continue
        try:
            m = re.search(r"^name:\s*['\"]?([\w.-]+)", f.read_text(errors="ignore"), re.M)
        except OSError:
            continue
        noms.add(m.group(1) if m else re.sub(r"[^a-z0-9_-]", "", f.parent.name.lower()))
    return noms


def plan_docker(racine: Path):
    try:
        import purge
    except Exception:
        return None
    if not shutil.which("docker"):
        return None
    groupes = {g["name"]: g for g in purge.docker_plan(racine.resolve())}
    for nom in noms_compose(racine):
        if nom in groupes:
            continue
        vols, nets, imgs = purge._named("volume", nom), purge._named("network", nom), purge._built_images(nom, [])
        if vols or nets or imgs:
            groupes[nom] = {"compose": nom, "name": nom, "containers": [], "networks": nets, "volumes": vols, "images": imgs}
    return list(groupes.values())


def action_docker(groupes):
    import purge
    def faire(journal):
        for g in groupes:
            if g["containers"]:
                if SIMULATION:
                    journal.append(f"· arrêterait et effacerait {len(g['containers'])} conteneur(s) de {g['name']}")
                else:
                    rc, _, err = purge._docker(["rm", "-f", "-v"] + [c["id"] for c in g["containers"]], timeout=300)
                    journal.append(f"{'✓' if rc == 0 else '✗'} conteneurs {g['name']}" + ("" if rc == 0 else f" : {err[-120:]}"))
            for kind, cmd in (("networks", ["network", "rm"]), ("volumes", ["volume", "rm"])):
                for x in g[kind]:
                    if SIMULATION:
                        journal.append(f"· effacerait {kind[:-1]} {x}"); continue
                    rc, _, err = purge._docker(cmd + [x])
                    journal.append(f"{'✓' if rc == 0 else '✗'} {kind[:-1]} {x}" + ("" if rc == 0 else f" : {err[-120:]}"))
            for im in g["images"]:
                if SIMULATION:
                    journal.append(f"· effacerait l'image {im['ref']}"); continue
                rc, _, err = purge._docker(["rmi", "-f", im["ref"]], timeout=300)
                journal.append(f"{'✓' if rc == 0 else '✗'} image {im['ref']}" + ("" if rc == 0 else f" : {err[-120:]}"))
    return faire


def action_processus(racine: Path):
    def faire(journal):
        try:
            import purge, ports
            procs = purge._processes(racine.resolve(), ignore={os.getpid(), os.getppid()})
        except Exception:
            procs = []
        for p in procs:
            if SIMULATION:
                journal.append(f"· arrêterait {p['name']} (pid {p['pid']})"); continue
            ports.kill_tree(p["pid"], grace=3)
            journal.append(f"✓ arrêté : {p['name']} (pid {p['pid']})")
    return faire


def crochets(racine: Path):
    """Les éléments déclarés par les projets (effacer-poste.sh)."""
    elements, avertissements = [], []
    for repo in depots(racine):
        script = repo / "effacer-poste.sh"
        if not script.is_file():
            continue
        rc, out, err = sh(["bash", str(script), "liste"], cwd=repo, timeout=120)
        for ligne in out.splitlines():
            try:
                x = json.loads(ligne)
            except ValueError:
                continue
            e = Element(id=f"{repo.name}:{x['id']}", titre=x.get("titre", x["id"]), detail=x.get("detail", ""),
                        chemins=[chemin(c) for c in x.get("chemins", [])], secret=bool(x.get("secret")),
                        defaut=bool(x.get("defaut", True)), avant=bool(x.get("avant")))
            if x.get("action"):
                ident = x["id"]
                def faire(journal, script=script, repo=repo, ident=ident, titre=e.titre):
                    if SIMULATION:
                        journal.append(f"· ferait : {titre}"); return
                    rc, out, err = sh(["bash", str(script), "faire", ident], cwd=repo, timeout=900)
                    for l in (out + "\n" + err).strip().splitlines()[-6:]:
                        journal.append("    " + l)
                    journal.append(f"{'✓' if rc == 0 else '✗'} {titre}")
                e.action = faire
            elements.append(e)
        _, out, _ = sh(["bash", str(script), "verifier"], cwd=repo, timeout=120)
        avertissements += [f"{repo.name} : {l}" for l in out.splitlines() if l.strip()]
    return elements, avertissements


def projets_enregistres():
    """Les projets connus de DevPilot (certains peuvent vivre hors de ~/devpilot/projects)."""
    out = []
    try:
        con = sqlite3.connect(ESPACE / "data" / "devpilot.db")
        for nom, path in con.execute("SELECT name, path FROM projects"):
            if path:
                out.append((nom, chemin(path)))
    except sqlite3.Error:
        pass
    noms = {p for _, p in out}
    if PROJETS.is_dir():
        for d in sorted(PROJETS.iterdir()):
            if d.is_dir() and not d.is_symlink() and d not in noms:
                out.append((d.name, d))
    return out


def inventaire():
    elements, avertissements = [], []
    donnees = ESPACE / "data"
    if (donnees / "devpilot.db").exists():
        elements.append(Element(
            id="export", titre="Sauvegarder les données DevPilot AVANT (archive chiffrée, hors de ce qui est effacé)",
            detail="projets, notes, étapes, réglages — tape une phrase secrète ; garde l'archive (copie-la dans ton cloud)",
            avant=True, defaut=True, action=action_export(donnees)))
    for nom, racine in projets_enregistres():
        if not racine.exists():
            continue
        repos = depots(racine)
        pertes = {str(r.relative_to(racine)) if r != racine else ".": travail_local(r) for r in repos}
        pertes = {k: v for k, v in pertes.items() if v}
        risque = "; ".join(f"{k} : {', '.join(v)}" for k, v in pertes.items())
        if not repos:
            risque = "aucun dépôt git : rien de ce dossier n'est sur GitHub"
        el, av = crochets(racine)
        elements += el
        avertissements += av
        groupes = plan_docker(racine)
        if groupes:
            nc = sum(len(g["containers"]) for g in groupes); nv = sum(len(g["volumes"]) for g in groupes)
            ni = sum(len(g["images"]) for g in groupes)
            elements.append(Element(id=f"docker:{nom}", titre=f"Docker de « {nom} »",
                                    detail=f"{nc} conteneur(s), {nv} volume(s) (bases de données locales), {ni} image(s) construite(s)",
                                    action=action_docker(groupes)))
        try:
            import purge
            dehors = purge.outside_plan(racine.resolve())
        except Exception:
            dehors = []
        liens = [chemin(x["path"]) for x in dehors if x["default"]]
        if liens:
            elements.append(Element(id=f"liens:{nom}", titre=f"Liens et raccourcis vers « {nom} »",
                                    detail=", ".join(str(p).replace(str(HOME), "~") for p in liens), chemins=liens))
        elements.append(Element(id=f"projet:{nom}", titre=f"Le projet « {nom} »",
                                detail=f"{str(racine).replace(str(HOME), '~')} · {humain(taille([racine]))} · {len(repos)} dépôt(s)",
                                chemins=[racine], risque=risque, defaut=not risque, action=action_processus(racine)))
    # DevPilot lui-même
    appli = [ESPACE, HOME / ".config" / "devpilot", HOME / ".local" / "bin" / "devpilot", HOME / ".local" / "bin" / "devpilot-watcher",
             HOME / ".local" / "share" / "applications" / "devpilot.desktop",
             HOME / ".config" / "systemd" / "user" / "devpilot-watcher.service", HOME / ".config" / "autostart" / "devpilot.desktop"]
    appli = [p for p in appli if p.exists() or p.is_symlink()]
    if appli:
        elements.append(Element(id="devpilot", titre="DevPilot lui-même (programme, données, commandes, icône, service)",
                                detail=", ".join(str(p).replace(str(HOME), "~") for p in appli), chemins=appli,
                                action=action_arret_devpilot()))
    if (HOME / ".ssh" / "devpilot").exists():
        elements.append(Element(id="cles-devpilot", titre="Les clés SSH créées par DevPilot pour tes serveurs (~/.ssh/devpilot)",
                                chemins=[HOME / ".ssh" / "devpilot"], secret=True,
                                detail="sans elles, ce PC n'entre plus sur ces serveurs (une nouvelle clé se crée à la reconstruction)"))
    for outil, cmd, quoi in (("gh", ["gh", "auth", "logout", "--hostname", "github.com"], "GitHub CLI (gh)"),
                             ("bw", ["bw", "logout"], "Bitwarden CLI (bw)")):
        if shutil.which(outil):
            elements.append(Element(id=f"deco:{outil}", titre=f"Déconnecter {quoi} de ce PC", defaut=False,
                                    detail="ton compte reste intact ; ce PC n'y a simplement plus accès",
                                    action=action_commande(cmd, f"{quoi} déconnecté")))
    return elements, avertissements


def action_export(donnees: Path):
    def faire(journal):
        dest = HOME / f"devpilot-donnees-{time.strftime('%Y%m%d-%H%M')}.tar.gz.gpg"
        if SIMULATION:
            journal.append(f"· sauvegarderait les données DevPilot → {dest}"); return
        tar = subprocess.Popen(["tar", "-czf", "-", "-C", str(donnees.parent), donnees.name], stdout=subprocess.PIPE)
        print(f"  {G}Phrase secrète de l'archive{N} (gpg te la demande deux fois — note-la) :")
        rc = subprocess.run(["gpg", "--symmetric", "--cipher-algo", "AES256", "-o", str(dest)], stdin=tar.stdout).returncode
        tar.wait()
        journal.append(f"✓ données DevPilot sauvegardées : {dest} (garde-la !)" if rc == 0 and dest.exists()
                       else "✗ sauvegarde des données DevPilot ÉCHOUÉE")
        if rc != 0:
            raise SystemExit("arrêt : la sauvegarde a échoué, RIEN n'a été effacé")
    return faire


def action_arret_devpilot():
    def faire(journal):
        if SIMULATION:
            journal.append("· arrêterait DevPilot, son service et ses sessions tmux"); return
        sh(["systemctl", "--user", "disable", "--now", "devpilot-watcher.service"])
        sh(["tmux", "-L", os.environ.get("DEVPILOT_TMUX_SOCKET", "devpilot"), "kill-server"])
        sh(["fuser", "-k", "-TERM", f"{os.environ.get('DEVPILOT_PORT', '5555')}/tcp"])
        journal.append("✓ DevPilot arrêté (service, sessions, serveur)")
    return faire


def action_commande(cmd, ok):
    def faire(journal):
        if SIMULATION:
            journal.append(f"· {' '.join(cmd)}"); return
        rc, out, err = sh(cmd)
        journal.append(f"✓ {ok}" if rc == 0 else f"✗ {' '.join(cmd)} : {(err or out)[-120:]}")
    return faire


# ── déroulé ─────────────────────────────────────────────────────────────────

def afficher(elements, avertissements):
    print(f"\n{G}Ce qui touche à DevPilot et à tes projets sur ce PC ({socket.gethostname()}){N}\n")
    for i, e in enumerate(elements, 1):
        marque = f"{R}⚠ {N}" if e.risque else ("🔑 " if e.secret else "")
        print(f"  {i:>2}. {marque}{e.titre}")
        if e.detail:
            print(f"      {D}{e.detail}{N}")
        if e.risque:
            print(f"      {R}travail qui n'est QUE sur ce PC : {e.risque}{N}")
    if avertissements:
        print(f"\n{J}À savoir avant :{N}")
        for a in avertissements:
            print(f"  {J}!{N} {a}")
    print(f"\n{D}Jamais touché : tes autres clés (~/.ssh/id_*), ~/.gitconfig, docker lui-même, les paquets (tmux, gh…),\n"
          f"tes fichiers hors projets. Les copies dans le cloud (GitHub, coffre, sauvegardes R2) restent intactes.{N}")


def choisir(elements):
    print(f"\n{G}Que veux-tu effacer ?{N}")
    print("  1) TOUT ce qui est listé — je te redemande seulement pour le travail non sauvegardé et les secrets")
    print("  2) Choisir élément par élément")
    try:
        mode = input(f"Ton choix {D}[2]{N} : ").strip() or "2"
    except EOFError:
        mode = "2"
    for e in elements:
        if mode == "1" and not e.risque and not e.secret and e.defaut:
            e.choisi = True
            continue
        q = e.titre + (f"\n     {R}⚠ ce travail sera PERDU : {e.risque}{N}\n     L'effacer quand même ?" if e.risque else " ?")
        e.choisi = demander(("  " if not e.risque else "  ") + q, defaut=e.defaut and not e.risque)


def confirmer(elements):
    choisis = [e for e in elements if e.choisi]
    if not choisis:
        print("Rien de choisi : rien n'est effacé."); return False
    print(f"\n{G}Récapitulatif — sera fait, dans cet ordre :{N}")
    for e in sorted(choisis, key=lambda e: not e.avant):
        print(f"  {'→' if e.avant else '✗'} {e.titre}")
        for p in e.chemins:
            print(f"      {D}{str(p).replace(str(HOME), '~')}{N}")
    if SIMULATION:
        print(f"\n{J}SIMULATION : rien ne sera effacé.{N}"); return True
    mot = f"EFFACER {socket.gethostname()}"
    r = ""
    print(f"\n{R}C'est définitif.{N} Pour confirmer, tape exactement : {G}{mot}{N}  (autre chose = annuler)")
    for _ in range(5):                       # une Entrée de trop ne confirme ni n'annule : on redemande
        try:
            r = input("> ").strip()
        except EOFError:
            r = ""; break
        if r:
            break
    if r != mot:
        print("Pas confirmé : RIEN n'a été effacé."); return False
    return True


def executer(elements):
    journal = []
    choisis = [e for e in elements if e.choisi]
    for e in [e for e in choisis if e.avant]:                    # sauvegardes, révocations… d'abord
        print(f"→ {e.titre}")
        e.action(journal)
    for e in [e for e in choisis if not e.avant]:
        if e.action:
            e.action(journal)
        for p in e.chemins:
            effacer(p, e.secret, journal)
    # ~/devpilot ne contient plus que des dossiers vides ? on l'enlève aussi
    if DEVPILOT.exists() and not any(p.is_file() or p.is_symlink() for p in DEVPILOT.rglob("*")):
        effacer(DEVPILOT, journal=journal)
    print(f"\n{G}Journal{N}")
    for l in journal:
        print("  " + l)
    if not SIMULATION:
        print(f"\n{G}Pour tout reconstruire plus tard{N} : la section « PC vierge » du README de DevPilot\n"
              "  https://github.com/osarsar/devpilot-app#2-pc-vierge--installer-devpilot\n"
              "  puis l'assistant de chaque projet.")
    return journal


def lance_depuis_devpilot():
    """Lancé dans un terminal DevPilot (tmux de DevPilot, ou enfant du serveur) ? Arrêter DevPilot
    tuerait ce programme au milieu de l'effacement."""
    if "/devpilot" in os.environ.get("TMUX", "").split(",")[0]:
        return True
    try:
        import psutil
        p = psutil.Process(os.getpid()).parent()
        while p:
            if "dashboard.py" in " ".join(p.cmdline()):
                return True
            p = p.parent()
    except Exception:
        pass
    return False


def main(argv=None):
    global SIMULATION
    ap = argparse.ArgumentParser(description="Effacer de ce PC tout ce qui touche à DevPilot et à tes projets")
    ap.add_argument("--simulation", action="store_true", help="tout montrer, ne rien effacer")
    a = ap.parse_args(argv)
    SIMULATION = a.simulation
    if not SIMULATION and lance_depuis_devpilot():
        print(f"{R}✗ Lance-moi depuis un terminal NORMAL (pas un terminal de DevPilot) :{N} arrêter DevPilot "
              "pendant l'effacement me couperait au milieu.\n  Ouvre un terminal (Ctrl+Alt+T) et tape : devpilot effacer")
        return 2
    print(f"{G}DevPilot — effacer ce PC{N}" + (f"  {J}(SIMULATION : rien ne sera effacé){N}" if SIMULATION else ""))
    print("Inventaire en cours (dépôts, docker, raccourcis)…")
    elements, avertissements = inventaire()
    if not elements:
        print("Rien de DevPilot sur ce PC."); return 0
    afficher(elements, avertissements)
    choisir(elements)
    if not confirmer(elements):
        return 1
    executer(elements)
    return 0


if __name__ == "__main__":
    sys.exit(main())
