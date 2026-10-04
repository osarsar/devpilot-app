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
    rel = str(path.relative_to(root)) if path != root else "."
    return {"dir": rel, "name": path.name if path != root else root.name, "path": str(path),
            "branch": branch or "(détachée)", "base": base, "on_base": branch == base,
            "modified": len([l for l in lines if not l.startswith("??")]), "untracked": len([l for l in lines if l.startswith("??")]),
            "upstream": upstream, "ahead": ahead, "behind": behind,
            "for_base": _n(_git(path, "rev-list", "--count", f"{ob}..HEAD")[1]) if branch != base else 0,
            "base_behind": _n(_git(path, "rev-list", "--count", f"{base}..{ob}")[1]) if ob != base and _exists(path, f"refs/heads/{base}") else 0,
            "last": {"sha": sha, "message": msg[:100], "when": when}, "remote": remote,
            "rebasing": (path / ".git" / "rebase-merge").exists() or (path / ".git" / "rebase-apply").exists(),
            "merging": (path / ".git" / "MERGE_HEAD").exists()}


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


def new_branch(pid, repo, name, stash=False):
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
    note = _refuse_if_dirty(path, stash, f"de créer {name}")
    rc, _, err = _git(path, "switch", "--no-track", "-c", name, start)
    if rc != 0:
        raise ProjectError("Création refusée : " + (err.splitlines()[-1] if err else "?"))
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
    return {"editor": {"cmd": editor_cmd(), "ok": bool(ed and shutil.which(ed))},
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


