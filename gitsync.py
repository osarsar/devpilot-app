"""DevPilot — when the project folder and its GitHub repo differ.

1. At link time (folder had its own files, the repo has code, no common history):
   compare() shows the differences, reconcile() applies the user's choice:
     "github"  the folder becomes the repo (local files backed up first)
     "local"   the repo's code as base, the folder's files on top, one new commit
     "merge"   both kept; each file that differs: "local" | "github" | "edit"
   GitHub's history is never rewritten, and a backup is always made first.

2. While working (both sides moved): status() / sync() with pull --rebase,
   conflicts resolved file by file, abort() puts everything back.
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

import db
import projects as P
from projects import ProjectError, git

BACKUP_DIR = "sauvegardes"          # .devpilot/sauvegardes/<timestamp>/


# ── helpers ─────────────────────────────────────────────────────────────────

def _repo(pid, repo_dir=None):
    """Path of the project's repo (the folder, or one of its repos)."""
    project = P.get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        raise ProjectError("Le dossier du projet est introuvable")
    root = Path(project["path"])
    path = (root / repo_dir).resolve() if repo_dir and repo_dir != "." else root
    if not path.is_relative_to(root) or not (path / ".git").exists():
        raise ProjectError(f"{repo_dir or 'Le dossier'} n'est pas un depot git du projet")
    return project, path


def _ok(rc, err, what):
    if rc != 0:
        raise ProjectError(f"{what} a echoue : {err[-400:]}")


def _lines(out):
    return [l for l in out.splitlines() if l.strip()]


def _unborn(path):
    rc, _, _ = git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=path)
    return rc != 0


def _branch(path):
    rc, out, _ = git(["symbolic-ref", "--short", "HEAD"], cwd=path)
    return out if rc == 0 else ""


def _backup(path, files):
    """Copy files (relative paths) into .devpilot/sauvegardes/<ts>/ before anything changes."""
    if not files:
        return None
    dest = Path(path) / ".devpilot" / BACKUP_DIR / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    for f in files:
        src = Path(path) / f
        if src.is_file() or src.is_symlink():
            (dest / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / f, follow_symlinks=False)
    P.exclude_from_git(path)
    return str(dest)


# ── 1. At link time ─────────────────────────────────────────────────────────

def pending_link(path):
    """Linked but not reconciled yet: no commit here, origin has a branch, and local files."""
    path = Path(path)
    if not (path / ".git").exists() or not _unborn(path):
        return None
    branch = _branch(path) or "main"
    rc, _, _ = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=path)
    if rc != 0:
        return None
    return branch


def compare(pid, repo_dir=None):
    """Folder files vs origin/<branch>: identical / different / only_local / only_github."""
    project, path = _repo(pid, repo_dir)
    branch = pending_link(path)
    if not branch:
        raise ProjectError("Rien a comparer : le projet n'attend pas de reconciliation")
    ref = f"origin/{branch}"

    rc, out, err = git(["ls-tree", "-r", ref], cwd=path)
    _ok(rc, err, "Lecture du depot")
    remote = {}
    for line in _lines(out):
        meta, name = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob":
            remote[name] = sha

    # local files git would track: the folder's own ignores + the repo's root .gitignore
    extra = []
    rc, gi, _ = git(["show", f"{ref}:.gitignore"], cwd=path)
    if rc == 0 and gi:
        tmp = path / ".git" / "devpilot-remote-gitignore"
        tmp.write_text(gi + "\n", encoding="utf-8")
        extra = [f"--exclude-from={tmp}"]
    rc, out, err = git(["ls-files", "--others", "--exclude-standard", "-z"] + extra, cwd=path)
    _ok(rc, err, "Liste des fichiers locaux")
    local = [f for f in out.split("\0") if f]

    hashes = {}
    if local:                     # via stdin: no argv limit with thousands of files
        import subprocess
        r = subprocess.run(["git", "hash-object", "--stdin-paths"], cwd=path, input="\n".join(local),
                           capture_output=True, text=True, timeout=300)
        _ok(r.returncode, r.stderr, "Empreinte des fichiers locaux")
        hashes = dict(zip(local, r.stdout.splitlines()))

    res = {"branch": branch, "identical": [], "different": [], "only_local": [], "only_github": []}
    for f in sorted(local):
        if f not in remote:
            res["only_local"].append(f)
        elif remote[f] == hashes.get(f):
            res["identical"].append(f)
        else:
            res["different"].append(f)
    res["only_github"] = sorted(set(remote) - set(local))
    res["repo"] = repo_dir or "."
    return res


