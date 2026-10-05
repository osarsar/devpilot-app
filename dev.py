"""DevPilot — the development side of a project.

A project may hold several git repos (MarocDefender: md_infra and, inside it,
md-backend, md-frontend…). Developing = choosing WHERE (repo, branch), then
opening the right tool THERE (Claude, VS Code, a terminal).

    repos(pid)                       every repo of the project, nested ones included
    branches(pid, repo)              local + GitHub branches of a repo
    switch(pid, repo, branch)        change branch — never with uncommitted work
    new_branch(pid, repo, name)      ALWAYS from an up-to-date main
    update_main(pid, repo)           fast-forward main from GitHub, without touching your work
    open_editor / open_external      VS Code / an external terminal (or Claude) IN the repo

Never destructive: no reset, no forced checkout, no forced push.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import db
import projects as P
from projects import ProjectError

MAX_DEPTH = 4


def _git(path, *args, timeout=60):
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "délai dépassé"


def _n(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _exists(path, ref):
    return _git(path, "rev-parse", "--verify", "-q", ref)[0] == 0


def base_branch(path):
    rc, out, _ = _git(path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.startswith("origin/"):
        return out[len("origin/"):]
    for b in ("main", "master"):
        if _exists(path, f"refs/heads/{b}") or _exists(path, f"origin/{b}"):
            return b
    return "main"


def project_root(pid):
    p = P.get(pid)
    root = Path(p["path"]).expanduser() if p.get("path") else None
    if not root or not root.is_dir():
        raise ProjectError("Le dossier du projet est introuvable")
    return p, root.resolve()


def repo_path(pid, repo):
    """The repo dir (relative to the project, "." = root) → absolute path, refused if outside."""
    _, root = project_root(pid)
    path = (root / (repo or ".")).resolve()
    if not path.is_relative_to(root):
        raise ProjectError("Dépôt hors du projet")
    if not (path / ".git").exists():
        raise ProjectError(f"« {repo} » n'est pas un dépôt git")
    return path


def _find(root, depth=MAX_DEPTH):
    out = []
    if (root / ".git").exists():
        out.append(root)
    skip = {"node_modules", ".venv", "venv", "__pycache__", ".git", "dist", "build", ".devpilot", ".next", ".cache"}

    def walk(d, level):
        if level > depth:
            return
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if not e.is_dir() or e.is_symlink() or e.name in skip or e.name.startswith("."):
                continue
            if (e / ".git").exists():
                out.append(e)
            walk(e, level + 1)
    walk(root, 1)
    return out


def repo_state(root, path, fetch=False):
    if fetch:
        _git(path, "fetch", "-q", "--prune", "origin", timeout=40)
    base = base_branch(path)
    ob = f"origin/{base}" if _exists(path, f"origin/{base}") else base
    _, branch, _ = _git(path, "branch", "--show-current")
    _, porc, _ = _git(path, "status", "--porcelain")
    lines = [l for l in porc.splitlines() if l.strip()]
    _, upstream, _ = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ahead = behind = 0
    if upstream and not upstream.startswith("fatal"):
        ahead = _n(_git(path, "rev-list", "--count", f"{upstream}..HEAD")[1])
        behind = _n(_git(path, "rev-list", "--count", f"HEAD..{upstream}")[1])
    else:
        upstream = ""
    _, last, _ = _git(path, "log", "-1", "--format=%h\x1f%s\x1f%cI")
    sha, msg, when = (last.split("\x1f") + ["", "", ""])[:3]
    _, remote, _ = _git(path, "remote", "get-url", "origin")
    work = _work_state(path, branch, base, ob, upstream, ahead, behind)
    rel = str(path.relative_to(root)) if path != root else "."
    return {"dir": rel, "name": path.name if path != root else root.name, "path": str(path),
            "branch": branch or "(détachée)", "base": base, "on_base": branch == base,
            "modified": len([l for l in lines if not l.startswith("??")]), "untracked": len([l for l in lines if l.startswith("??")]),
            "upstream": upstream, "ahead": ahead, "behind": behind,
            "for_base": _n(_git(path, "rev-list", "--count", f"{ob}..HEAD")[1]) if branch != base else 0,
            "base_behind": _n(_git(path, "rev-list", "--count", f"{base}..{ob}")[1]) if ob != base and _exists(path, f"refs/heads/{base}") else 0,
            "last": {"sha": sha, "message": msg[:100], "when": when}, "remote": remote,
            "rebasing": (path / ".git" / "rebase-merge").exists() or (path / ".git" / "rebase-apply").exists(),
            "merging": (path / ".git" / "MERGE_HEAD").exists(), **work}


def _integrated(path, ob):
    """Is everything this branch changes already in main? (also after a squash merge: the
    branch's commits are not in main, but merging it would change nothing)."""
    rc, out, _ = _git(path, "merge-tree", "--write-tree", ob, "HEAD")
    if rc != 0 or not out:
        return False
    return out.split()[0] == _git(path, "rev-parse", f"{ob}^{{tree}}")[1]


def _work_state(path, branch, base, ob, upstream, ahead, behind):
    """The work branch on the way to main (branch map):
    pushed      : on GitHub (origin/<branch>) and nothing left to push
    to_push     : local commits not on GitHub (all of them when the branch was never pushed)
    to_pull     : commits on GitHub not here yet (pushed from another PC)
    merge       : none (on main) | empty (nothing yet) | todo (commits for main) | done (already in main)
    others      : the other local branches (not main, not this one)"""
    _, loc, _ = _git(path, "for-each-ref", "refs/heads", "--format=%(refname:short)")
    others = [b for b in loc.splitlines() if b and b not in (base, branch)]
    res = {"remote_branch": False, "pushed": False, "to_push": 0, "to_pull": 0, "merge": "none", "others": others[:20]}
    if not branch or branch == base:
        return res
    gh = upstream if upstream else (f"origin/{branch}" if _exists(path, f"origin/{branch}") else "")
    if gh:
        res["remote_branch"] = True
        res["to_push"] = ahead if upstream else _n(_git(path, "rev-list", "--count", f"{gh}..HEAD")[1])
        res["to_pull"] = behind if upstream else _n(_git(path, "rev-list", "--count", f"HEAD..{gh}")[1])
        res["pushed"] = res["to_push"] == 0
    else:
        res["to_push"] = _n(_git(path, "rev-list", "--count", f"{ob}..HEAD")[1])
    if _n(_git(path, "rev-list", "--count", f"{ob}..HEAD")[1]) == 0:
        res["merge"] = "empty"
    else:
        res["merge"] = "done" if _integrated(path, ob) else "todo"
    return res


def repos(pid, fetch=False):
    p, root = project_root(pid)
    from concurrent.futures import ThreadPoolExecutor
    found = _find(root)
    with ThreadPoolExecutor(max_workers=8) as ex:
        states = list(ex.map(lambda r: repo_state(root, r, fetch), found))
    return {"project": p["name"], "root": str(root), "repos": states}


def branches(pid, repo):
    path = repo_path(pid, repo)
    _git(path, "fetch", "-q", "--prune", "origin", timeout=40)
    _, cur, _ = _git(path, "branch", "--show-current")
    _, loc, _ = _git(path, "for-each-ref", "refs/heads", "--sort=-committerdate", "--format=%(refname:short)\x1f%(committerdate:iso-strict)\x1f%(upstream:short)")
    out, seen = [], set()
    for l in loc.splitlines():
        name, when, up = (l.split("\x1f") + ["", "", ""])[:3]
        seen.add(name)
        out.append({"name": name, "local": True, "remote": bool(up) or _exists(path, f"origin/{name}"), "when": when,
                    "current": name == cur})
    _, rem, _ = _git(path, "for-each-ref", "refs/remotes/origin", "--sort=-committerdate", "--format=%(refname:short)\x1f%(committerdate:iso-strict)")
    for l in rem.splitlines():
        ref, when = (l.split("\x1f") + ["", ""])[:2]
        name = ref[len("origin/"):]
        if name in ("HEAD", "") or name in seen or ref == "origin":
            continue
        out.append({"name": name, "local": False, "remote": True, "when": when, "current": False})
    return {"current": cur, "base": base_branch(path), "branches": out}


def _dirty(path):
    _, porc, _ = _git(path, "status", "--porcelain", "--untracked-files=no")
    # « XY fichier » : _git a retiré l'espace de tête de la 1re ligne → on découpe, on ne compte pas les colonnes
    return [l.split(maxsplit=1)[1] for l in porc.splitlines() if len(l.split(maxsplit=1)) == 2]


def _refuse_if_dirty(path, stash, what):
    files = _dirty(path)
    if not files:
        return None
    if not stash:
        raise ProjectError(f"{len(files)} fichier(s) modifié(s) non commité(s) ({', '.join(files[:4])}{'…' if len(files) > 4 else ''}) : "
                           f"commit-les, ou coche « mettre de côté » pour {what} (git stash).", )
    rc, _, err = _git(path, "stash", "push", "-m", f"DevPilot : avant {what}")
    if rc != 0:
        raise ProjectError("Impossible de mettre les modifications de côté : " + err[-200:])
    return f"modifications mises de côté (git stash list → « DevPilot : avant {what} »)"


def switch(pid, repo, branch, stash=False):
    path = repo_path(pid, repo)
    if not branch or branch.startswith("-"):
        raise ProjectError("Branche invalide")
    note = _refuse_if_dirty(path, stash, f"de passer sur {branch}")
    if _exists(path, f"refs/heads/{branch}"):
        rc, _, err = _git(path, "switch", branch)
    elif _exists(path, f"origin/{branch}"):
        rc, _, err = _git(path, "switch", "--track", f"origin/{branch}")
    else:
        raise ProjectError(f"Branche « {branch} » inconnue")
    if rc != 0:
        raise ProjectError("Changement de branche refusé : " + (err.splitlines()[-1] if err else "?"))
    return {"branch": branch, "note": note}


def valid_name(path, name):
    if not name or len(name) > 100:
        return False
    return _git(path, "check-ref-format", "--branch", name)[0] == 0 and not name.startswith("-")


def new_branch(pid, repo, name, stash=False, carry=False):
    """A new branch ALWAYS starts from the latest main on GitHub — never from wherever you happen to be."""
    path = repo_path(pid, repo)
    name = (name or "").strip()
    if not valid_name(path, name):
        raise ProjectError("Nom de branche invalide (ex. fix/message-connexion, feat/export-pdf)")
    if _exists(path, f"refs/heads/{name}") or _exists(path, f"origin/{name}"):
        raise ProjectError(f"La branche « {name} » existe déjà — choisis-la dans la liste")
    base = base_branch(path)
    _git(path, "fetch", "-q", "origin", base, timeout=60)
    start = f"origin/{base}" if _exists(path, f"origin/{base}") else base
    # carry: the files modified (on main, by mistake) go along to the new branch
    note = None if carry else _refuse_if_dirty(path, stash, f"de créer {name}")
    rc, _, err = _git(path, "switch", "--no-track", "-c", name, start)
    if rc != 0:
        if carry and "overwritten" in err:
            raise ProjectError("Tes modifications touchent des fichiers qui ont changé sur GitHub : "
                               "coche plutôt « mettre de côté » (git stash), puis reprends-les avec git stash pop.")
        raise ProjectError("Création refusée : " + (err.splitlines()[-1] if err else "?"))
    if carry and _dirty(path):
        note = "tes modifications ont suivi sur la nouvelle branche"
    return {"branch": name, "from": start, "note": note}


def update_main(pid, repo):
    """Bring the local main up to date with GitHub (fast-forward only), wherever you are."""
    path = repo_path(pid, repo)
    base = base_branch(path)
    rc, _, err = _git(path, "fetch", "-q", "origin", base, timeout=60)
    if rc != 0:
        raise ProjectError("GitHub injoignable : " + (err.splitlines()[-1] if err else "?"))
    _, cur, _ = _git(path, "branch", "--show-current")
    if cur == base:
        rc, out, err = _git(path, "merge", "--ff-only", f"origin/{base}")
    elif _exists(path, f"refs/heads/{base}"):
        rc, out, err = _git(path, "fetch", "-q", "origin", f"{base}:{base}")
    else:
        rc, out, err = _git(path, "branch", "--track", base, f"origin/{base}")
    if rc != 0:
        raise ProjectError(f"{base} n'a pas pu avancer simplement (des commits locaux sur {base} ?) : "
                           + (err.splitlines()[-1] if err else "?"))
    return {"base": base, "message": f"{base} est à jour"}


def _gh_repo(url):
    """git@github.com:org/repo.git | https://github.com/org/repo(.git) → org/repo"""
    import re
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else ""


def prs(pid):
    """The pull request of each work branch, read with gh: {dir: {number, state, url}}.
    Empty when gh is missing or not logged in — the map then simply shows no PR."""
    if not shutil.which("gh"):
        return {}
    from concurrent.futures import ThreadPoolExecutor
    import json
    st = repos(pid)["repos"]

    def one(r):
        gh = _gh_repo(r["remote"])
        if r["on_base"] or not gh or r["branch"].startswith("("):
            return r["dir"], None
        try:
            out = subprocess.run(["gh", "pr", "list", "-R", gh, "--head", r["branch"], "--state", "all", "--limit", "1",
                                  "--json", "number,state,url"], capture_output=True, text=True, timeout=20)
            l = json.loads(out.stdout or "[]") if out.returncode == 0 else []
        except (OSError, ValueError, subprocess.TimeoutExpired):
            l = []
        return r["dir"], (l[0] if l else None)
    with ThreadPoolExecutor(max_workers=8) as ex:
        return {d: x for d, x in ex.map(one, st) if x}


# ── mettre à jour les dépôts : la branche de chacun est MONTRÉE et CHOISIE ──────────
# Jamais de « git pull » à l'aveugle sur la branche où un dépôt se trouve : resté sur une vieille
# branche, il ne récupérait rien de main (vécu sur plusieurs PC, 2026-10-05).

def _recommande(st):
    """La branche à récupérer par défaut (★) : main si rien n'est en cours, sinon rester (rien ne se perd)."""
    cur, base = st["branch"], st["base"]
    if st["branch"].startswith("("):
        return base, "HEAD détaché"
    if st["modified"]:
        return cur, f"{st['modified']} fichier(s) modifié(s) non commité(s) — reste sur sa branche"
    if cur == base:
        return base, ""
    if st["merge"] == "done" or (st["merge"] == "empty" and not st["to_push"]):
        return base, "branche finie (déjà dans main)" if st["merge"] == "done" or st["remote_branch"] \
            else "branche locale sans travail propre"
    if not st["remote_branch"] or st["to_push"]:
        return cur, f"{st['to_push'] or 'des'} commit(s) jamais poussé(s) — reste sur sa branche"
    return cur, "branche en cours (sur GitHub)"


def plan_maj(pid):
    """Pour chaque dépôt : branche actuelle, état, nouveautés de main, branches possibles, choix ★."""
    p, root = project_root(pid)
    from concurrent.futures import ThreadPoolExecutor

    def un(path):
        st = repo_state(root, path, fetch=True)
        cible, pourquoi = _recommande(st)
        base = st["base"]
        ob = f"origin/{base}"
        nouveautes = _n(_git(path, "rev-list", "--count", f"{'HEAD' if st['on_base'] else base}..{ob}")[1]) if _exists(path, ob) else 0
        _, loc, _ = _git(path, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads")
        _, rem, _ = _git(path, "for-each-ref", "--sort=-committerdate", "--format=%(refname:lstrip=3)", "refs/remotes/origin")
        vues, choix = set(), []
        for b, ici in [(base, None)] + [(x, True) for x in loc.splitlines()] + [(x, False) for x in rem.splitlines()]:
            if not b or b == "HEAD" or b in vues:
                continue
            vues.add(b)
            choix.append({"name": b, "local": _exists(path, f"refs/heads/{b}"), "github": _exists(path, f"origin/{b}")})
        return {"dir": st["dir"], "name": st["name"], "branch": st["branch"], "base": base, "modified": st["modified"],
                "merge": st["merge"], "nouveautes": nouveautes, "cible": cible, "pourquoi": pourquoi, "choix": choix[:15]}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return {"project": p["name"], "repos": list(ex.map(un, _find(root)))}


def appliquer_maj(pid, choix):
    """choix = {dir: branche}. Passe chaque dépôt sur SA branche choisie puis l'avance depuis GitHub
    (avance rapide seulement). Rien n'est écrasé : modifications non commitées → le dépôt reste."""
    _, root = project_root(pid)
    out = []
    for d, cible in (choix or {}).items():
        try:
            path = repo_path(pid, d)
        except ProjectError as e:
            out.append({"dir": d, "ok": False, "message": e.message}); continue
        if not valid_name(path, cible):
            out.append({"dir": d, "ok": False, "message": f"branche invalide : {cible}"}); continue
        _git(path, "fetch", "-q", "--prune", "origin", timeout=60)       # l'état de GitHub MAINTENANT
        _, cur, _ = _git(path, "branch", "--show-current")
        avant = _git(path, "rev-parse", "--short", "HEAD")[1]
        if cible != cur:
            if _dirty(path):
                out.append({"dir": d, "ok": False, "message": f"modifications non commitées — reste sur {cur} (commite ou mets de côté, puis recommence)"})
                continue
            if _exists(path, f"refs/heads/{cible}"):
                rc, _, err = _git(path, "switch", cible)
            elif _exists(path, f"origin/{cible}"):
                rc, _, err = _git(path, "switch", "--track", f"origin/{cible}")
            else:
                out.append({"dir": d, "ok": False, "message": f"branche inconnue : {cible}"}); continue
            if rc != 0:
                out.append({"dir": d, "ok": False, "message": "changement de branche refusé : " + (err.splitlines()[-1] if err else "?")}); continue
        if _exists(path, f"origin/{cible}"):
            rc, _, err = _git(path, "merge", "--ff-only", f"origin/{cible}")
            if rc != 0:
                out.append({"dir": d, "ok": False, "branch": cible,
                            "message": f"{cible} a des commits absents de GitHub — laissé tel quel"}); continue
        apres = _git(path, "rev-parse", "--short", "HEAD")[1]
        _, msg, _ = _git(path, "log", "-1", "--format=%s")
        out.append({"dir": d, "ok": True, "branch": cible, "avant": avant, "apres": apres, "change": avant != apres or cible != cur,
                    "de": cur, "message": msg[:90]})
    return {"resultats": out}


# ── tools ───────────────────────────────────────────────────────────────────

def editor_cmd():
    return (db.get_setting("editor_cmd", "") or "code").strip()


def claude_cmd():
    return (db.get_setting("claude_cmd", "") or "claude").strip()


def tools():
    """Which tools exist on this machine (the UI greys out the missing ones)."""
    ed = shlex.split(editor_cmd())[0] if editor_cmd() else ""
    cl = shlex.split(claude_cmd())[0] if claude_cmd() else ""
    term = next((t for t in ("gnome-terminal", "x-terminal-emulator", "konsole", "xterm") if shutil.which(t)), None)
    return {"persistence": {"ok": bool(shutil.which("tmux")), "cmd": "tmux"},
            "editor": {"cmd": editor_cmd(), "ok": bool(ed and shutil.which(ed))},
            "claude": {"cmd": claude_cmd(), "ok": bool(cl and shutil.which(cl))},
            "external_terminal": {"cmd": term, "ok": bool(term)}}


def _detach(argv, cwd):
    subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def open_editor(pid, repo):
    path = repo_path(pid, repo)
    argv = shlex.split(editor_cmd())
    if not argv or not shutil.which(argv[0]):
        raise ProjectError(f"Éditeur introuvable : « {editor_cmd()} ». Réglages → commande de l'éditeur (ex. code, codium, subl).")
    _detach(argv + [str(path)], path)
    return {"opened": str(path), "with": argv[0]}


def open_external(pid, repo, claude=False):
    p, _ = project_root(pid)
    path = repo_path(pid, repo)
    inner = (f"{claude_cmd()}; exec bash" if claude else "exec bash")
    title = f"DevPilot — {p['name']} · {path.name}" + (" · Claude" if claude else "")
    if shutil.which("gnome-terminal"):
        argv = ["gnome-terminal", "--title", title, "--working-directory", str(path), "--", "bash", "-lc", inner]
    elif shutil.which("konsole"):
        argv = ["konsole", "--workdir", str(path), "-e", "bash", "-lc", inner]
    elif shutil.which("x-terminal-emulator"):
        argv = ["x-terminal-emulator", "-e", "bash", "-lc", f"cd {shlex.quote(str(path))} && {inner}"]
    elif shutil.which("xterm"):
        argv = ["xterm", "-T", title, "-e", "bash", "-lc", f"cd {shlex.quote(str(path))} && {inner}"]
    else:
        raise ProjectError("Aucun terminal externe trouvé (gnome-terminal, konsole, xterm)")
    if claude and not tools()["claude"]["ok"]:
        raise ProjectError(f"Claude introuvable : « {claude_cmd()} ». Réglages → commande de Claude.")
    _detach(argv, path)
    return {"opened": str(path), "with": argv[0]}


