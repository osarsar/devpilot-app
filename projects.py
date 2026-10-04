"""DevPilot — project lifecycle: the one place that creates, registers, moves
and removes projects. Every route goes through here, so the rules below hold
whatever button was clicked:

- a project lives in ~/devpilot/projects/<name> by default; any other place
  only when the user chose it explicitly (``custom_location=True``)
- a project is ONE folder that may contain several git repos
- nothing is deleted without an inspection (git, docker, processes) and the
  project name typed back; deleted files go to the system trash
- DevPilot never overwrites something it did not create
"""

import base64
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import psutil

import db

HOME = Path.home().resolve()
DEVPILOT_ROOT = HOME / "devpilot"
PROJECTS_ROOT = DEVPILOT_ROOT / "projects"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build",
             ".dart_tool", ".gradle", "target", ".cache", ".tox"}
TECH_COLORS = {
    "package.json": "#fbbf24", "pubspec.yaml": "#38bdf8", "requirements.txt": "#34d399",
    "pyproject.toml": "#34d399", "build.gradle": "#06b6d4", "Cargo.toml": "#fb923c",
    "go.mod": "#60a5fa",
}

# Called with the project dict after it is removed or moved (the dashboard
# uses it to close the project's terminal sessions).
on_removed = []
# Callables returning pids to ignore in inspections: DevPilot's own idle
# terminal shells (what runs INSIDE them still counts).
ignored_pids = []