def reconcile(pid, mode, choices=None, repo_dir=None, message=None):
    """Apply the user's choice. choices (merge mode): {file: "local"|"github"|"edit"}.
    Returns {status: "done"|"edit", ...}. Nothing is pushed: the user pushes."""
    if mode not in ("github", "local", "merge"):
        raise ProjectError(f"Choix inconnu : {mode}")
    cmp_ = compare(pid, repo_dir)
    project, path = _repo(pid, repo_dir)
    branch, ref = cmp_["branch"], f"origin/{cmp_['branch']}"
    choices = dict(choices or {})
    if mode == "github":
        choices = {f: "github" for f in cmp_["different"]}
    elif mode == "local":
        choices = {f: "local" for f in cmp_["different"]}
    else:
        missing = [f for f in cmp_["different"] if choices.get(f) not in ("local", "github", "edit")]
        if missing:
            raise ProjectError("Choisis une version pour chaque fichier different",
                               details={"missing": missing})

    backup = _backup(path, cmp_["different"] + cmp_["only_local"])

    # HEAD + index = GitHub's version; the working tree still holds the folder's files
    rc, _, err = git(["reset", "-q", ref], cwd=path)
    _ok(rc, err, "Mise en place de la base GitHub")
    git(["config", f"branch.{branch}.remote", "origin"], cwd=path)
    git(["config", f"branch.{branch}.merge", f"refs/heads/{branch}"], cwd=path)

    if cmp_["only_github"]:                                    # files the folder did not have
        rc, _, err = git(["checkout", ref, "--"] + cmp_["only_github"], cwd=path)
        _ok(rc, err, "Recuperation des fichiers GitHub")
    take_github = [f for f, c in choices.items() if c == "github"]
    if take_github:
        rc, _, err = git(["checkout", ref, "--"] + take_github, cwd=path)
        _ok(rc, err, "Recuperation des versions GitHub")
    if mode == "github":
        for f in cmp_["only_local"]:                           # the folder becomes the repo (backed up)
            (path / f).unlink(missing_ok=True)
        _prune_empty_dirs(path, cmp_["only_local"])
        P.write_space(pid)
        return {"status": "done", "backup": backup, "commit": None, "branch": branch,
                "message": f"Le dossier est maintenant la copie de GitHub ({branch}). "
                           f"Tes fichiers d'avant sont dans {backup}." if backup else
                           f"Le dossier est maintenant la copie de GitHub ({branch})."}

    to_edit = [f for f, c in choices.items() if c == "edit"]
    for f in to_edit:                                          # both versions, with conflict markers
        _write_both(path, ref, f)
    if to_edit:
        (path / ".git" / "devpilot-reconcile").write_text("\n".join(to_edit), encoding="utf-8")
        P.write_space(pid)
        return {"status": "edit", "files": to_edit, "backup": backup, "branch": branch,
                "message": "Edite ces fichiers (les deux versions sont dedans, entre <<<<<<< et >>>>>>>), "
                           "puis clique « Terminer la fusion »."}
    return _commit_reconcile(pid, path, branch, mode, backup, message)


def finish_reconcile(pid, repo_dir=None, message=None):
    project, path = _repo(pid, repo_dir)
    marker = path / ".git" / "devpilot-reconcile"
    if not marker.exists():
        raise ProjectError("Aucune fusion en cours")
    files = _lines(marker.read_text(encoding="utf-8"))
    left = [f for f in files if _has_markers(path / f)]
    if left:
        raise ProjectError("Ces fichiers contiennent encore les deux versions (<<<<<<< >>>>>>>) : " + ", ".join(left),
                           details={"files": left})
    marker.unlink()
    return _commit_reconcile(pid, path, _branch(path) or "main", "merge", None, message)


def _commit_reconcile(pid, path, branch, mode, backup, message):
    rc, _, err = git(["add", "-A"], cwd=path)
    _ok(rc, err, "git add")
    rc, out, _ = git(["diff", "--cached", "--name-only"], cwd=path)
    commit = None
    if out.strip():
        msg = message or ("Import du dossier local par-dessus GitHub" if mode == "local"
                          else "Fusion du dossier local avec GitHub")
        rc, _, err = git(["commit", "-q", "-m", msg], cwd=path)
        _ok(rc, err, "git commit")
        commit = git(["rev-parse", "--short", "HEAD"], cwd=path)[1]
    P.write_space(pid)
    return {"status": "done", "backup": backup, "commit": commit, "branch": branch,
            "message": (f"Commit {commit} cree sur {branch}. " if commit else "Aucune difference a commiter. ")
                       + "Clique « Synchroniser » (ou git push) pour l'envoyer sur GitHub."}


