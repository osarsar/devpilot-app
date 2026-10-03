"""DevPilot — « Supprimer tout » : a project and everything that depends on its folder.

plan()    lists what belongs to the project, each item with a default choice:
  - Docker: every compose project that has a container using the folder
    (compose dir or bind mount) — its containers, networks, volumes, and the
    images it BUILT (<project>-<service>); shared base images are never listed
  - processes running from the folder (and the ports they listen on)
  - outside the folder: symlinks to it, launchers / desktop entries / systemd
    units that reference it (checked), config files that mention it (not checked)
  - the folder itself (to the trash)
execute() does it in order, stops before the folder if something still uses it.
Secrets/keys that do not mention the folder are never found, hence never deleted.
"""

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import db
import projects as P
from projects import ProjectError

HOME = Path.home()
_SCAN_DIRS = [(HOME / ".local" / "bin", "launcher"), (HOME / ".local" / "share" / "applications", "desktop"),
              (HOME / ".config" / "autostart", "desktop"), (HOME / ".config" / "systemd" / "user", "systemd")]
_SCAN_CONFIG = [HOME / ".config", HOME]         # top-level files only (config / dotfiles)


def _docker(args, timeout=60):
    try:
        r = subprocess.run(["docker"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _inside(p, root):
    try:
        return bool(p) and Path(p).resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        return False


# ── Docker ──────────────────────────────────────────────────────────────────

def docker_plan(root):
    rc, out, _ = _docker(["ps", "-aq"])
    ids = out.split()
    if rc != 0 or not ids:
        return []
    rc, out, _ = _docker(["inspect"] + ids, timeout=60)
    containers = json.loads(out or "[]")

    def project_of(c):
        return ((c.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project", "")

    uses = set()
    for c in containers:
        labels = (c.get("Config") or {}).get("Labels") or {}
        wd = labels.get("com.docker.compose.project.working_dir", "")
        binds = [m.get("Source", "") for m in c.get("Mounts", []) if m.get("Type") == "bind"]
        if any(_inside(x, root) for x in [wd] + binds):
            uses.add(project_of(c) or "__loose__" + c["Id"])

    groups = []
    for proj in sorted(uses):
        if proj.startswith("__loose__"):             # a container outside compose that uses the folder
            cs = [c for c in containers if c["Id"] == proj[9:]]
            name = cs[0]["Name"].lstrip("/")
            groups.append({"compose": None, "name": name, "containers": [_c(c) for c in cs],
                           "networks": [], "volumes": [], "images": []})
            continue
        cs = [c for c in containers if project_of(c) == proj]
        groups.append({"compose": proj, "name": proj, "containers": [_c(c) for c in cs],
                       "networks": _named("network", proj), "volumes": _named("volume", proj),
                       "images": _built_images(proj, cs)})
    return groups


def _c(c):
    ports = sorted({f"{b['HostPort']}" for binds in ((c.get("NetworkSettings") or {}).get("Ports") or {}).values()
                    for b in (binds or []) if b.get("HostPort")}, key=int)
    return {"id": c["Id"][:12], "name": c["Name"].lstrip("/"), "running": (c.get("State") or {}).get("Running", False),
            "image": (c.get("Config") or {}).get("Image", ""), "ports": ports}


def _named(kind, proj):
    """Networks / volumes of a compose project: by label, or by the <project>_ name prefix (older ones)."""
    rc, out, _ = _docker([kind, "ls", "--format", "{{.Name}}\t{{.Label \"com.docker.compose.project\"}}"])
    names = []
    for line in out.splitlines():
        name, _, label = line.partition("\t")
        if label == proj or name.startswith(proj + "_"):
            names.append(name)
    return sorted(set(names))


def _built_images(proj, containers):
    """Images the compose project built (<project>-<service>), never pulled base images."""
    rc, out, _ = _docker(["images", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}"])
    imgs = {}
    for line in out.splitlines():
        ref, iid, size = (line.split("\t") + ["", ""])[:3]
        if ref.startswith(proj + "-"):
            imgs[ref] = {"ref": ref, "id": iid, "size": size}
    return sorted(imgs.values(), key=lambda x: x["ref"])


# ── Processes / outside references ──────────────────────────────────────────

def _processes(root, ignore=()):
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "name", "cwd"]):
        try:
            if p.info["pid"] in ignore or p.info["pid"] == os.getpid() or not _inside(p.info.get("cwd"), root):
                continue
            ports = sorted({c.laddr.port for c in p.net_connections(kind="inet") if c.status == "LISTEN"})
            out.append({"pid": p.info["pid"], "name": p.info["name"], "cwd": p.info["cwd"], "ports": ports})
        except Exception:
            continue
    return out


_SKIP_NAMES = ("history", ".viminfo", ".lesshst", ".wget-hsts", ".xsession-errors", ".recently-used", ".sudo_as_admin")


def _mentions(path, root_strs):
    if any(k in path.name for k in _SKIP_NAMES):
        return False
    try:
        if path.stat().st_size > 512 * 1024:
            return False
        text = path.read_text(errors="ignore")
    except OSError:
        return False
    # the path itself, not a longer one (…/projects/shop must not match …/projects/shop2)
    return any(re.search(re.escape(r) + r"(?![A-Za-z0-9._-])", text) for r in root_strs)


def outside_plan(root):
    """Things outside the folder that point to it."""
    root_strs = {str(root)}
    items, seen = [], set()
    # symlinks in ~ pointing into the folder (e.g. ~/marocdefender -> ~/devpilot/projects/marocdefender)
    for d in [HOME, HOME / "Desktop", HOME / ".local" / "bin"]:
        if not d.is_dir():
            continue
        for e in d.iterdir():
            if e.is_symlink() and _inside(os.path.realpath(e), root):
                items.append({"path": str(e), "kind": "symlink", "default": True, "why": f"lien vers {os.readlink(e)}"})
                seen.add(str(e))
                root_strs.add(str(e))          # files may mention the link instead of the real path
    for d, kind in _SCAN_DIRS:
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.is_file() and str(f) not in seen and _mentions(f, root_strs):
                    items.append({"path": str(f), "kind": kind, "default": True,
                                  "why": {"launcher": "lanceur", "desktop": "raccourci", "systemd": "service"}[kind]})
                    seen.add(str(f))
    for d in _SCAN_CONFIG:
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.is_file() and not f.is_symlink() and str(f) not in seen and _mentions(f, root_strs):
                    items.append({"path": str(f), "kind": "config", "default": False,
                                  "why": "fichier de config qui mentionne le projet (peut contenir des secrets)"})
                    seen.add(str(f))
    return items


# ── Plan / execute ──────────────────────────────────────────────────────────

def plan(pid):
    project = P.get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        return {"project": project["name"], "exists": False, "docker": [], "processes": [], "outside": [],
                "folder": None}
    root = Path(project["path"]).resolve()
    ignore = set()
    for fn in P.ignored_pids:
        try:
            ignore |= set(fn())
        except Exception:
            pass
    report = P.removal_report(pid)
    # blockers about docker/processes are handled here: they are part of what gets removed
    blockers = [b for b in report.get("blockers", []) if not b.startswith(("Conteneurs Docker", "Process lances"))]
    return {"project": project["name"], "exists": True, "docker": docker_plan(root),
            "processes": _processes(root, ignore), "outside": outside_plan(root),
            "folder": {"path": str(root), "size": report.get("size"), "repos": report.get("repos", []),
                       "risks": report.get("risks", []), "blockers": blockers}}


def execute(pid, confirm_name, choices=None):
    """choices: {containers, networks, volumes, images, processes: bool, outside: [paths]} (default = plan defaults)."""
    project = P.get(pid)
    if (confirm_name or "").strip() != project["name"]:
        raise ProjectError("Tape exactement le nom du projet pour confirmer")
    pl = plan(pid)
    if pl["exists"] and pl["folder"]["blockers"]:
        raise ProjectError("Suppression impossible : " + " ; ".join(pl["folder"]["blockers"]), details=pl)
    ch = {"containers": True, "networks": True, "volumes": True, "images": True, "processes": True,
          "outside": [o["path"] for o in pl["outside"] if o["default"]]}
    ch.update({k: v for k, v in (choices or {}).items() if k in ch})
    log = []

    for cb in P.on_removed:                   # DevPilot's own terminals in this project
        try:
            cb(project)
        except Exception:
            pass

    if ch["processes"]:
        for pr in pl["processes"]:
            try:
                os.kill(pr["pid"], signal.SIGTERM)
            except ProcessLookupError:
                continue
        deadline = time.time() + 6
        for pr in pl["processes"]:
            while time.time() < deadline and _alive(pr["pid"]):
                time.sleep(0.2)
            if _alive(pr["pid"]):
                try:
                    os.kill(pr["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            log.append(f"processus arrete : {pr['name']} ({pr['pid']})" + (f", port {', '.join(map(str, pr['ports']))} libere" if pr["ports"] else ""))

    for g in pl["docker"]:
        if ch["containers"] and g["containers"]:
            rc, _, err = _docker(["rm", "-f", "-v"] + [c["id"] for c in g["containers"]], timeout=180)
            ports = sorted({p for c in g["containers"] for p in c["ports"]}, key=int)
            log.append(f"{g['name']} : {len(g['containers'])} conteneur(s) supprime(s)" + (f", ports {', '.join(ports)} liberes" if ports else "")
                       if rc == 0 else f"{g['name']} : conteneurs — {err.strip()[-200:]}")
        if ch["networks"]:
            for n in g["networks"]:
                rc, _, err = _docker(["network", "rm", n])
                log.append(f"reseau {n} supprime" if rc == 0 else f"reseau {n} : {err.strip()[-150:]}")
        if ch["volumes"]:
            for v in g["volumes"]:
                rc, _, err = _docker(["volume", "rm", v])
                log.append(f"volume {v} supprime" if rc == 0 else f"volume {v} : {err.strip()[-150:]}")
        if ch["images"]:
            for im in g["images"]:
                rc, _, err = _docker(["rmi", im["ref"]], timeout=180)
                log.append(f"image {im['ref']} supprimee ({im['size']})" if rc == 0 else f"image {im['ref']} : {err.strip()[-150:]}")

    wanted = set(ch["outside"] or [])
    for o in pl["outside"]:
        if o["path"] not in wanted:
            continue
        p = Path(o["path"])
        try:
            if o["kind"] == "symlink":
                p.unlink()
                log.append(f"lien supprime : {p}")
            else:
                if o["kind"] == "systemd":
                    subprocess.run(["systemctl", "--user", "disable", "--now", p.name], capture_output=True, timeout=30)
                P.move_to_trash(p)
                log.append(f"{o['why']} a la corbeille : {p}")
        except OSError as e:
            log.append(f"{p} : {e}")

    trashed = None
    if pl["exists"]:
        root = Path(pl["folder"]["path"])
        still = [c["name"] for g in docker_plan(root) for c in g["containers"] if c["running"]]
        procs = _processes(root)
        if still or procs:
            raise ProjectError("Le dossier est encore utilise, il n'a pas ete supprime : "
                               + ", ".join(still + [f"{p['name']} ({p['pid']})" for p in procs]),
                               details={"log": log})
        trashed = P.move_to_trash(root)
        log.append(f"dossier a la corbeille : {root}")
    P.remove(pid)
    log.append("projet retire de DevPilot")
    return {"log": log, "trashed_to": trashed}


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