class ProjectError(Exception):
    """A refused operation. ``details`` carries the inspection report, if any."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details


# ── Paths and names ─────────────────────────────────────────────────────────

def validate_name(name):
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ProjectError(f'Nom invalide "{name}" : lettres, chiffres, . _ - '
                           "(pas d'espace ni d'accent, 64 caracteres max)")
    return name


def name_from_url(url):
    tail = url.rstrip("/").split("/")[-1].split(":")[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def resolve(raw):
    """Absolute, symlink-free path. ``~`` is expanded."""
    return Path(os.path.expanduser(str(raw).strip())).resolve()


def default_path(name):
    return PROJECTS_ROOT / name


def is_managed(path):
    """True when the folder is inside ~/devpilot/projects."""
    path = resolve(path)
    return path != PROJECTS_ROOT and path.is_relative_to(PROJECTS_ROOT)


def _check_location(path, custom_location):
    """Refuse the places a project folder must never be."""
    forbidden = {HOME, DEVPILOT_ROOT, PROJECTS_ROOT, DEVPILOT_ROOT / ".devpilot"}
    if path in forbidden or any(f.is_relative_to(path) for f in forbidden):
        raise ProjectError(f"{path} ne peut pas etre un dossier de projet")
    if path.is_relative_to(DEVPILOT_ROOT / ".devpilot"):
        raise ProjectError("Le dossier d'installation de DevPilot n'est pas un projet")
    if not path.is_relative_to(HOME):
        raise ProjectError(f"{path} est hors de ton home")
    if not is_managed(path) and not custom_location:
        raise ProjectError(f"{path} est hors de ~/devpilot/projects. "
                           "Choisis explicitement cet emplacement pour l'utiliser.")


def _dir_is_empty(path):
    return path.is_dir() and not any(path.iterdir())


# ── Lookups ─────────────────────────────────────────────────────────────────

def _real(p):
    try:
        return resolve(p)
    except (OSError, RuntimeError):
        return None


def all_projects():
    return db.get_projects()


def get(pid):
    project = db.get_project(pid)
    if not project:
        raise ProjectError("Projet introuvable")
    return project


def find_by_path(path, exclude_id=None):
    path = resolve(path)
    for p in all_projects():
        if p["id"] != exclude_id and p.get("path") and _real(p["path"]) == path:
            return p
    return None


def find_by_name(name, exclude_id=None):
    for p in all_projects():
        if p["id"] != exclude_id and p["name"].lower() == name.lower():
            return p
    return None


def overlapping(path, exclude_id=None):
    """Registered projects inside ``path``, or containing it."""
    path = resolve(path)
    out = []
    for p in all_projects():
        other = _real(p["path"]) if p.get("path") else None
        if p["id"] == exclude_id or other is None or other == path:
            continue
        if other.is_relative_to(path):
            out.append({"id": p["id"], "name": p["name"], "path": str(other), "relation": "inside"})
        elif path.is_relative_to(other):
            out.append({"id": p["id"], "name": p["name"], "path": str(other), "relation": "parent"})
    return out


def _check_new(name, path, exclude_id=None):
    """Name and folder are free to be registered."""
    same_path = find_by_path(path, exclude_id)
    if same_path:
        raise ProjectError(f'{path} est deja le projet "{same_path["name"]}"')
    same_name = find_by_name(name, exclude_id)
    if same_name:
        raise ProjectError(f'Un projet "{name}" existe deja ({same_name.get("path") or "sans dossier"})')
    over = overlapping(path, exclude_id)
    if over:
        o = over[0]
        where = "contient" if o["relation"] == "inside" else "est dans"
        raise ProjectError(f'{path} {where} le projet "{o["name"]}" ({o["path"]})')


def unregistered_folders():
    """Folders of ~/devpilot/projects that no project points to."""
    if not PROJECTS_ROOT.is_dir():
        return []
    known = {_real(p["path"]) for p in all_projects() if p.get("path")}
    out = []
    for d in sorted(PROJECTS_ROOT.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and d.resolve() not in known:
            out.append({"name": d.name, "path": str(d.resolve()),
                        "repos": [r["dir"] for r in find_repos(d)]})
    return out


def state(project):
    """ok | missing | no_path"""
    if not project.get("path"):
        return "no_path"
    return "ok" if os.path.isdir(project["path"]) else "missing"


# ── Git ─────────────────────────────────────────────────────────────────────

def _git_env():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"      # never hang on a password prompt
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    return env


def git(args, cwd=None, timeout=30, extra=None, on_output=None):
    """Run git without a shell. ``on_output(line)`` streams stderr (progress)."""
    cmd = ["git"] + (extra or []) + args
    if on_output:
        return _git_stream(cmd, cwd, timeout, on_output)
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=_git_env())
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout ({timeout}s)"
    except FileNotFoundError:
        return 127, "", "git n'est pas installe"


def _git_stream(cmd, cwd, timeout, on_output):
    import threading
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_git_env())
    except FileNotFoundError:
        return 127, "", "git n'est pas installe"
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    err, buf = [], b""
    try:
        while True:
            chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for line in lines:
                if line.strip():
                    text = line.decode(errors="replace")
                    err.append(text)
                    on_output(text)
        out = proc.stdout.read().decode(errors="replace")
        rc = proc.wait()
    finally:
        timer.cancel()
    if buf.strip():
        err.append(buf.decode(errors="replace"))
    if rc == -9:
        return 124, out.strip(), f"timeout ({timeout}s)"
    return rc, out.strip(), "\n".join(err[-40:])


def _remote(repo):
    """origin as recorded in the repo (get-url would apply url.*.insteadOf rewrites)."""
    rc, out, _ = git(["config", "--get", "remote.origin.url"], cwd=repo, timeout=5)
    return out if rc == 0 else ""


def find_repos(path, depth=1):
    """Git repos in the folder itself and its subfolders (down to ``depth``)."""
    path = Path(path)
    repos = []
    if not path.is_dir():
        return repos

    def walk(d, level):
        if (d / ".git").exists():
            rel = str(d.relative_to(path)) if d != path else "."
            repos.append({"dir": rel, "path": str(d), "remote": _remote(d)})
        if level >= depth:
            return
        try:
            subs = sorted(x for x in d.iterdir() if x.is_dir() and not x.is_symlink())
        except OSError:
            return
        for sub in subs:
            if sub.name in SKIP_DIRS or sub.name == ".git":
                continue
            walk(sub, level + 1)

    walk(path, 0)
    return repos


def git_state(repo):
    """What would be lost if this repo disappeared."""
    rc, out, err = git(["status", "--porcelain=v1", "-b"], cwd=repo, timeout=20)
    if rc != 0:
        return {"error": err or "git status a echoue"}
    lines = out.splitlines()
    head = lines[0] if lines and lines[0].startswith("##") else ""
    files = [l for l in lines if not l.startswith("##")]
    rc, unpushed, _ = git(["rev-list", "--count", "--branches", "--not", "--remotes"], cwd=repo, timeout=20)
    rc2, stash, _ = git(["stash", "list"], cwd=repo, timeout=10)
    return {
        "branch": head[3:].split("...")[0] if head else "",
        "modified": sum(1 for l in files if not l.startswith("??")),
        "untracked": sum(1 for l in files if l.startswith("??")),
        "unpushed": int(unpushed) if rc == 0 and unpushed.isdigit() else 0,
        "stash": len(stash.splitlines()) if rc2 == 0 and stash else 0,
        "has_remote": bool(_remote(repo)),
    }


# ── Inspection ──────────────────────────────────────────────────────────────

def _du(path):
    try:
        r = subprocess.run(["du", "-sb", "--", str(path)], capture_output=True, text=True, timeout=60)
        return int(r.stdout.split()[0])
    except Exception:
        return None


def _docker_users(path):
    """Containers whose compose dir or a bind mount is inside ``path``."""
    try:
        ids = subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True, timeout=10)
        if ids.returncode != 0 or not ids.stdout.split():
            return []
        r = subprocess.run(["docker", "inspect"] + ids.stdout.split(),
                           capture_output=True, text=True, timeout=20)
        data = json.loads(r.stdout or "[]")
    except Exception:
        return []
    out = []
    for c in data:
        labels = (c.get("Config") or {}).get("Labels") or {}
        sources = [m.get("Source", "") for m in c.get("Mounts", []) if m.get("Type") == "bind"]
        wd = labels.get("com.docker.compose.project.working_dir", "")
        hits = [s for s in sources + [wd] if s and _real(s) and _real(s).is_relative_to(path)]
        if hits:
            out.append({"name": c.get("Name", "").lstrip("/"),
                        "running": (c.get("State") or {}).get("Running", False),
                        "compose_project": labels.get("com.docker.compose.project", ""),
                        "compose_dir": wd})
    return out


def _processes(path):
    out = []
    ignore = {os.getpid()}
    for fn in ignored_pids:
        try:
            ignore |= set(fn())
        except Exception:
            pass
    for p in psutil.process_iter(["pid", "name", "cwd"]):
        try:
            cwd = p.info.get("cwd")
            if p.info["pid"] not in ignore and cwd and Path(cwd).is_relative_to(path):
                out.append({"pid": p.info["pid"], "name": p.info["name"], "cwd": cwd})
        except (psutil.Error, ValueError):
            continue
    return out


def _venvs(path, max_depth=4):
    out = []
    for root, dirs, files in os.walk(path):
        depth = len(Path(root).relative_to(path).parts)
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".git"} and depth < max_depth]
        if "pyvenv.cfg" in files:
            out.append(root)
            dirs[:] = []
    return out


def inspect(path, with_size=True):
    """Everything that matters before moving or deleting a folder."""
    path = resolve(path)
    report = {"path": str(path), "exists": path.is_dir(), "managed": is_managed(path),
              "repos": [], "docker": [], "processes": [], "venvs": [], "size": None,
              "risks": [], "blockers": []}
    if not path.is_dir():
        return report
    for repo in find_repos(path, depth=4):
        repo["state"] = git_state(repo["path"])
        report["repos"].append(repo)
    report["docker"] = _docker_users(path)
    report["processes"] = _processes(path)
    report["venvs"] = _venvs(path)
    if with_size:
        report["size"] = _du(path)

    risks = report["risks"]
    if not report["repos"]:
        risks.append("Aucun depot git : ces fichiers n'existent nulle part ailleurs")
    for r in report["repos"]:
        s, name = r["state"], r["dir"]
        if s.get("error"):
            risks.append(f"{name} : {s['error']}")
            continue
        if s["modified"] or s["untracked"]:
            risks.append(f"{name} : {s['modified']} fichier(s) modifie(s), {s['untracked']} non suivi(s)")
        if s["unpushed"]:
            risks.append(f"{name} : {s['unpushed']} commit(s) non pousse(s)")
        if s["stash"]:
            risks.append(f"{name} : {s['stash']} stash")
        if not s["has_remote"]:
            risks.append(f"{name} : pas de remote origin")
    running = [c["name"] for c in report["docker"] if c["running"]]
    if running:
        report["blockers"].append("Conteneurs Docker en cours qui utilisent ce dossier : "
                                  + ", ".join(running) + " — arrete-les d'abord")
    elif report["docker"]:
        risks.append("Conteneurs Docker arretes lies a ce dossier : "
                     + ", ".join(c["name"] for c in report["docker"]))
    procs = [f'{p["name"]} ({p["pid"]})' for p in report["processes"]]
    if procs:
        report["blockers"].append("Process lances depuis ce dossier : " + ", ".join(procs[:8]))
    return report


# ── Trash (freedesktop spec, same as the file manager) ──────────────────────

def trash_dir():
    base = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share")
    return base / "Trash"


def move_to_trash(path):
    path = resolve(path)
    tdir = trash_dir()
    (tdir / "files").mkdir(parents=True, exist_ok=True)
    (tdir / "info").mkdir(parents=True, exist_ok=True)
    name, n = path.name, 1
    while (tdir / "files" / name).exists() or (tdir / "info" / f"{name}.trashinfo").exists():
        n += 1
        name = f"{path.name}.{n}"
    info = tdir / "info" / f"{name}.trashinfo"
    info.write_text("[Trash Info]\nPath={}\nDeletionDate={}\n".format(
        quote(str(path)), datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    try:
        shutil.move(str(path), str(tdir / "files" / name))
    except Exception:
        info.unlink(missing_ok=True)
        raise
    return str(tdir / "files" / name)


def safe_rmtree(path, root):
    """rmtree only strictly inside ``root`` (caches, build dirs)."""
    path, root = resolve(path), resolve(root)
    if path == root or not path.is_relative_to(root):
        raise ProjectError(f"Refuse : {path} n'est pas dans {root}")
    shutil.rmtree(path)


# ── Create / register ───────────────────────────────────────────────────────

def _color(path):
    for mf, c in TECH_COLORS.items():
        if (Path(path) / mf).exists():
            return c
    for sub in Path(path).iterdir() if Path(path).is_dir() else []:
        for mf, c in TECH_COLORS.items():
            if (sub / mf).exists():
                return c
    return "#8b5cf6"


def register(name, path, custom_location=False, description="", color=None):
    """Register an existing folder as a project."""
    name = validate_name(name)
    path = resolve(path)
    if not path.is_dir():
        raise ProjectError(f"{path} n'existe pas")
    _check_location(path, custom_location)
    _check_new(name, path)
    repos = find_repos(path)
    if not description and repos:
        description = "Depots : " + ", ".join(r["dir"] for r in repos)
    known = read_manifest(path)                  # a folder that was already a DevPilot project
    pid = db.create_project(name=name, color=color or known.get("color") or _color(path), path=str(path),
                            git_remote=repos[0]["remote"] if repos else "",
                            description=description or known.get("description", ""))
    db.add_rule(pattern=re.escape(name.lower()), project_id=pid, resource_type="any")
    if known:
        _restore_from_space(pid, path)
    write_space(pid)
    return {**db.get_project(pid), "repos": repos}


def _prepare_target(name, path, custom_location):
    """Validated destination for a NEW folder: free, empty or absent."""
    name = validate_name(name)
    path = resolve(path) if path else default_path(name)
    _check_location(path, custom_location)
    _check_new(name, path)
    if path.exists() and not _dir_is_empty(path):
        raise ProjectError(f"Le dossier {path} existe deja et contient des fichiers. "
                           "Pour en faire un projet, utilise l'onglet « Dossier existant ».")
    return name, path


# ── The project's space: .devpilot/ inside its folder ────────────────────────
# Everything that belongs to a project lives in its folder. The DB is the
# index DevPilot searches; .devpilot/ is what travels with the folder, so a
# moved, copied or re-imported folder brings its project back.
#   project.json   name, description, color, status, profile, created_at, git_remote
#   prompt.md      the prompt
#   checklist.json the checklist (checklist.md: readable copy)
#   sessions/      session notes
# (the folder never stores its own path: it can move)

MANIFEST_KEYS = ("name", "description", "color", "status", "profile", "created_at", "git_remote")


def space_dir(path):
    return Path(path) / ".devpilot"


def read_manifest(path):
    try:
        return json.loads((space_dir(path) / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(file, data):
    tmp = file.with_suffix(file.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(file)                              # atomic: never a half-written file


def init_space(path, name):
    """Create .devpilot/ (sessions/ + project.json). Existing files are kept."""
    dp = space_dir(path)
    (dp / "sessions").mkdir(parents=True, exist_ok=True)
    if not (dp / "project.json").exists():
        _write_json(dp / "project.json", {"name": name, "created_at": datetime.now().isoformat(timespec="seconds")})
    exclude_from_git(path)
    return dp


def write_space(pid):
    """Write the project's data into its folder (after every change)."""
    project = db.get_project(pid)
    if not project or not project.get("path") or not os.path.isdir(project["path"]):
        return False
    dp = space_dir(project["path"])
    (dp / "sessions").mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(project["path"])
    manifest.update({k: project.get(k) or "" for k in MANIFEST_KEYS if k != "created_at"})
    if project.get("created_at"):                 # the registration date is the reference
        manifest["created_at"] = project["created_at"].replace(" ", "T")
    _write_json(dp / "project.json", manifest)

    specs = db.get_project_specs(pid) or {}
    if specs.get("prompt"):
        (dp / "prompt.md").write_text(specs["prompt"], encoding="utf-8")
    checklist = specs.get("checklist") or []
    if checklist:
        _write_json(dp / "checklist.json", checklist)
        (dp / "checklist.md").write_text("# Checklist\n\n" + "\n".join(
            f"- [{'x' if t.get('done') else ' '}] {t.get('text', '')}" for t in checklist) + "\n", encoding="utf-8")
    if specs.get("wizard_data"):
        _write_json(dp / "wizard.json", specs["wizard_data"])
    exclude_from_git(project["path"])
    return True