def _write_both(path, ref, f):
    rc, theirs, _ = git(["show", f"{ref}:{f}"], cwd=path)
    mine = (path / f).read_text(encoding="utf-8", errors="replace")
    (path / f).write_text(f"<<<<<<< ton dossier\n{mine.rstrip()}\n=======\n{theirs.rstrip()}\n>>>>>>> GitHub\n",
                          encoding="utf-8")


def _has_markers(file):
    try:
        text = Path(file).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "<<<<<<< " in text and ">>>>>>> " in text


def _prune_empty_dirs(path, files):
    for f in sorted({str(Path(f).parent) for f in files}, key=len, reverse=True):
        d = Path(path) / f
        while d != Path(path) and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent


# ── 2. While working: status / sync (pull --rebase) / conflicts ─────────────

STASH_MARK = "devpilot-sync"


def _rebasing(path):
    g = Path(path) / ".git"
    return (g / "rebase-merge").exists() or (g / "rebase-apply").exists()


def _conflicts(path):
    rc, out, _ = git(["diff", "--name-only", "--diff-filter=U"], cwd=path)
    return _lines(out)


def status(pid, repo_dir=None, fetch=True):
    project, path = _repo(pid, repo_dir)
    if fetch:
        git(["fetch", "-q", "origin"], cwd=path, timeout=60)
    branch = _branch(path)
    st = P.git_state(path)
    res = {"repo": repo_dir or ".", "branch": branch, "modified": st.get("modified", 0),
           "untracked": st.get("untracked", 0), "ahead": 0, "behind": 0, "upstream": None,
           "rebasing": _rebasing(path), "conflicts": [], "pending_link": bool(pending_link(path)),
           "stashed": (path / ".git" / STASH_MARK).exists(),
           "reconcile_edit": _lines((path / ".git" / "devpilot-reconcile").read_text())
           if (path / ".git" / "devpilot-reconcile").exists() else []}
    rc, up, _ = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=path)
    if rc == 0:
        res["upstream"] = up
        rc, out, _ = git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=path)
        if rc == 0 and out:
            res["ahead"], res["behind"] = (int(x) for x in out.split())
    elif branch and not _unborn(path):
        rc, out, _ = git(["rev-list", "--count", "HEAD", "--not", "--remotes"], cwd=path)
        res["ahead"] = int(out) if rc == 0 and out.isdigit() else 0
    if res["rebasing"]:
        res["conflicts"] = _conflicts(path)
    return res


def sync(pid, repo_dir=None, commit_message=None, stash=False):
    """Commit (or put aside) local changes, pull --rebase, push. Stops on conflict."""
    project, path = _repo(pid, repo_dir)
    if _rebasing(path):
        raise ProjectError("Une synchronisation est deja en cours : resous les conflits ou annule-la.")
    if pending_link(path):
        raise ProjectError("Termine d'abord la liaison avec GitHub (comparaison du dossier et du depot).")
    st = status(pid, repo_dir)
    log = []

    if st["modified"] or st["untracked"]:
        if commit_message and commit_message.strip():
            for args in (["add", "-A"], ["commit", "-q", "-m", commit_message.strip()]):
                rc, _, err = git(args, cwd=path)
                _ok(rc, err, "git " + args[0])
            log.append("changements commites")
        elif stash:
            rc, _, err = git(["stash", "push", "-u", "-q", "-m", STASH_MARK], cwd=path)
            _ok(rc, err, "Mise de cote")
            (path / ".git" / STASH_MARK).write_text("1")
            log.append("changements mis de cote")
        else:
            raise ProjectError("Tu as des changements non commites : donne un message de commit, "
                               "ou mets-les de cote le temps de la synchro.",
                               details={"needs_commit": True, "modified": st["modified"], "untracked": st["untracked"]})

    if st["upstream"]:
        rc, out, err = git(["pull", "--rebase", "-q"], cwd=path, timeout=300)
        if rc != 0:
            if _rebasing(path):
                return {"status": "conflict", "conflicts": _conflicts(path), "log": log,
                        "message": "Le meme fichier a ete modifie ici et sur GitHub. Choisis la version a garder."}
            raise ProjectError("Recuperation depuis GitHub echouee : " + P._explain_git_error(err))
        log.append("GitHub recupere")
    return _push_and_restore(path, log)