def _restore_from_space(pid, path):
    """Registering a folder that already has a .devpilot/ space: take its data back."""
    m = read_manifest(path)
    meta = {k: m[k] for k in ("description", "color", "status", "profile") if m.get(k)}
    if (m.get("github") or {}).get("web"):          # multi-repo project linked to its organisation
        meta["git_remote"] = m["github"]["web"]
    if meta:
        db.update_project(pid, **meta)
    if db.get_project_specs(pid):
        return
    dp = space_dir(path)
    prompt = (dp / "prompt.md").read_text(encoding="utf-8") if (dp / "prompt.md").exists() else ""
    try:
        checklist = json.loads((dp / "checklist.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        checklist = []
    try:
        wizard = json.loads((dp / "wizard.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        wizard = {}
    if prompt or checklist or wizard:
        db.save_project_specs(pid, wizard, checklist, prompt)
    try:
        import connections
        connections.restore(pid, path)
    except Exception:
        pass


def create(name, path=None, custom_location=False, git_init=False, description=""):
    """New project = its folder (default ~/devpilot/projects/<name>) + its
    DevPilot space (.devpilot/) + its registration. All or nothing."""
    name, path = _prepare_target(name, path, custom_location)
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    try:
        if git_init:
            rc, _, err = git(["init", "-q"], cwd=path)
            if rc != 0:
                raise ProjectError(f"git init a echoue : {err}")
        init_space(path, name)
        return register(name, path, custom_location=True, description=description)
    except Exception:
        if created:
            shutil.rmtree(path, ignore_errors=True)
        else:                                    # was an empty folder: empty again
            for child in path.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
        raise


def clone(url, name=None, path=None, custom_location=False, branch=None, token=None, timeout=900,
          on_output=None):
    """git clone then register. A failed clone leaves nothing behind."""
    url = (url or "").strip()
    if not url or url.startswith("-"):
        raise ProjectError("URL du depot invalide")
    name, path = _prepare_target(name or name_from_url(url), path, custom_location)
    extra = []
    if token and url.startswith("https://"):
        # sent as a header for this command only: never written to .git/config
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        host = "/".join(url.split("/")[:3]) + "/"
        extra = ["-c", f"http.{host}.extraheader=AUTHORIZATION: basic {basic}"]
    args = ["clone", "--progress"]
    if branch:
        args += ["--branch", branch]
    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = git(args + ["--", url, str(path)], timeout=timeout, extra=extra, on_output=on_output)
    if rc != 0:
        if created:
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_dir():
            for child in path.iterdir():   # was empty before: back to empty
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
        raise ProjectError("Clone echoue : " + _explain_git_error(err))
    return register(name, path, custom_location=True, description=f"Clone de {url}")


def _explain_git_error(err):
    e = err or ""
    if "Permission denied (publickey)" in e or "Host key verification failed" in e:
        return "acces SSH refuse — ta cle SSH n'est pas sur GitHub (ou pas chargee)\n" + e[-300:]
    if "could not read Username" in e or "Authentication failed" in e or "terminal prompts disabled" in e:
        return "depot prive — configure un token GitHub dans Parametres\n" + e[-300:]
    if "not found" in e.lower() and "repository" in e.lower():
        return "depot introuvable (URL ? droits ?)\n" + e[-300:]
    if "Remote branch" in e and "not found" in e:
        return "branche introuvable\n" + e[-300:]
    return e[-500:] or "erreur inconnue"


def adopt(src, mode="move", name=None, dest=None, custom_location=False):
    """Existing folder → project. mode: move (into projects/) | copy | link (in place)."""
    src = resolve(src)
    if not src.is_dir():
        raise ProjectError(f"{src} n'existe pas")
    name = validate_name(name or src.name)
    if mode == "link" or (mode == "move" and is_managed(src) and not dest):
        # in place: outside projects/ only because the user chose "keep here"
        return register(name, src, custom_location=(mode == "link") or custom_location)
    if mode not in ("move", "copy"):
        raise ProjectError(f"Mode inconnu : {mode}")
    name, target = _prepare_target(name, dest, custom_location)
    if src.is_relative_to(target) or target.is_relative_to(src):
        raise ProjectError("La destination et la source se chevauchent")
    if mode == "move":
        report = inspect(src, with_size=False)
        if report["blockers"]:
            raise ProjectError("Deplacement impossible pour l'instant", details=report)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()               # empty (checked by _prepare_target)
        shutil.move(str(src), str(target))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()
        shutil.copytree(src, target, symlinks=True)
    return register(name, target, custom_location=True)


# ── Git link / DevPilot files in repos ─────────────────────────────────────

DEVPILOT_MARK = "<!-- devpilot:generated -->"
# Local files that may exist in a folder before its repo is cloned into it
_DEVPILOT_ITEMS = {".devpilot", "CLAUDE.md", ".claude"}


def exclude_from_git(project_path, entries=(".devpilot/",)):
    """Add entries to .git/info/exclude of the folder's repo (never to .gitignore)."""
    info = Path(project_path) / ".git" / "info"
    if not (Path(project_path) / ".git").is_dir():
        return False
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    current = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [e for e in entries if e not in current]
    if missing:
        with exclude.open("a") as f:
            if current and current[-1] != "":
                f.write("\n")
            f.write("\n".join(missing) + "\n")
    return True


def _exclude_devpilot_files(path):
    """Keep DevPilot's own files out of the repo: .devpilot/ always, the root
    CLAUDE.md only when DevPilot generated it (a repo's own CLAUDE.md stays tracked)."""
    entries = [".devpilot/"]
    if is_devpilot_file(Path(path) / "CLAUDE.md"):
        entries.append("/CLAUDE.md")
    return exclude_from_git(path, tuple(entries))


def is_devpilot_file(path):
    """A CLAUDE.md DevPilot wrote itself (safe to regenerate)."""
    try:
        return DEVPILOT_MARK in Path(path).read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return False


def _default_branch(repo):
    rc, out, _ = git(["ls-remote", "--symref", "origin", "HEAD"], cwd=repo, timeout=60)
    m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out or "")
    return m.group(1) if m else None


_GH_RE = re.compile(
    r"^(?:(?:https?://|ssh://git@|git@)?(?:www\.)?github\.com[/:])?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?(?:/(?:tree|blob|pull|issues|commits?)(?:/.*)?)?/?$")


def parse_github(url):
    """owner/repo from any GitHub form (https, ssh, "owner/repo", page links), or None."""
    m = _GH_RE.match((url or "").strip())
    if not m or m.group("repo") in (".", ".."):
        return None
    return m.group("owner"), m.group("repo")


def _probe(url, extra=None):
    """(ok, empty, error): can we read this remote, and does it have commits?"""
    rc, out, err = git(["ls-remote", "--", url], timeout=45, extra=extra)
    if rc != 0:
        return False, False, err
    return True, not out.strip(), ""


def resolve_remote(url, token=None):
    """The remote URL to store + how to reach it. GitHub: SSH first (push/pull
    then work from any terminal with your key), HTTPS as fallback."""
    url = (url or "").strip()
    if not url or url.startswith("-"):
        raise ProjectError("Lien du depot invalide")
    gh = parse_github(url)
    if not gh:
        ok, empty, err = _probe(url)
        if not ok:
            raise ProjectError("Depot inaccessible : " + _explain_git_error(err))
        return {"url": url, "empty": empty, "github": None, "via": "custom", "extra": []}
    owner, repo = gh
    ssh = f"git@github.com:{owner}/{repo}.git"
    https = f"https://github.com/{owner}/{repo}.git"
    ok, empty, err_ssh = _probe(ssh)
    if ok:
        return {"url": ssh, "empty": empty, "github": f"{owner}/{repo}", "via": "ssh", "extra": []}
    extra = []
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        extra = ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
    ok, empty, err_https = _probe(https, extra)
    if ok:
        return {"url": https, "empty": empty, "github": f"{owner}/{repo}", "via": "https", "extra": extra}
    if "not found" in (err_ssh + err_https).lower() or "does not exist" in (err_ssh + err_https).lower():
        raise ProjectError(f"Le depot github.com/{owner}/{repo} n'existe pas, ou ton compte n'y a pas acces.")
    raise ProjectError(f"Impossible de joindre github.com/{owner}/{repo} :\n" + _explain_git_error(err_ssh or err_https))


def _set_upstream(path, branch):
    git(["config", f"branch.{branch}.remote", "origin"], cwd=path)
    git(["config", f"branch.{branch}.merge", f"refs/heads/{branch}"], cwd=path)


def _current_branch(path):
    rc, out, _ = git(["symbolic-ref", "--short", "HEAD"], cwd=path)
    return out if rc == 0 else ""


def _record_link(pid, remote):
    db.update_project(pid, git_remote=remote["url"])
    cfg = {"url": remote["url"]}
    if remote.get("github"):
        cfg.update(repo=remote["github"], web=f"https://github.com/{remote['github']}")
    existing = db.get_project_component(pid, "git")
    if existing:
        merged = existing.get("config") or {}
        if isinstance(merged, str):
            try:
                merged = json.loads(merged)
            except ValueError:
                merged = {}
        merged.update(cfg)
        db.update_project_component(pid, "git", config=merged, enabled=True)
    else:
        db.add_project_component(pid, "git", enabled=True, config=cfg)
    write_space(pid)


def link_git(pid, url, token=None, replace=False):
    """Link the project's folder to a (GitHub) repo so pull/push just work.
    Never deletes, never merges, never overwrites a file."""
    project = get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        raise ProjectError("Le dossier du projet est introuvable")
    path = Path(project["path"])

    if not (path / ".git").exists():
        subs = [r["dir"] for r in find_repos(path) if r["dir"] != "."]
        if subs:
            raise ProjectError("Ce projet contient deja ses propres depots (" + ", ".join(subs) + ") : "
                               "chacun est lie a son GitHub. Utilise l'onglet Git pour choisir le depot.")

    remote = resolve_remote(url, token)
    extra = remote["extra"]
    web = f"github.com/{remote['github']}" if remote["github"] else remote["url"]

    # ── already a git repo ──
    if (path / ".git").exists():
        current = _remote(path)
        same = current and (current == remote["url"] or
                            (remote["github"] and parse_github(current) == tuple(remote["github"].split("/"))))
        if current and not same and not replace:
            raise ProjectError(f"Ce projet est deja lie a {current}. Confirme pour le remplacer par {web}.",
                               details={"needs_confirm": True, "current": current})
        rc, _, e = git(["remote", "set-url" if current else "add", "origin", remote["url"]], cwd=path)
        if rc != 0:
            raise ProjectError(f"git remote a echoue : {e}")
        if not remote["empty"]:
            git(["fetch", "origin"], cwd=path, timeout=600, extra=extra)
        branch = _current_branch(path) or "main"
        rc, _, _ = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=path)
        if rc == 0 or remote["empty"]:
            _set_upstream(path, branch)
        _exclude_devpilot_files(path)
        _record_link(pid, remote)
        return {"action": "remote_updated", "remote": remote["url"], "branch": branch,
                "message": f"Lie a {web} (branche {branch}). pull / push sont prets."}

    # ── folder not yet under git ──
    local = [x.name for x in path.iterdir() if x.name not in _DEVPILOT_ITEMS]

    if remote["empty"]:
        # new GitHub repo: start 'main' locally, tracking origin/main (created by the first push)
        for args in (["init", "-q", "-b", "main"], ["remote", "add", "origin", remote["url"]]):
            rc, _, e = git(args, cwd=path)
            if rc != 0:
                shutil.rmtree(path / ".git", ignore_errors=True)
                raise ProjectError(f"git {args[0]} a echoue : {e}")
        _set_upstream(path, "main")
        _exclude_devpilot_files(path)
        _record_link(pid, remote)
        return {"action": "linked_empty", "remote": remote["url"], "branch": "main",
                "message": f"Lie a {web} (depot vide). Premier envoi : commit puis push, la branche main sera creee sur GitHub."}

    for args in (["init", "-q"], ["remote", "add", "origin", remote["url"]]):
        rc, _, e = git(args, cwd=path)
        if rc != 0:
            shutil.rmtree(path / ".git", ignore_errors=True)
            raise ProjectError(f"git {args[0]} a echoue : {e}")
    rc, _, e = git(["fetch", "origin"], cwd=path, timeout=600, extra=extra)
    if rc != 0:
        shutil.rmtree(path / ".git", ignore_errors=True)       # back to how it was
        raise ProjectError("Recuperation du depot echouee : " + _explain_git_error(e))
    branch = _default_branch(path) or "main"

    if local:
        git(["checkout", "-q", "-b", branch], cwd=path)       # unborn branch: files untouched
        _set_upstream(path, branch)
        _exclude_devpilot_files(path)
        _record_link(pid, remote)
        import gitsync                                    # (imports projects: load lazily)
        return {"action": "needs_choice", "remote": remote["url"], "branch": branch,
                "compare": gitsync.compare(pid),
                "message": f"Lie a {web}. Ton dossier et GitHub sont differents : choisis comment les reunir. "
                           f"Rien n'a encore change dans ton dossier."}

    # only DevPilot files here: the repo's content is checked out
    aside = []
    claude = path / "CLAUDE.md"
    if claude.exists() and is_devpilot_file(claude):
        rc, _, _ = git(["cat-file", "-e", f"origin/{branch}:CLAUDE.md"], cwd=path)
        if rc == 0:
            (path / ".devpilot").mkdir(exist_ok=True)
            shutil.move(str(claude), str(path / ".devpilot" / "CLAUDE.devpilot.md"))
            aside.append("CLAUDE.md")
    rc, _, e = git(["checkout", "-q", "-b", branch, "--track", f"origin/{branch}"], cwd=path)
    if rc != 0:
        raise ProjectError(f"Checkout impossible (rien n'a ete ecrase) : {e[-300:]}")
    _exclude_devpilot_files(path)
    _record_link(pid, remote)
    msg = f"Lie a {web} : code recupere dans le projet (branche {branch}). pull / push sont prets."
    if aside:
        msg += " Le CLAUDE.md du depot est garde, celui de DevPilot est dans .devpilot/."
    return {"action": "cloned", "remote": remote["url"], "branch": branch, "message": msg}


# ── Link from the terminal: the user clones, DevPilot records ─────────────

_gh_user = None


def github_user():
    """Your GitHub login (for the hints), via gh; cached."""
    global _gh_user
    if _gh_user is None:
        try:
            r = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True, timeout=15)
            _gh_user = r.stdout.strip() if r.returncode == 0 else ""
        except (FileNotFoundError, subprocess.TimeoutExpired):
            _gh_user = ""
    return _gh_user


def detect_link(pid, record=False):
    """What was cloned in the project folder, and (record=True) link the project to it:
    one repo at the root -> that repo; several repos of one GitHub owner -> the organisation."""
    project = get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        raise ProjectError("Le dossier du projet est introuvable")
    path = Path(project["path"])
    repos = []
    for r in find_repos(path, depth=4):
        gh = parse_github(r["remote"]) if r["remote"] else None
        repos.append({"dir": r["dir"], "remote": r["remote"], "github": "/".join(gh) if gh else None})
    root = next((r for r in repos if r["dir"] == "."), None)
    owners = {r["github"].split("/")[0].lower() for r in repos if r["github"]}
    if root:
        kind = "single"
    elif repos and len(owners) == 1 and all(r["github"] for r in repos):
        kind = "org"
    elif repos:
        kind = "multi"
    else:
        kind = None
    res = {"kind": kind, "repos": repos, "org": next(iter(owners)) if kind == "org" else None}
    if not record:
        return res

    if kind is None:
        raise ProjectError("Aucun depot git dans le dossier du projet. Lance ton git clone dans le terminal "
                           "(ou ton script), puis clique Termine.")
    if kind == "single":
        if not root["remote"]:
            raise ProjectError("Le depot a la racine n'a pas de remote origin (git remote add origin <lien>).")
        _exclude_devpilot_files(path)
        _record_link(pid, {"url": root["remote"], "github": root["github"]})
        res["message"] = f"Projet lie a {root['github'] or root['remote']}."
        return res
    if kind == "org":
        import ghorg                                      # (imports projects: load lazily)
        try:
            r = ghorg.link_org(pid, res["org"])
            res["message"] = r["message"]
            return res
        except ProjectError:
            pass                                          # gh unavailable: record the repos as they are
    db.update_project(pid, git_remote=repos[0]["remote"] or "")
    write_space(pid)
    res["message"] = f"{len(repos)} depot(s) enregistre(s) : " + ", ".join(r["dir"] for r in repos)
    return res


# ── Change ──────────────────────────────────────────────────────────────────

def rename(pid, new_name):
    """Display name only: the folder is not touched."""
    project = get(pid)
    new_name = validate_name(new_name)
    if find_by_name(new_name, exclude_id=pid):
        raise ProjectError(f'Un projet "{new_name}" existe deja')
    db.update_project(pid, name=new_name)
    with db.get_db() as conn:
        conn.execute("UPDATE rules SET pattern = ? WHERE project_id = ? AND pattern = ?",
                     (re.escape(new_name.lower()), pid, re.escape(project["name"].lower())))
    write_space(pid)
    return db.get_project(pid)


def relocate(pid, new_path, custom_location=False):
    """The folder was moved outside DevPilot: point the project to it."""
    get(pid)
    new_path = resolve(new_path)
    if not new_path.is_dir():
        raise ProjectError(f"{new_path} n'existe pas")
    _check_location(new_path, custom_location)
    if find_by_path(new_path, exclude_id=pid):
        raise ProjectError(f"{new_path} est deja un autre projet")
    if overlapping(new_path, exclude_id=pid):
        raise ProjectError(f"{new_path} chevauche un autre projet")
    repos = find_repos(new_path)
    old = db.get_project(pid)
    db.update_project(pid, path=str(new_path), git_remote=repos[0]["remote"] if repos else "")
    write_space(pid)
    for cb in on_removed:          # terminals still sitting in the old (gone) path
        try:
            cb(old)
        except Exception:
            pass
    return db.get_project(pid)


def move(pid, dest, custom_location=False, leave_symlink=False):
    project = get(pid)
    src = resolve(project["path"]) if project.get("path") else None
    if not src or not src.is_dir():
        raise ProjectError("Le dossier du projet est introuvable — utilise « relocaliser »")
    dest = resolve(dest)
    _check_location(dest, custom_location)
    if dest == src:
        return project
    if dest.exists() and not _dir_is_empty(dest):
        raise ProjectError(f"{dest} existe deja et n'est pas vide")
    if dest.is_relative_to(src):
        raise ProjectError("Impossible de deplacer un dossier dans lui-meme")
    if overlapping(dest, exclude_id=pid):
        raise ProjectError(f"{dest} chevauche un autre projet")
    report = inspect(src, with_size=False)
    if report["blockers"]:
        raise ProjectError("Deplacement impossible pour l'instant", details=report)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.rmdir()
    shutil.move(str(src), str(dest))
    if leave_symlink:
        src.symlink_to(dest, target_is_directory=True)
    db.update_project(pid, path=str(dest))
    for cb in on_removed:          # terminals opened in the old folder
        try:
            cb(project)
        except Exception:
            pass
    return {**db.get_project(pid), "venvs_to_rebuild": [v.replace(str(src), str(dest), 1)
                                                        for v in report["venvs"]]}


def duplicate(pid, new_name, dest=None, custom_location=False):
    project = get(pid)
    src = resolve(project["path"])
    if not src.is_dir():
        raise ProjectError("Le dossier du projet est introuvable")
    return adopt(src, mode="copy", name=new_name, dest=dest, custom_location=custom_location)


# ── Remove ──────────────────────────────────────────────────────────────────

def removal_report(pid):
    project = get(pid)
    report = inspect(project["path"]) if project.get("path") else {"exists": False}
    report["project"] = {"id": project["id"], "name": project["name"], "path": project.get("path", "")}
    if report.get("exists"):
        path = resolve(project["path"])
        try:
            _check_location(path, custom_location=True)
        except ProjectError as e:
            report["blockers"].append(e.message)
        inside = [o for o in overlapping(path, exclude_id=pid) if o["relation"] == "inside"]
        if inside:
            report["blockers"].append("Contient d'autres projets : " + ", ".join(o["name"] for o in inside))
        if not report["managed"]:
            report["risks"].append("Dossier hors de ~/devpilot/projects (emplacement choisi a la main)")
    return report


def remove(pid, delete_files=False, confirm_name=None):
    """Unregister; with ``delete_files`` also send the folder to the trash."""
    project = get(pid)
    trashed = None
    if delete_files and project.get("path") and os.path.isdir(project["path"]):
        if (confirm_name or "").strip() != project["name"]:
            raise ProjectError("Tape exactement le nom du projet pour confirmer la suppression des fichiers")
        report = removal_report(pid)
        if report["blockers"]:
            raise ProjectError("Suppression des fichiers impossible", details=report)
        trashed = move_to_trash(project["path"])
    db.delete_project(pid)
    db.log_event("deleted", "project", project["name"], project.get("path", ""), 0)
    for cb in on_removed:
        try:
            cb(project)
        except Exception:
            pass
    return {"removed": project["name"], "trashed_to": trashed}