def _push_and_restore(path, log):
    rc, ahead, _ = git(["rev-list", "--count", "@{u}..HEAD"], cwd=path)
    no_upstream = rc != 0
    if no_upstream or (ahead.isdigit() and int(ahead) > 0):
        rc, _, err = git(["push", "-u", "origin", "HEAD"], cwd=path, timeout=300)
        if rc != 0:
            raise ProjectError("Envoi vers GitHub echoue : " + P._explain_git_error(err))
        log.append("envoye sur GitHub")
    restored = _restore_stash(path)
    if restored is False:
        return {"status": "stash_conflict", "log": log, "conflicts": _conflicts(path),
                "message": "Synchronise. Tes changements mis de cote entrent en conflit avec GitHub : "
                           "ils sont gardes dans le stash (git stash list)."}
    if restored:
        log.append("changements remis en place")
    return {"status": "done", "log": log, "message": "Synchronise : " + ", ".join(log or ["deja a jour"])}


def _restore_stash(path):
    mark = Path(path) / ".git" / STASH_MARK
    if not mark.exists():
        return None
    mark.unlink()
    rc, _, _ = git(["stash", "pop", "-q"], cwd=path)
    if rc != 0:
        git(["reset", "-q", "--merge"], cwd=path)       # leave the stash entry intact
        return False
    return True


def resolve(pid, file, choice, repo_dir=None):
    """During the rebase: 'local' (your version) | 'github' | 'edited' (you fixed the file)."""
    project, path = _repo(pid, repo_dir)
    if not _rebasing(path):
        raise ProjectError("Aucun conflit en cours")
    if file not in _conflicts(path):
        raise ProjectError(f"{file} n'est pas en conflit")
    # in a rebase, "ours" is the GitHub side and "theirs" is your commit being replayed
    if choice in ("local", "github"):
        rc, _, err = git(["checkout", "--theirs" if choice == "local" else "--ours", "--", file], cwd=path)
        if rc != 0:           # deleted on one side: take the deletion or the file accordingly
            git(["rm", "-q", "--", file], cwd=path)
            return {"conflicts": _conflicts(path)}
    elif choice == "edited":
        if _has_markers(path / file):
            raise ProjectError(f"{file} contient encore <<<<<<< / >>>>>>> : termine l'edition")
    else:
        raise ProjectError(f"Choix inconnu : {choice}")
    rc, _, err = git(["add", "--", file], cwd=path)
    _ok(rc, err, "git add")
    return {"conflicts": _conflicts(path)}


def continue_sync(pid, repo_dir=None):
    project, path = _repo(pid, repo_dir)
    if not _rebasing(path):
        raise ProjectError("Aucune synchronisation en cours")
    left = _conflicts(path)
    if left:
        raise ProjectError("Reste a choisir : " + ", ".join(left), details={"conflicts": left})
    env_editor = ["-c", "core.editor=true"]
    rc, _, err = git(["rebase", "--continue"], cwd=path, extra=env_editor, timeout=120)
    if rc != 0 and _rebasing(path):
        conflicts = _conflicts(path)
        if conflicts:
            return {"status": "conflict", "conflicts": conflicts,
                    "message": "Un autre de tes commits touche aussi ces fichiers : choisis encore."}
        # nothing left to apply for this commit (your change equals GitHub's)
        rc, _, err = git(["rebase", "--skip"], cwd=path, timeout=120)
        if _rebasing(path):
            return {"status": "conflict", "conflicts": _conflicts(path), "message": "Choisis encore."}
    return _push_and_restore(path, ["conflits resolus"])


def abort(pid, repo_dir=None):
    """Put everything back as it was before « Synchroniser »."""
    project, path = _repo(pid, repo_dir)
    if _rebasing(path):
        rc, _, err = git(["rebase", "--abort"], cwd=path)
        _ok(rc, err, "Annulation")
    restored = _restore_stash(path)
    return {"status": "aborted", "message": "Synchro annulee : tout est comme avant."
            + (" (tes changements mis de cote sont dans git stash list)" if restored is False else "")}
