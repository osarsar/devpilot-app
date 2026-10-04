#!/usr/bin/env python3
"""DevPilot — Developer Project Manager."""

import os
import subprocess
import shutil
import json
import re
import time
import psutil
from pathlib import Path
from datetime import datetime, timedelta
import pty
import select
import struct
import fcntl
import termios
import threading
import signal
from flask import Flask, render_template, jsonify, request
from flask_sock import Sock

import db
import components as comp
from cloud_routes import cloud_bp
import projects as P
import servers
import dev as dev_mod
import persist
import ports as ports_mod
import sizes
import model as M
import phases as PH
from project_routes import projects_bp
from docgen_routes import docgen_bp

app = Flask(__name__)
sock = Sock(app)
app.register_blueprint(cloud_bp)
app.register_blueprint(docgen_bp)
app.register_blueprint(projects_bp)

# Only this dashboard may call the API. Without this, any website open in the
# browser could POST to localhost:5555 or open the terminal WebSocket
# (cross-site requests, DNS rebinding): the API runs shell commands.
ALLOWED_HOSTS = {"127.0.0.1:5555", "localhost:5555"}


@app.after_request
def _refresh_sizes_after_changes(resp):
    p = request.path
    if request.method != "GET" and resp.status_code < 400 and (
            "clean" in p or "purge" in p or "/move" in p or "/duplicate" in p or "/adopt" in p
            or "/clone" in p or (request.method == "DELETE" and p.startswith("/api/projects/"))):
        sizes.invalidate_all()
    return resp


@app.before_request
def _only_from_dashboard():
    if request.host not in ALLOWED_HOSTS:
        return jsonify({"error": "host refuse"}), 403
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] not in ALLOWED_HOSTS:
        return jsonify({"error": "origine refusee"}), 403
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify({"error": "requete cross-site refusee"}), 403
HOME = Path.home()
DEVPILOT_ROOT = HOME / "devpilot"
DEVPILOT_PROJECTS = DEVPILOT_ROOT / "projects"
DEVPILOT_PROJECTS.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def fast_size(path):
    """Display only: cached size (background du), 0 until the first computation ends."""
    v = sizes.size(path)
    return v if v is not None else 0


def dir_size(path):
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += dir_size(entry.path)
            except (PermissionError, OSError):
                pass
    except (PermissionError, OSError):
        pass
    return total


def fmt(b):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


# ═══════════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/terminal-test")
def terminal_test():
    """Minimal terminal test page — isolates xterm.js + WebSocket."""
    return """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>DevPilot Terminal Test</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a0f; display:flex; flex-direction:column; height:100vh; font-family:sans-serif; }
  #status { padding:8px 16px; font-size:13px; color:#888; background:#111; }
  #status.ok { color:#22d3a7; }
  #status.err { color:#f43f5e; }
  #term { flex:1; }
</style>
</head><body>
<div id="status">Connexion...</div>
<div id="term"></div>
<script>
const status = document.getElementById('status');
try {
    const term = new Terminal({
        cursorBlink: true, fontSize: 14,
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        theme: { background:'#0a0a0f', foreground:'#c8c8d4', cursor:'#6d5cff' }
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById('term'));
    setTimeout(() => fit.fit(), 50);
    window.addEventListener('resize', () => fit.fit());

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(proto + '//' + location.host + '/api/terminal/ws');

    ws.onopen = () => {
        status.textContent = 'Connecte — clique ici et tape';
        status.className = 'ok';
        const d = fit.proposeDimensions();
        if (d) ws.send('\\x1b[RESIZE:' + d.rows + ',' + d.cols + ']');
        term.focus();
    };
    ws.onmessage = (e) => term.write(e.data);
    ws.onerror = () => { status.textContent = 'Erreur WebSocket'; status.className = 'err'; };
    ws.onclose = () => { status.textContent = 'Deconnecte'; status.className = 'err'; };
    term.onData((data) => { if (ws.readyState === 1) ws.send(data); });

    document.getElementById('term').addEventListener('click', () => term.focus());
} catch(e) {
    status.textContent = 'Erreur: ' + e.message;
    status.className = 'err';
}
</script>
</body></html>"""



# ═══════════════════════════════════════════════════════════════════════════
# SYSTEM APIs
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/system")
def api_system():
    """Real-time system stats for topbar + overview."""
    disks = []
    for part in psutil.disk_partitions():
        if part.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mount": part.mountpoint, "device": part.device, "fstype": part.fstype,
                "total": u.total, "used": u.used, "free": u.free, "percent": u.percent,
                "total_h": fmt(u.total), "used_h": fmt(u.used), "free_h": fmt(u.free),
            })
        except (PermissionError, OSError):
            pass

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu = psutil.cpu_percent(interval=0.3)
    trash_path = HOME / ".local" / "share" / "Trash"
    trash_size = fast_size(str(trash_path)) if trash_path.exists() else 0
    stats = db.get_stats()

    return jsonify({
        "disks": disks,
        "ram": {"total": mem.total, "used": mem.used, "available": mem.available,
                "percent": mem.percent, "total_h": fmt(mem.total), "used_h": fmt(mem.used)},
        "swap": {"total": swap.total, "used": swap.used, "percent": swap.percent,
                 "total_h": fmt(swap.total), "used_h": fmt(swap.used)},
        "cpu": {"percent": cpu, "count": psutil.cpu_count()},
        "trash": {"size": trash_size, "size_h": fmt(trash_size)},
        "stats": stats,
    })


@app.route("/api/home")
def api_home():
    """Home directory breakdown by folder."""
    entries = []
    categories = {
        ".local": ("system", "Cache/Corbeille"), "Downloads": ("downloads", "Telechargements"),
        ".cache": ("cache", "Cache systeme"), ".config": ("system", "Configuration"),
        "Desktop": ("projects", "Bureau/Projets"), "snap": ("system", "Snap"),
        "flutter": ("sdk", "SDK Flutter"), ".gradle": ("cache", "Cache Gradle"),
        "Android": ("sdk", "SDK Android"), ".npm": ("cache", "Cache NPM"),
        ".nvm": ("sdk", "Node versions"), ".vscode": ("system", "VS Code"),
        ".pyenv": ("sdk", "Python versions"), "Pictures": ("personal", "Images"),
        ".docker": ("system", "Docker config"), ".pub-cache": ("cache", "Cache Dart/Flutter"),
        "Documents": ("personal", "Documents"), "Videos": ("personal", "Videos"),
        "Music": ("personal", "Music"),
    }

    for item in HOME.iterdir():
        try:
            if item.is_symlink():
                continue
            size = item.stat().st_size if item.is_file() else fast_size(str(item))
            if size < 5 * 1024 * 1024:
                continue
            cat, label = categories.get(item.name, ("project" if not item.name.startswith(".") else "system", item.name))
            entries.append({"name": item.name, "size": size, "size_h": fmt(size), "category": cat, "label": label})
        except (PermissionError, OSError):
            pass

    entries.sort(key=lambda x: x["size"], reverse=True)
    return jsonify(entries[:30])


# ═══════════════════════════════════════════════════════════════════════════
# PROCESSES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/processes")
def api_processes():
    """Top processes by memory, with dev server detection."""
    dev_keywords = {"node", "python", "python3", "flask", "gunicorn", "uvicorn", "next",
                    "vite", "webpack", "docker", "java", "gradle", "cargo", "go",
                    "ruby", "php", "nginx", "postgres", "mysql", "redis", "mongo"}
    procs = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_percent", "create_time", "username"]):
        try:
            info = p.info
            mem = info.get("memory_info")
            if not mem or mem.rss < 20 * 1024 * 1024:
                continue
            name = info.get("name", "")
            cmd_parts = info.get("cmdline") or [name]
            cmd = " ".join(cmd_parts[:4])[:120]
            is_dev = any(k in name.lower() or k in cmd.lower() for k in dev_keywords)

            # Try to detect project from cmdline
            project_hint = ""
            for part in cmd_parts:
                if HOME.name in str(part) and "/" in part:
                    rel = part.replace(str(HOME) + "/", "").split("/")
                    if len(rel) >= 2 and rel[0] in ("Desktop", "projects", "dev"):
                        project_hint = rel[1]
                    elif len(rel) >= 1 and not rel[0].startswith("."):
                        project_hint = rel[0]
                    break

            procs.append({
                "pid": info["pid"], "name": name, "cmd": cmd,
                "memory": mem.rss, "memory_h": fmt(mem.rss),
                "cpu": info.get("cpu_percent", 0),
                "is_dev": is_dev, "project_hint": project_hint,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    procs.sort(key=lambda x: x["memory"], reverse=True)
    return jsonify(procs[:40])


# ═══════════════════════════════════════════════════════════════════════════
# DOCKER
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/docker")
def api_docker():
    if not run("docker info 2>/dev/null"):
        return jsonify({"available": False})

    system_df = []
    for line in run("docker system df --format json").splitlines():
        try:
            system_df.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    images = []
    for line in run("docker images --format json").splitlines():
        try:
            img = json.loads(line)
            name = f"{img.get('Repository', '')}:{img.get('Tag', '')}"
            res = db.get_resource_by_path(f"docker:image:{name}")
            images.append({
                "repo": img.get("Repository", ""), "tag": img.get("Tag", ""),
                "size": img.get("Size", ""), "created": img.get("CreatedSince", ""),
                "id": img.get("ID", ""),
                "project_id": res["project_id"] if res else None,
                "project_name": res.get("project_name", "") if res else "",
                "resource_id": res["id"] if res else None,
            })
        except (json.JSONDecodeError, Exception):
            pass

    containers = []
    for line in run("docker ps -a --format json").splitlines():
        try:
            c = json.loads(line)
            name = c.get("Names", "")
            res = db.get_resource_by_path(f"docker:container:{name}")
            containers.append({
                "name": name, "status": c.get("Status", ""), "state": c.get("State", ""),
                "size": c.get("Size", ""), "ports": c.get("Ports", ""),
                "image": c.get("Image", ""), "id": c.get("ID", ""),
                "project_id": res["project_id"] if res else None,
                "project_name": res.get("project_name", "") if res else "",
                "resource_id": res["id"] if res else None,
            })
        except (json.JSONDecodeError, Exception):
            pass

    volumes = []
    for line in run("docker volume ls --format json").splitlines():
        try:
            v = json.loads(line)
            name = v.get("Name", "")
            res = db.get_resource_by_path(f"docker:volume:{name}")
            volumes.append({
                "name": name, "driver": v.get("Driver", ""),
                "project_id": res["project_id"] if res else None,
                "project_name": res.get("project_name", "") if res else "",
                "resource_id": res["id"] if res else None,
            })
        except (json.JSONDecodeError, Exception):
            pass

    return jsonify({
        "available": True, "system_df": system_df,
        "images": images, "containers": containers, "volumes": volumes,
    })


# ═══════════════════════════════════════════════════════════════════════════
# PORTS
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/ports")
def api_ports():
    ports = []
    seen = set()
    output = run("ss -tlnp 2>/dev/null")
    system_ports = {22: "SSH", 53: "DNS", 631: "CUPS", 5900: "VNC", 5432: "PostgreSQL",
                    3306: "MySQL", 6379: "Redis", 27017: "MongoDB", 9200: "Elasticsearch"}

    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3]
        if ":" not in addr:
            continue
        port_str = addr.rsplit(":", 1)[-1]
        try:
            port = int(port_str)
        except ValueError:
            continue
        if port in seen:
            continue
        seen.add(port)

        bind = addr.rsplit(":", 1)[0]
        process = ""
        if "users:" in line:
            try:
                process = line.split("users:")[1].split('"')[1]
            except (IndexError, ValueError):
                pass
        if not process:
            dp = run(f"docker ps --filter 'publish={port}' --format '{{{{.Names}}}}' 2>/dev/null")
            if dp:
                process = f"docker:{dp}"

        ptype = "system" if port in system_ports else ("docker" if "docker" in process.lower() else "dev")
        label = system_ports.get(port, "")

        # Try to link port to project via tracked ports
        tracked = db.get_all_active_ports()
        project_name = ""
        project_id = None
        for tp in tracked:
            if tp["port"] == port and tp.get("project_id"):
                project_name = tp.get("project_name", "")
                project_id = tp["project_id"]
                break

        ports.append({"port": port, "bind": bind, "process": process or "—", "type": ptype,
                      "label": label, "project_name": project_name, "project_id": project_id})

    ports.sort(key=lambda x: x["port"])
    return jsonify(ports)


# ═══════════════════════════════════════════════════════════════════════════
# DOWNLOADS & STORAGE
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/downloads")
def api_downloads():
    dl = HOME / "Downloads"
    if not dl.exists():
        return jsonify({"files": [], "duplicates": []})

    files = []
    for item in dl.iterdir():
        try:
            if item.is_symlink():
                continue
            size = item.stat().st_size if item.is_file() else fast_size(str(item))
            mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d")
            res = db.get_resource_by_path(str(item))
            files.append({
                "name": item.name, "size": size, "size_h": fmt(size),
                "is_dir": item.is_dir(), "ext": item.suffix or ("dir" if item.is_dir() else "—"),
                "mtime": mtime, "path": str(item),
                "is_temporary": res["is_temporary"] if res else False,
                "expires_at": res["expires_at"] if res else None,
                "project_name": res.get("project_name", "") if res else "",
                "resource_id": res["id"] if res else None,
            })
        except (PermissionError, OSError):
            pass

    files.sort(key=lambda x: x["size"], reverse=True)
    names = {f["name"] for f in files}
    name_to_info = {f["name"]: f for f in files}

    duplicates = []
    seen = set()
    for f in files:
        if f["name"] in seen:
            continue
        p = Path(f["path"])
        if p.is_file() and p.suffix == ".zip":
            folder = p.stem
            if folder in names and name_to_info[folder]["is_dir"]:
                duplicates.append({**f, "reason": f"Archive de '{folder}/'"})
                seen.add(f["name"])
        elif p.is_file() and f["name"].endswith(".tar.gz"):
            folder = f["name"].replace(".tar.gz", "")
            if folder in names and name_to_info[folder]["is_dir"]:
                duplicates.append({**f, "reason": f"Archive de '{folder}/'"})
                seen.add(f["name"])
        elif f["name"].endswith(")") and " (" in f["name"]:
            if p.is_file() and p.suffix:
                base = p.stem.rsplit(" (", 1)[0] + p.suffix
            else:
                base = f["name"].rsplit(" (", 1)[0]
            if base in names:
                duplicates.append({**f, "reason": f"Copie de '{base}'"})
                seen.add(f["name"])

    return jsonify({"files": files[:40], "duplicates": duplicates})


# ═══════════════════════════════════════════════════════════════════════════
# CACHES — Browser / Project / Global / System
# ═══════════════════════════════════════════════════════════════════════════

BUILD_CACHES = {
    "node_modules":       {"label": "Node modules",     "tech": "node",    "safe": True,  "heavy": True},
    ".next":              {"label": "Next.js build",     "tech": "nextjs",  "safe": True,  "heavy": False},
    ".nuxt":              {"label": "Nuxt build",        "tech": "nuxt",    "safe": True,  "heavy": False},
    ".angular":           {"label": "Angular cache",     "tech": "angular", "safe": True,  "heavy": False},
    ".vite":              {"label": "Vite cache",        "tech": "vite",    "safe": True,  "heavy": False},
    ".turbo":             {"label": "Turbo cache",       "tech": "turbo",   "safe": True,  "heavy": False},
    ".parcel-cache":      {"label": "Parcel cache",      "tech": "parcel",  "safe": True,  "heavy": False},
    ".svelte-kit":        {"label": "SvelteKit",         "tech": "svelte",  "safe": True,  "heavy": False},
    ".dart_tool":         {"label": "Dart tooling",      "tech": "dart",    "safe": True,  "heavy": False},
    "__pycache__":        {"label": "Python cache",      "tech": "python",  "safe": True,  "heavy": False},
    ".pytest_cache":      {"label": "Pytest cache",      "tech": "python",  "safe": True,  "heavy": False},
    ".mypy_cache":        {"label": "Mypy cache",        "tech": "python",  "safe": True,  "heavy": False},
    ".tox":               {"label": "Tox env",           "tech": "python",  "safe": True,  "heavy": True},
    ".gradle":            {"label": "Gradle cache",      "tech": "gradle",  "safe": True,  "heavy": True},
    ".expo":              {"label": "Expo cache",        "tech": "expo",    "safe": True,  "heavy": False},
    ".webpack":           {"label": "Webpack cache",     "tech": "webpack", "safe": True,  "heavy": False},
    "build":              {"label": "Build output",      "tech": "general", "safe": False, "heavy": False},
    "dist":               {"label": "Dist output",       "tech": "general", "safe": False, "heavy": False},
    "target":             {"label": "Rust/Maven target", "tech": "rust",    "safe": False, "heavy": True},
}

BROWSER_CACHES = {
    "google-chrome": {
        "label": "Google Chrome",
        "paths": [
            ("Cache", ".cache/google-chrome/Default/Cache"),
            ("Code Cache", ".cache/google-chrome/Default/Code Cache"),
            ("Service Worker", ".cache/google-chrome/Default/Service Worker"),
            ("GPU Cache", ".cache/google-chrome/ShaderCache"),
        ],
        "full": ".cache/google-chrome",
    },
    "chromium": {
        "label": "Chromium",
        "paths": [("Cache", ".cache/chromium/Default/Cache"), ("Code Cache", ".cache/chromium/Default/Code Cache")],
        "full": ".cache/chromium",
    },
    "firefox": {
        "label": "Firefox",
        "paths": [],
        "full": ".cache/mozilla/firefox",
    },
    "brave": {
        "label": "Brave",
        "paths": [("Cache", ".cache/BraveSoftware/Brave-Browser/Default/Cache")],
        "full": ".cache/BraveSoftware",
    },
}

GLOBAL_CACHES = [
    {"key": "npm",      "label": "npm",              "path": ".npm",             "cmd": "npm cache clean --force"},
    {"key": "pip",      "label": "pip",              "path": ".cache/pip",        "cmd": "pip cache purge"},
    {"key": "yarn",     "label": "Yarn",             "path": ".cache/yarn",       "cmd": "yarn cache clean"},
    {"key": "pnpm",     "label": "pnpm",             "path": ".cache/pnpm",       "cmd": "pnpm store prune"},
    {"key": "gradle",   "label": "Gradle",           "path": ".gradle/caches",    "cmd": None},
    {"key": "pub",      "label": "Pub/Flutter",      "path": ".pub-cache",        "cmd": None},
    {"key": "go",       "label": "Go build",         "path": ".cache/go-build",   "cmd": "go clean -cache"},
    {"key": "maven",    "label": "Maven",            "path": ".m2/repository",    "cmd": None},
    {"key": "cargo",    "label": "Cargo/Rust",       "path": ".cargo/registry",   "cmd": None},
    {"key": "composer", "label": "Composer",         "path": ".cache/composer",   "cmd": "composer clear-cache"},
]


@app.route("/api/caches/browser")
def api_caches_browser():
    browsers = []
    for key, info in BROWSER_CACHES.items():
        full_path = HOME / info["full"]
        if not full_path.exists():
            continue
        total = fast_size(str(full_path))
        if total < 1024 * 1024:
            continue
        subs = []
        for label, rp in info["paths"]:
            p = HOME / rp
            if p.exists():
                s = fast_size(str(p))
                if s > 0:
                    subs.append({"label": label, "path": str(p), "size": s, "size_h": fmt(s)})
        browsers.append({
            "key": key, "label": info["label"],
            "total": total, "total_h": fmt(total),
            "full_path": str(full_path), "subs": sorted(subs, key=lambda x: x["size"], reverse=True),
        })
    browsers.sort(key=lambda x: x["total"], reverse=True)
    return jsonify(browsers)


@app.route("/api/caches/projects")
def api_caches_projects():
    """Walks the scan dirs (e.g. ~/Desktop, 12 GB): last result now, refreshed in the background."""
    value, _ = sizes.cached_call("caches/projects", _scan_project_caches)
    return jsonify(value or [])


def _scan_project_caches():
    scan_dirs_setting = db.get_setting("scan_dirs", "~/Desktop")
    base_dirs = []
    for d in scan_dirs_setting.split(","):
        p = Path(os.path.expanduser(d.strip()))
        if p.is_dir():
            base_dirs.append(p)

    manifests = ["package.json", "pubspec.yaml", "requirements.txt", "setup.py",
                 "pyproject.toml", "build.gradle", "Cargo.toml", "go.mod", "composer.json", "pom.xml"]
    results = []
    seen = set()
    projects_db = db.get_projects()

    for base in base_dirs:
        for mf_name in manifests:
            try:
                for mf in base.rglob(mf_name):
                    if "node_modules" in str(mf) or ".git" in str(mf) or "vendor" in str(mf):
                        continue
                    pdir = mf.parent
                    pdir_str = str(pdir)
                    if pdir_str in seen:
                        continue
                    seen.add(pdir_str)

                    proj_name = pdir.name
                    db_proj = None
                    for p in projects_db:
                        if p["name"].lower() in proj_name.lower() or proj_name.lower() in p["name"].lower():
                            db_proj = p
                            break

                    caches = []
                    total = 0
                    for cn, ci in BUILD_CACHES.items():
                        cp = pdir / cn
                        if cp.exists() and cp.is_dir() and not cp.is_symlink():
                            try:
                                s = fast_size(str(cp))
                                if s < 1024:
                                    continue
                                total += s
                                caches.append({"name": cn, "path": str(cp), "size": s, "size_h": fmt(s), **ci})
                            except (PermissionError, OSError):
                                pass
                    # node_modules/.cache
                    nmc = pdir / "node_modules" / ".cache"
                    if nmc.exists():
                        try:
                            s = fast_size(str(nmc))
                            if s > 1024:
                                caches.append({"name": "node_modules/.cache", "path": str(nmc), "size": s,
                                               "size_h": fmt(s), "label": "Build cache", "tech": "node",
                                               "safe": True, "heavy": False})
                        except (PermissionError, OSError):
                            pass

                    if caches:
                        results.append({
                            "dir": pdir_str, "name": proj_name,
                            "project_id": db_proj["id"] if db_proj else None,
                            "project_color": db_proj["color"] if db_proj else None,
                            "manifest": mf_name,
                            "caches": sorted(caches, key=lambda x: x["size"], reverse=True),
                            "total": total, "total_h": fmt(total),
                        })
            except (PermissionError, OSError):
                pass

    results.sort(key=lambda x: x["total"], reverse=True)
    return results


@app.route("/api/caches/global")
def api_caches_global():
    results = []
    for g in GLOBAL_CACHES:
        p = HOME / g["path"]
        if not p.exists():
            continue
        s = fast_size(str(p))
        if s < 1024 * 1024:
            continue
        results.append({
            "key": g["key"], "label": g["label"],
            "size": s, "size_h": fmt(s), "path": str(p), "cmd": g["cmd"],
        })
    # Docker build cache
    if run("docker info 2>/dev/null"):
        for line in run("docker system df --format json").splitlines():
            try:
                d = json.loads(line)
                if d.get("Type") == "Build Cache":
                    results.append({
                        "key": "docker_build", "label": "Docker build cache",
                        "size": 0, "size_h": d.get("Size", "0B"), "path": None,
                        "cmd": "docker builder prune -a -f",
                        "reclaimable": d.get("Reclaimable", ""),
                    })
            except json.JSONDecodeError:
                pass
    results.sort(key=lambda x: x.get("size", 0), reverse=True)
    return jsonify(results)


@app.route("/api/caches/system")
def api_caches_system():
    cache_dir = HOME / ".cache"
    if not cache_dir.exists():
        return jsonify([])
    safe = {"pip", "ms-playwright", "electron", "thumbnails", "yarn", "npm", "pnpm"}
    partial = {"google-chrome", "mozilla", "chromium"}
    caches = []
    for item in cache_dir.iterdir():
        if item.is_dir() and not item.is_symlink():
            try:
                s = fast_size(str(item))
                if s < 10 * 1024 * 1024:
                    continue
                cl = "safe" if item.name in safe else ("partial" if item.name in partial else "unknown")
                caches.append({"name": item.name, "size": s, "size_h": fmt(s), "cleanable": cl})
            except (PermissionError, OSError):
                pass
    caches.sort(key=lambda x: x["size"], reverse=True)
    return jsonify(caches)


# ═══════════════════════════════════════════════════════════════════════════
# SUGGESTIONS
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/suggestions")
def api_suggestions():
    sug = []

    # Trash
    trash = HOME / ".local" / "share" / "Trash"
    if trash.exists():
        s = fast_size(str(trash))
        if s > 100 * 1024 * 1024:
            sug.append({"priority": "critical", "msg": f"Corbeille = {fmt(s)}", "action": "trash", "size": s})

    # Expired
    expired = db.get_expired_resources()
    if expired:
        total = sum(r.get("size", 0) for r in expired)
        sug.append({"priority": "critical", "msg": f"{len(expired)} fichiers expires ({fmt(total)})",
                     "action": "clean_expired", "size": total})

    # Docker
    if run("docker info 2>/dev/null"):
        stopped = run("docker ps -a --filter 'status=exited' -q")
        if stopped:
            n = len(stopped.splitlines())
            sug.append({"priority": "medium", "msg": f"{n} containers arretes", "action": "docker_containers", "size": 0})
        dangling = run("docker images -f 'dangling=true' -q")
        if dangling:
            n = len(dangling.splitlines())
            sug.append({"priority": "medium", "msg": f"{n} images orphelines", "action": "docker_dangling", "size": 0})
        unused_vols = run("docker volume ls -q --filter 'dangling=true'")
        if unused_vols:
            n = len(unused_vols.splitlines())
            sug.append({"priority": "medium", "msg": f"{n} volumes inutilises", "action": "docker_volumes", "size": 0})

    # Unlinked
    stats = db.get_stats()
    if stats["unlinked_resources"] > 5:
        sug.append({"priority": "low", "msg": f"{stats['unlinked_resources']} ressources non liees",
                     "action": "nav:projects", "size": 0})

    # Downloads
    dl = HOME / "Downloads"
    if dl.exists():
        s = fast_size(str(dl))
        if s > 2 * 1024 * 1024 * 1024:
            sug.append({"priority": "medium", "msg": f"Downloads = {fmt(s)}", "action": "nav:storage", "size": s})

    # pip cache
    pip_cache = HOME / ".cache" / "pip"
    if pip_cache.exists():
        s = fast_size(str(pip_cache))
        if s > 500 * 1024 * 1024:
            sug.append({"priority": "low", "msg": f"Cache pip = {fmt(s)}", "action": "pip_cache", "size": s})

    return jsonify(sug)


# ═══════════════════════════════════════════════════════════════════════════
# PROJECTS
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/projects")
def api_projects():
    projects = db.get_projects()
    try:
        _listen = ports_mod.listening()
    except Exception:
        _listen = []
    for p in projects:
        p["state"] = P.state(p)                  # ok | missing | no_path
        p["managed"] = bool(p.get("path")) and P.is_managed(p["path"])
        try:                                     # type + needs summary (no network: fast)
            st = M.connections_status(p["id"], probe=False)
            p["needs"] = {"profile": st["profile"], "label": st["label"], "done": st["done"], "total": st["total"],
                          "items": [{"need": x["need"], "label": x["label"], "state": x["state"]} for x in st["needs"]],
                          "controller": bool(st["controller"])}
        except Exception:
            p["needs"] = None
        p["phase"] = PH.summary(p["id"])         # current phase + next step (never raises)
        try:                                      # ports held by processes running from the folder (one scan)
            root = P.resolve(p["path"]) if p.get("path") and os.path.isdir(p["path"]) else None
            p["running_ports"] = sorted({l["port"] for l in _listen if root and l["cwd"] and ports_mod._inside(l["cwd"], root)}) if root else []
        except Exception:
            p["running_ports"] = []
        # Real disk size of the project folder
        if p.get("path") and os.path.isdir(p["path"]):
            size = sizes.size(p["path"])           # cached, computed in the background
            p["disk_size"] = size or 0
            p["disk_size_h"] = sizes.fmt_or_pending(size, fmt)
        else:
            p["disk_size"] = 0
            p["disk_size_h"] = "—"
        p["total_size_h"] = fmt(p["total_size"])
        p["breakdown"] = db.get_project_resources_breakdown(p["id"])
        p["profile"] = p.get("profile", "")
        active_comps = db.get_project_components(p["id"])
        p["components"] = [c["component"] for c in active_comps if c["enabled"]]
    return jsonify(projects)


@app.route("/api/projects/<int:pid>/resources")
def api_project_resources(pid):
    resources = db.get_resources(project_id=pid)
    for r in resources:
        r["size_h"] = fmt(r["size"]) if r["size"] else "—"
    return jsonify(resources)


def _docker_rm(kind, name):
    """Remove a docker container/image/volume by name — argument list, no shell."""
    if not name or name.startswith("-"):
        raise ValueError(f"nom invalide : {name!r}")
    cmd = {"container": ["docker", "rm", "-f", name], "image": ["docker", "rmi", "-f", name],
           "volume": ["docker", "volume", "rm", "-f", name]}[kind]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])


def _trash_resource_file(path, project):
    """A tracked file (download, asset...) of this project → trash. Only paths
    DevPilot knows about: inside the project folder or recorded as its resource."""
    p = P.resolve(path)
    inside = project.get("path") and p != P.resolve(project["path"]) and p.is_relative_to(P.resolve(project["path"]))
    res = db.get_resource_by_path(path)
    if not inside and not (res and res.get("project_id") == project["id"]):
        raise P.ProjectError(f"{path} n'appartient pas a ce projet")
    if p == P.HOME or not p.is_relative_to(P.HOME):
        raise P.ProjectError(f"{path} : refuse")
    size = p.stat().st_size if p.is_file() else fast_size(str(p))
    P.move_to_trash(p)
    return size


@app.route("/api/projects/<int:pid>/clean", methods=["POST"])
def api_clean_project(pid):
    project = P.get(pid)
    cleaned, errors = [], []
    for r in db.get_resources(project_id=pid):
        try:
            if r["type"] in ("file", "directory"):
                if Path(r["path"]).exists():
                    _trash_resource_file(r["path"], project)
                    cleaned.append(r["name"])
            elif r["type"] in ("docker_container", "docker_image", "docker_volume"):
                _docker_rm(r["type"].split("_", 1)[1], r["name"])
                cleaned.append(f"{r['type'].split('_', 1)[1]}:{r['name']}")
            db.update_resource(r["id"], status="deleted")
            db.log_event("cleaned", r["type"], r["name"], r.get("path", ""), r.get("size", 0), pid)
        except Exception as e:
            errors.append(f"{r['name']}: {getattr(e, 'message', e)}")
    return jsonify({"success": True, "cleaned": cleaned, "errors": errors})


# ═══════════════════════════════════════════════════════════════════════════
# PROJECT LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════

def _get_git_info(project_path):
    """Get git status for a project directory."""
    if not project_path or not Path(project_path).exists():
        return None
    git_dir = Path(project_path) / ".git"
    if not git_dir.exists():
        return None

    branch = run(f"git -C '{project_path}' branch --show-current 2>/dev/null")
    last_commit = run(f"git -C '{project_path}' log -1 --format='%cr' 2>/dev/null")
    status_raw = run(f"git -C '{project_path}' status --porcelain 2>/dev/null")

    untracked = 0
    modified = 0
    staged = 0
    for line in status_raw.splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line[0] in ("M", "A", "D", "R"):
            staged += 1
        elif line[1] in ("M", "D"):
            modified += 1

    remote = run(f"git -C '{project_path}' remote get-url origin 2>/dev/null")
    is_clean = (untracked == 0 and modified == 0 and staged == 0)

    return {
        "branch": branch or "—",
        "last_commit": last_commit or "—",
        "untracked": untracked,
        "modified": modified,
        "staged": staged,
        "is_clean": is_clean,
        "remote": remote or "",
    }


def _get_project_caches(project_path):
    """Scan a project dir for build caches."""
    if not project_path or not Path(project_path).exists():
        return []
    caches = []
    for name, info in BUILD_CACHES.items():
        cp = Path(project_path) / name
        if cp.exists() and cp.is_dir() and not cp.is_symlink():
            try:
                s = dir_size(str(cp))
                if s > 1024:
                    caches.append({"name": name, "path": str(cp), "size": s, "size_h": fmt(s), **info})
            except (PermissionError, OSError):
                pass
    # node_modules/.cache
    nmc = Path(project_path) / "node_modules" / ".cache"
    if nmc.exists():
        try:
            s = dir_size(str(nmc))
            if s > 1024:
                caches.append({"name": "node_modules/.cache", "path": str(nmc), "size": s,
                               "size_h": fmt(s), "label": "Build cache", "tech": "node", "safe": True, "heavy": False})
        except (PermissionError, OSError):
            pass
    caches.sort(key=lambda x: x["size"], reverse=True)
    return caches


@app.route("/api/projects/<int:pid>/specs")
def api_get_specs(pid):
    specs = db.get_project_specs(pid)
    return jsonify(specs or {"wizard_data": {}, "checklist": [], "prompt": ""})


@app.route("/api/projects/<int:pid>/specs", methods=["POST"])
def api_save_specs(pid):
    data = request.get_json(silent=True) or {}
    db.save_project_specs(pid, data.get("wizard_data", {}), data.get("checklist", []), data.get("prompt", ""))
    P.write_space(pid)                    # prompt.md / checklist.json in the project folder
    return jsonify({"success": True})


@app.route("/api/projects/<int:pid>/checklist", methods=["POST"])
def api_update_checklist(pid):
    db.update_checklist(pid, (request.get_json(silent=True) or {}).get("checklist", []))
    P.write_space(pid)
    return jsonify({"success": True})


@app.route("/api/projects/<int:pid>/status")
def api_project_status(pid):
    """Complete project status: git, resources, ports, caches, disk."""
    project = db.get_project(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    ppath = project.get("path", "")

    # Git info (the folder itself) + every repo it contains
    git = _get_git_info(ppath)
    repos = P.find_repos(ppath) if ppath and os.path.isdir(ppath) else []
    for r in repos:
        r["state"] = P.git_state(r["path"])

    # Resources
    resources = db.get_resources(project_id=pid)
    for r in resources:
        r["size_h"] = fmt(r["size"]) if r["size"] else "—"

    # Ports
    ports = db.get_project_ports(pid)

    # Caches
    caches = _get_project_caches(ppath)
    total_cache = sum(c["size"] for c in caches)

    # Docker resources (filtered)
    docker = {"containers": [], "images": [], "volumes": []}
    for r in resources:
        if r["type"] == "docker_container":
            docker["containers"].append(r)
        elif r["type"] == "docker_image":
            docker["images"].append(r)
        elif r["type"] == "docker_volume":
            docker["volumes"].append(r)

    # Disk usage of project dir
    disk_total = 0
    if ppath and Path(ppath).exists():
        disk_total = sizes.size(ppath)

    # The project's own console (controller.py): declared or auto-detected, with its live status
    try:
        import controller as _CT
        ctrl = _CT.get(pid)
    except Exception:
        ctrl = None
    # Detect if project has its own console (run.sh or server.py)
    has_console = False
    if ppath and Path(ppath).is_dir():
        has_console = (Path(ppath) / "run.sh").exists() or (Path(ppath) / "server.py").exists()

    return jsonify({
        "project": {**project, "total_size_h": fmt(project.get("total_size", 0) or 0)},
        "git": git,
        "repos": repos,
        "github_org": (P.read_manifest(ppath).get("github") or {}).get("org") if ppath and os.path.isdir(ppath) else None,
        "state": P.state(project),
        "resources": resources,
        "ports": ports,
        "caches": caches,
        "total_cache": total_cache,
        "total_cache_h": fmt(total_cache),
        "docker": docker,
        "disk_total": disk_total or 0,
        "disk_total_h": sizes.fmt_or_pending(disk_total, fmt),
        "has_console": has_console or bool(ctrl),
        "controller": ctrl,
    })


@app.route("/api/projects/<int:pid>/cleanup-preview")
def api_cleanup_preview(pid):
    """Preview everything linked to a project, grouped for selective cleanup."""
    project = db.get_project(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    ppath = project.get("path", "")
    items = []
    total_reclaimable = 0

    # Caches in project dir
    caches = _get_project_caches(ppath)
    for c in caches:
        items.append({
            "category": "cache", "name": c["name"], "path": c["path"],
            "size": c["size"], "size_h": c["size_h"],
            "safe": c.get("safe", True), "detail": c.get("label", c["name"]),
        })
        if c.get("safe", True):
            total_reclaimable += c["size"]

    # Docker resources
    resources = db.get_resources(project_id=pid)
    for r in resources:
        if r["type"].startswith("docker_"):
            items.append({
                "category": "docker", "name": r["name"], "path": r.get("path", ""),
                "size": r.get("size", 0), "size_h": fmt(r.get("size", 0)),
                "safe": True, "detail": r["type"].replace("docker_", ""),
            })

    # Files/directories linked
    for r in resources:
        if r["type"] in ("file", "directory"):
            exists = Path(r["path"]).exists() if r.get("path") else False
            items.append({
                "category": "file", "name": r["name"], "path": r.get("path", ""),
                "size": r.get("size", 0), "size_h": fmt(r.get("size", 0)),
                "safe": True, "detail": r["type"], "exists": exists,
            })
            total_reclaimable += r.get("size", 0)

    # Ports
    ports = db.get_project_ports(pid)
    for p in ports:
        items.append({
            "category": "port", "name": f"Port {p['port']}", "path": "",
            "size": 0, "size_h": "—",
            "safe": True, "detail": p.get("process_name", ""),
        })

    return jsonify({
        "project": project,
        "items": items,
        "total_reclaimable": total_reclaimable,
        "total_reclaimable_h": fmt(total_reclaimable),
    })


@app.route("/api/projects/<int:pid>/cleanup", methods=["POST"])
def api_selective_cleanup(pid):
    """Selective cleanup of what the preview listed. Caches are deleted only
    inside the project folder, files go to the trash. The project folder
    itself is never deleted here: that is DELETE /api/projects/<id>."""
    project = P.get(pid)
    data = request.get_json(silent=True) or {}
    if data.get("delete_project_dir"):
        return jsonify({"success": False, "message":
                        "Pour supprimer le dossier du projet, utilise « Supprimer » (inspection + corbeille)."}), 409
    root = project.get("path", "")
    cleaned, errors = [], []

    for item in data.get("items", []):
        cat, path, name = item.get("category", ""), item.get("path", ""), item.get("name", "")
        try:
            if cat == "cache" and path and os.path.isdir(path):
                if not root:
                    raise P.ProjectError("projet sans dossier")
                s = dir_size(path)
                P.safe_rmtree(path, root)
                cleaned.append(f"Cache {name}: {fmt(s)}")
                db.log_event("cleaned", "cache", name, path, s, pid)

            elif cat == "docker":
                detail = item.get("detail", "")
                _docker_rm(detail, name)
                res = db.get_resource_by_path(path)
                if res:
                    db.update_resource(res["id"], status="deleted")
                cleaned.append(f"Docker {detail}: {name}")
                db.log_event("cleaned", f"docker_{detail}", name, "", 0, pid)

            elif cat == "file" and path:
                if Path(path).exists():
                    s = _trash_resource_file(path, project)
                    cleaned.append(f"{name}: {fmt(s)} (corbeille)")
                res = db.get_resource_by_path(path)
                if res:
                    db.update_resource(res["id"], status="deleted")
                db.log_event("cleaned", "file", name, path, 0, pid)

            elif cat == "port":
                cleaned.append(f"Port {name} (le process doit etre arrete)")

        except Exception as e:
            errors.append(f"{name}: {getattr(e, 'message', e)}")

    if data.get("set_done"):
        db.set_project_status(pid, "done")

    return jsonify({
        "success": True,
        "cleaned": cleaned,
        "errors": errors,
        "message": f"{len(cleaned)} elements nettoyes" + (f", {len(errors)} erreurs" if errors else ""),
    })


@app.route("/api/projects/<int:pid>/open-terminal", methods=["POST"])
def api_open_terminal(pid):
    """Open a real system terminal in the project directory."""
    project = db.get_project(pid)
    if not project:
        return jsonify({"success": False, "error": "Project not found"}), 404

    ppath = project.get("path", "")
    if not ppath or not os.path.isdir(ppath):
        return jsonify({"success": False, "error": "Chemin introuvable"})

    data = request.json or {}
    mode = data.get("mode", "bash")  # "bash" or "claude"
    title = f"DevPilot — {project['name']}"

    if mode == "claude":
        # Launch claude in the project dir
        cmd = ["gnome-terminal", "--title", title, "--working-directory", ppath,
               "--", "bash", "-c", "claude; exec bash"]
    else:
        cmd = ["gnome-terminal", "--title", title, "--working-directory", ppath]

    try:
        subprocess.Popen(cmd, start_new_session=True)
        return jsonify({"success": True, "mode": mode, "path": ppath})
    except FileNotFoundError:
        # Fallback to x-terminal-emulator
        try:
            import shlex
            fallback = ["x-terminal-emulator", "-e",
                        f"cd {shlex.quote(ppath)} && {'claude; exec bash' if mode == 'claude' else 'bash'}"]
            subprocess.Popen(fallback, start_new_session=True)
            return jsonify({"success": True, "mode": mode, "path": ppath})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})


@app.route("/api/projects/<int:pid>/launch-console", methods=["POST"])
def api_launch_console(pid):
    """Launch a project's own console/server (e.g. md_console for marocdefender)."""
    project = db.get_project(pid)
    if not project:
        return jsonify({"success": False, "error": "Project not found"}), 404

    ppath = project.get("path", "")
    if not ppath or not os.path.isdir(ppath):
        return jsonify({"success": False, "error": "Chemin du projet introuvable"})

    # Detect the launch method
    p = Path(ppath)
    run_sh = p / "run.sh"
    server_py = p / "server.py"

    if not (run_sh.exists() or server_py.exists()):
        return jsonify({"success": False, "error": "Pas de run.sh ou server.py dans ce projet"})

    # Read port from config.json if available
    port = 8800
    config_f = p / "config.json"
    if config_f.exists():
        try:
            port = json.loads(config_f.read_text()).get("port", 8800)
        except Exception:
            pass

    url = f"http://127.0.0.1:{port}"

    # Check if already running
    try:
        r = subprocess.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' -m 2 {url}",
            shell=True, capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "200":
            # Already running — just open in browser
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return jsonify({"success": True, "status": "already_running", "url": url})
    except Exception:
        pass

    # Launch the console — always prefer the venv python
    venv_py = p / ".venv" / "bin" / "python"
    if venv_py.exists() and server_py.exists():
        cmd = f"{venv_py} {server_py}"
    elif run_sh.exists():
        cmd = f"bash {run_sh}"
    else:
        cmd = f"python3 {server_py}"

    try:
        subprocess.Popen(
            cmd, shell=True, cwd=str(p),
            stdout=open("/tmp/devpilot-console.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

    # Wait for the server to be ready, then open browser
    for _ in range(20):
        time.sleep(0.5)
        try:
            r = subprocess.run(
                f"curl -s -o /dev/null -w '%{{http_code}}' -m 1 {url}",
                shell=True, capture_output=True, text=True, timeout=3,
            )
            if r.stdout.strip() == "200":
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return jsonify({"success": True, "status": "launched", "url": url})
        except Exception:
            pass

    return jsonify({"success": True, "status": "launched_no_confirm", "url": url,
                    "warning": "Le serveur a ete lance mais ne repond pas encore — verifie /tmp/devpilot-console.log"})


@app.route("/api/projects/<int:pid>/update-console", methods=["POST"])
def api_update_console(pid):
    """Met a jour la console du projet (git pull + assemble + restart)."""
    project = db.get_project(pid)
    if not project:
        return jsonify({"success": False, "error": "Projet introuvable"}), 404

    ppath = project.get("path", "")
    if not ppath or not os.path.isdir(ppath):
        return jsonify({"success": False, "error": "Chemin du projet introuvable"})

    p = Path(ppath)
    reconstruct = p / "reconstruct.sh"

    if reconstruct.exists():
        # Utiliser reconstruct.sh update (git pull + assemble + restart)
        try:
            r = subprocess.run(
                ["bash", str(reconstruct), "update"],
                capture_output=True, text=True, timeout=120, cwd=str(p))
            # Nettoyer les sequences ANSI pour la lisibilite
            import re as _re
            log = _re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout + r.stderr).strip())
            ok = r.returncode == 0
            return jsonify({"success": ok, "log": log[-1000:],
                            "message": "Console mise a jour" if ok else "Echec de la mise a jour"})
        except subprocess.TimeoutExpired:
            return jsonify({"success": False, "error": "Timeout (120s)"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        # Fallback : git pull + assemble manuellement
        try:
            log_parts = []
            # git pull
            r = subprocess.run(["git", "pull", "--ff-only"], capture_output=True, text=True,
                               timeout=30, cwd=str(p))
            log_parts.append(r.stdout.strip())
            if r.returncode != 0:
                return jsonify({"success": False, "error": "git pull echoue", "log": r.stderr})
            # assemble frontend
            assemble = p / "frontend-src" / "assemble.py"
            if assemble.exists():
                r2 = subprocess.run(["python3", str(assemble)], capture_output=True, text=True,
                                    timeout=30, cwd=str(p))
                log_parts.append(r2.stdout.strip())
            return jsonify({"success": True, "log": "\n".join(log_parts),
                            "message": "Console mise a jour (git pull + assemble)"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})


@app.route("/api/projects/<int:pid>/lifecycle", methods=["POST"])
def api_project_lifecycle(pid):
    """Change project lifecycle status."""
    status = request.json.get("status", "")
    if status not in ("active", "paused", "done"):
        return jsonify({"success": False, "message": "Status invalide"})
    db.set_project_status(pid, status)
    P.write_space(pid)
    db.log_event("lifecycle", "project", db.get_project(pid)["name"], "", 0, pid, details=f"Status: {status}")
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════════
# DEVPILOT SESSIONS — .devpilot/ folder inside each project
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_devpilot_dir(project_path):
    """Create .devpilot/ structure inside a project."""
    dp = Path(project_path) / ".devpilot"
    dp.mkdir(exist_ok=True)
    (dp / "sessions").mkdir(exist_ok=True)
    return dp


def _sync_connections_json(project_path, project_id):
    """Read .devpilot/connections.json written by Claude and sync into component configs."""
    conn_file = Path(project_path) / ".devpilot" / "connections.json"
    if not conn_file.exists():
        return

    try:
        data = json.loads(conn_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    # Map connection keys to component keys
    key_map = {
        "github": "git", "git": "git",
        "database": "database", "db": "database",
        "deploy": "deploy", "deployment": "deploy",
        "storage": "storage", "r2": "storage",
        "auth": "auth",
        "monitoring": "monitoring",
    }

    for conn_key, conn_config in data.items():
        if not isinstance(conn_config, dict):
            continue
        comp_key = key_map.get(conn_key, conn_key)

        # Special: github url also updates projects.git_remote
        if comp_key == "git" and conn_config.get("url"):
            db.update_project(project_id, git_remote=conn_config["url"])

        # Merge into existing component config
        existing = db.get_project_component(project_id, comp_key)
        if existing:
            merged = existing.get("config", {})
            merged.update(conn_config)
            db.update_project_component(project_id, comp_key, config=merged, enabled=True)
        else:
            db.add_project_component(project_id, comp_key, enabled=True, config=conn_config)


def _mask_connection_string(cs):
    """Mask password in connection string: postgresql://user:secret@host -> postgresql://user:****@host"""
    return re.sub(r'://([^:]+):([^@]+)@', r'://\1:****@', cs)


def _write_claude_md(project_path, project, specs):
    """Generate CLAUDE.md — the context file Claude can read."""
    dp = _ensure_devpilot_dir(project_path)
    wd = specs.get("wizard_data", {}) if specs else {}

    lines = [P.DEVPILOT_MARK, f"# {project['name']} — DevPilot Context\n"]
    if wd.get("client_name"):
        lines.append(f"Client: {wd['client_name']}")
    if wd.get("site_type"):
        lines.append(f"Type: {wd['site_type']}")
    if project.get("profile"):
        lines.append(f"Profil: {project['profile']}")
    if wd.get("domain_name"):
        lines.append(f"Domaine: {wd['domain_name']}")
    lines.append(f"Status: {project.get('status', 'active')}")
    if project.get("git_remote"):
        lines.append(f"Git: {project['git_remote']}")
    lines.append(f"Path: {project_path}")

    # Active components
    active_comps = db.get_project_components(project["id"])
    active_keys = [c["component"] for c in active_comps if c["enabled"]]
    if active_keys:
        lines.append(f"Composants actifs: {', '.join(active_keys)}")
    lines.append("")

    if wd.get("frontend_framework"):
        lines.append(f"## Stack")
        lines.append(f"- Frontend: {wd.get('frontend_framework', '')} + {wd.get('css_framework', '')}")
        if wd.get("needs_backend") == "yes":
            lines.append(f"- Backend: {wd.get('backend_framework', '')}")
        if wd.get("needs_db") == "yes":
            lines.append(f"- DB: {wd.get('db_type', '')}")
        lines.append("")

    # Checklist
    cl = specs.get("checklist", []) if specs else []
    if cl:
        lines.append("## Checklist")
        for t in cl:
            mark = "x" if t.get("done") else " "
            lines.append(f"- [{mark}] {t['text']}")
        lines.append("")

    # Sessions — list existing
    sess_dir = dp / "sessions"
    sessions = sorted(sess_dir.glob("*.md"), reverse=True)
    if sessions:
        lines.append("## Sessions")
        for s in sessions[:10]:
            lines.append(f"- [{s.stem}](.devpilot/sessions/{s.name})")
        lines.append("")

    # Connections — detailed per service, secrets masked
    has_connections = False
    conn_lines = []
    for c in active_comps:
        if not c["enabled"]:
            continue
        cfg = c.get("config", {})
        if isinstance(cfg, str):
            try: cfg = json.loads(cfg)
            except: cfg = {}
        comp_key = c["component"]

        if comp_key == "git" and project.get("git_remote"):
            url = project["git_remote"]
            conn_lines.append("### GitHub")
            conn_lines.append(f"- Repo: {url}")
            clone_url = url if url.endswith(".git") else url + ".git"
            conn_lines.append(f"- Clone: git clone {clone_url}")
            conn_lines.append("")
            has_connections = True

        elif comp_key == "deploy" and (cfg.get("site_url") or cfg.get("url") or cfg.get("platform")):
            platform = cfg.get("platform", "")
            account_name = cfg.get("account_name", "")
            conn_lines.append(f"### Hosting" + (f" ({platform})" if platform else ""))
            if platform: conn_lines.append(f"- Plateforme: {platform}")
            if account_name: conn_lines.append(f"- Compte: {account_name}")
            site = cfg.get("site_url") or cfg.get("url", "")
            if site: conn_lines.append(f"- URL du site: {site}")
            if cfg.get("project_url"): conn_lines.append(f"- Dashboard: {cfg['project_url']}")
            if cfg.get("domain"): conn_lines.append(f"- Domaine: {cfg['domain']}")
            # Token hint for Claude (account_id reference, not the actual token)
            if cfg.get("account_id"):
                conn_lines.append(f"- Token: configure dans DevPilot (compte {account_name})")
            conn_lines.append("")
            has_connections = True

        elif comp_key == "database" and (cfg.get("host") or cfg.get("provider") or cfg.get("type")):
            provider = cfg.get("provider") or cfg.get("type", "")
            conn_lines.append(f"### Base de donnees" + (f" ({provider})" if provider else ""))
            if provider: conn_lines.append(f"- Provider: {provider}")
            if cfg.get("host"): conn_lines.append(f"- Host: {cfg['host']}")
            if cfg.get("port"): conn_lines.append(f"- Port: {cfg['port']}")
            if cfg.get("name"): conn_lines.append(f"- Database: {cfg['name']}")
            if cfg.get("user"): conn_lines.append(f"- User: {cfg['user']}")
            if cfg.get("dashboard_url"): conn_lines.append(f"- Dashboard: {cfg['dashboard_url']}")
            if cfg.get("connection_string"):
                conn_lines.append(f"- Connection: {_mask_connection_string(cfg['connection_string'])}")
            conn_lines.append("")
            has_connections = True

        elif comp_key == "storage" and cfg.get("bucket"):
            conn_lines.append("### Stockage (Cloudflare R2)")
            conn_lines.append(f"- Bucket: {cfg['bucket']}")
            if cfg.get("dashboard_url"): conn_lines.append(f"- Dashboard: {cfg['dashboard_url']}")
            conn_lines.append("")
            has_connections = True

        elif comp_key == "email" and (cfg.get("provider") or cfg.get("addresses")):
            conn_lines.append("### Email")
            if cfg.get("provider"): conn_lines.append(f"- Provider: {cfg['provider']}")
            if cfg.get("addresses"): conn_lines.append(f"- Adresses: {cfg['addresses']}")
            if cfg.get("webmail_url"): conn_lines.append(f"- Webmail: {cfg['webmail_url']}")
            if cfg.get("dashboard_url"): conn_lines.append(f"- Dashboard: {cfg['dashboard_url']}")
            conn_lines.append("")
            has_connections = True

        elif comp_key == "domain" and cfg.get("domain"):
            conn_lines.append("### Domaine")
            conn_lines.append(f"- Domaine: {cfg['domain']}")
            if cfg.get("registrar"): conn_lines.append(f"- Registrar: {cfg['registrar']}")
            if cfg.get("ssl"): conn_lines.append(f"- SSL: {cfg['ssl']}")
            if cfg.get("dashboard_url"): conn_lines.append(f"- Dashboard: {cfg['dashboard_url']}")
            conn_lines.append("")
            has_connections = True

        elif comp_key == "server" and cfg.get("servers"):
            conn_lines.append("### Serveurs")
            for sv in cfg["servers"]:
                where = f"{sv.get('user')}@{sv.get('host')}" + (f":{sv['port']}" if sv.get("port") not in (22, None) else "")
                line = f"- {sv.get('name')} ({sv.get('role')}) : ssh {where}"
                if sv.get("remote_path"):
                    line += f", dossier {sv['remote_path']}"
                if sv.get("urls"):
                    line += ", " + " ".join(sv["urls"])
                conn_lines.append(line)
            conn_lines.append("")
            has_connections = True

        elif comp_key == "server" and cfg.get("ip"):
            conn_lines.append("### Serveur")
            conn_lines.append(f"- IP: {cfg['ip']}")
            if cfg.get("provider"): conn_lines.append(f"- Provider: {cfg['provider']}")
            if cfg.get("ssh_user"): conn_lines.append(f"- SSH: {cfg['ssh_user']}@{cfg['ip']}")
            if cfg.get("dashboard_url"): conn_lines.append(f"- Dashboard: {cfg['dashboard_url']}")
            conn_lines.append("")
            has_connections = True

        elif comp_key == "backup" and (cfg.get("bucket") or cfg.get("destination")):
            conn_lines.append("### Backup")
            if cfg.get("destination"): conn_lines.append(f"- Destination: {cfg['destination']}")
            if cfg.get("bucket"): conn_lines.append(f"- Bucket: {cfg['bucket']}")
            if cfg.get("frequency"): conn_lines.append(f"- Frequence: {cfg['frequency']}")
            conn_lines.append("")
            has_connections = True

    if has_connections:
        lines.append("## Connections\n")
        lines.extend(conn_lines)

    lines.append("## Prompt initial")
    lines.append("Voir: .devpilot/prompt.md\n")

    lines.append("## Instructions pour Claude")
    lines.append("")
    lines.append("Quand tu crees un repo GitHub, connectes une base de donnees, ou deploies :")
    lines.append("1. Ecris les infos dans `.devpilot/connections.json`")
    lines.append('2. Format: {"github":{"url":"..."},"database":{"type":"...","host":"...","port":5432,"name":"..."},"deploy":{"url":"...","platform":"vercel"},"storage":{"bucket":"..."}}')
    lines.append("3. DevPilot lira ce fichier automatiquement et affichera les connexions")
    lines.append("")
    lines.append("Quand l'utilisateur dit 'sauvegarde la session' ou 'save session' :")
    lines.append(f"1. Cree un fichier dans `.devpilot/sessions/` avec le format : `YYYY-MM-DD_titre.md`")
    lines.append(f"2. Le contenu doit resumer : ce qui a ete fait, ce qui reste a faire, les problemes rencontres")
    lines.append("")
    lines.append("Quand l'utilisateur dit 'lis la derniere session' ou 'reprends' :")
    lines.append(f"1. Lis le fichier le plus recent dans `.devpilot/sessions/`")
    lines.append(f"2. Reprends le travail la ou la session s'est arretee")
    lines.append("")

    (dp / "CLAUDE.md").write_text("\n".join(lines), encoding="utf-8")

    # Root CLAUDE.md for easy access — only if absent or written by DevPilot:
    # the repo's own CLAUDE.md is never overwritten
    root_md = Path(project_path) / "CLAUDE.md"
    if not root_md.exists() or P.is_devpilot_file(root_md):
        root_md.write_text("\n".join(lines), encoding="utf-8")
        P.exclude_from_git(project_path, (".devpilot/", "/CLAUDE.md"))
    else:
        P.exclude_from_git(project_path, (".devpilot/",))


@app.route("/api/projects/<int:pid>/init-devpilot", methods=["POST"])
def api_init_devpilot(pid):
    """Initialize .devpilot/ folder for a project, save prompt + checklist as .md files."""
    project = db.get_project(pid)
    if not project:
        return jsonify({"success": False, "message": "Projet introuvable"})

    ppath = project.get("path", "")
    if not ppath or not Path(ppath).exists():
        return jsonify({"success": False, "message": "Le projet n'a pas de dossier. Lie-le a Git d'abord."})

    specs = db.get_project_specs(pid)
    dp = P.init_space(ppath, project["name"])     # projects registered before .devpilot/ existed
    P.write_space(pid)

    # Save prompt.md
    if specs and specs.get("prompt"):
        (dp / "prompt.md").write_text(specs["prompt"], encoding="utf-8")

    # Save checklist.md
    if specs and specs.get("checklist"):
        cl_lines = ["# Checklist\n"]
        for t in specs["checklist"]:
            mark = "x" if t.get("done") else " "
            cl_lines.append(f"- [{mark}] {t['text']}")
        (dp / "checklist.md").write_text("\n".join(cl_lines), encoding="utf-8")

    # Sync connections from Claude's connections.json
    _sync_connections_json(ppath, pid)

    # Generate CLAUDE.md (after sync so it includes latest connections)
    project = db.get_project(pid)  # re-read to get updated git_remote
    _write_claude_md(ppath, project, specs)

    return jsonify({"success": True, "message": f".devpilot/ initialise dans {ppath}", "path": str(dp)})


@app.route("/api/projects/<int:pid>/sessions")
def api_list_sessions(pid):
    """List saved sessions for a project."""
    project = db.get_project(pid)
    if not project or not project.get("path"):
        return jsonify([])

    sess_dir = Path(project["path"]) / ".devpilot" / "sessions"
    if not sess_dir.exists():
        return jsonify([])

    sessions = []
    for f in sorted(sess_dir.glob("*.md"), reverse=True):
        content = f.read_text(encoding="utf-8")
        # Extract first line as title
        title = content.split("\n")[0].replace("# ", "").strip() if content else f.stem
        sessions.append({
            "name": f.stem,
            "filename": f.name,
            "title": title,
            "size": f.stat().st_size,
            "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "path": str(f),
        })
    return jsonify(sessions)


@app.route("/api/projects/<int:pid>/sessions", methods=["POST"])
def api_save_session(pid):
    """Save a new session note."""
    project = db.get_project(pid)
    if not project or not project.get("path"):
        return jsonify({"success": False, "message": "Projet sans dossier"})

    data = request.json
    title = data.get("title", "session").strip()
    content = data.get("content", "").strip()

    if not content:
        return jsonify({"success": False, "message": "Contenu vide"})

    dp = _ensure_devpilot_dir(project["path"])
    sess_dir = dp / "sessions"

    # Filename: date_title
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_title = re.sub(r'[^a-zA-Z0-9_-]', '-', title.lower())[:40]
    filename = f"{date_str}_{safe_title}.md"

    full_content = f"# {title}\n\n{content}\n"
    (sess_dir / filename).write_text(full_content, encoding="utf-8")

    # Update CLAUDE.md
    specs = db.get_project_specs(pid)
    _write_claude_md(project["path"], project, specs)

    return jsonify({"success": True, "message": f"Session sauvegardee: {filename}", "path": str(sess_dir / filename)})


@app.route("/api/projects/<int:pid>/sessions/<name>")
def api_get_session(pid, name):
    """Read a session file."""
    project = db.get_project(pid)
    if not project or not project.get("path"):
        return jsonify({"content": ""})

    f = Path(project["path"]) / ".devpilot" / "sessions" / (name + ".md")
    if not f.exists():
        return jsonify({"content": ""})
    return jsonify({"content": f.read_text(encoding="utf-8"), "path": str(f)})


@app.route("/api/projects/<int:pid>/sessions/<name>", methods=["DELETE"])
def api_delete_session(pid, name):
    project = db.get_project(pid)
    if not project or not project.get("path"):
        return jsonify({"success": False})
    f = Path(project["path"]) / ".devpilot" / "sessions" / (name + ".md")
    if f.exists():
        f.unlink()
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════════
# RUNNING PROJECTS
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/running")
def api_running():
    """Get all running projects — those with active ports, running containers, or dev processes."""
    import psutil as _ps

    projects = db.get_projects()
    projects = [p for p in projects if p.get("path")]
    running = []

    for proj in projects:
        ppath = proj["path"]
        items = {"ports": [], "containers": [], "processes": []}
        has_activity = False

        # Check ports linked to this project
        ports = db.get_project_ports(proj["id"])
        for pt in ports:
            items["ports"].append({"port": pt["port"], "process": pt.get("process_name", ""), "pid": pt.get("pid")})
            has_activity = True

        # Check running Docker containers linked to this project
        if run("docker info 2>/dev/null"):
            for line in run("docker ps --format json").splitlines():
                try:
                    c = json.loads(line)
                    cname = c.get("Names", "")
                    # Match by project name in container name
                    pname_lower = proj["name"].lower().replace("-", "").replace("_", "")
                    cname_lower = cname.lower().replace("-", "").replace("_", "")
                    if pname_lower in cname_lower or cname_lower in pname_lower:
                        items["containers"].append({
                            "name": cname, "image": c.get("Image", ""),
                            "status": c.get("Status", ""), "ports": c.get("Ports", ""),
                            "id": c.get("ID", ""),
                        })
                        has_activity = True
                except json.JSONDecodeError:
                    pass

        # Check running processes from this project dir
        try:
            for p in _ps.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_percent"]):
                try:
                    cwd = p.cwd()
                    if cwd and cwd.startswith(ppath):
                        mem = p.info.get("memory_info")
                        items["processes"].append({
                            "pid": p.info["pid"], "name": p.info["name"],
                            "memory_h": fmt(mem.rss) if mem else "—",
                            "cpu": p.info.get("cpu_percent", 0),
                        })
                        has_activity = True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        except Exception:
            pass

        if has_activity:
            running.append({
                "project": {
                    "id": proj["id"], "name": proj["name"],
                    "color": proj.get("color", "#8b5cf6"),
                    "path": ppath, "status": proj.get("status", "active"),
                },
                **items,
            })

    return jsonify(running)


@app.route("/api/running/stop", methods=["POST"])
def api_stop_project():
    """Stop all running resources for a project."""
    data = request.json
    pid = data.get("project_id")
    project = db.get_project(pid) if pid else None
    results = []

    # Kill processes
    for proc_pid in data.get("pids", []):
        try:
            psutil.Process(proc_pid).terminate()
            results.append(f"Process {proc_pid} termine")
        except Exception as e:
            results.append(f"Process {proc_pid}: {e}")

    # Stop containers
    for cname in data.get("containers", []):
        out = run(f"docker stop {cname}", timeout=30)
        results.append(f"Container {cname} arrete")

    # Log event
    if project:
        db.log_event("stopped", "project", project["name"], project.get("path", ""), 0, pid)

    return jsonify({"success": True, "results": results, "message": f"{len(results)} elements arretes"})


# ═══════════════════════════════════════════════════════════════════════════
# TERMINAL
# ═══════════════════════════════════════════════════════════════════════════

# ── Terminal sessions (PTY) ─────────────────────────────────────────────────
# A session outlives its WebSocket when opened with a session id (sid): closing
# the tab or changing page only detaches, so a long interactive script
# (e.g. reconstruct.sh) keeps running and the terminal can be reattached later.

IMPORT_RC = Path(__file__).resolve().parent / "import_rc.sh"
TERM_BUFFER_MAX = 256 * 1024
_term_sessions = {}
_term_lock = threading.Lock()


_DA_REPLY = re.compile(rb"\x1b\[[?>][0-9;]*c")


def _term_size(rows, cols):
    """The browser terminal's size (rows, cols), within sane bounds; 40×120 when unknown."""
    try:
        return max(5, min(int(rows), 500)), max(20, min(int(cols), 1000))
    except (TypeError, ValueError):
        return 40, 120


class TermSession:
    def __init__(self, sid, cwd, mode, argv=None, meta=None, size=None):
        self.sid, self.cwd, self.mode = sid, cwd, mode
        rows, cols = _term_size(*(size or (None, None)))
        self.meta = meta or {}                 # pid, repo, label, kind (terminal | claude | ssh | import…)
        self.buffer = bytearray()
        self.clients = set()
        self.lock = threading.Lock()
        self.alive = True
        self.created = time.time()

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        if mode in ("import", "link"):
            # rcfile: MD_ROOT (etc.) follow the current dir, `git clone <url> .` works in a project folder
            env["DEVPILOT_FOLLOW_CWD"] = db.get_setting("import_cwd_vars", "MD_ROOT")
            if mode == "link":
                env["DEVPILOT_TERM_TITLE"] = ("Lier a GitHub : clone ici (git clone <lien> .) ou lance ton script, "
                                              "puis clique Termine")
            argv = ["bash", "--rcfile", str(IMPORT_RC), "-i"]
        elif mode == "ssh" and argv:
            pass                                   # ssh to a project's server (servers.terminal_argv)
        elif mode == "claude":
            # Claude in this folder; when it ends the shell stays, in the same place
            argv = ["bash", "-lc", f"{dev_mod.claude_cmd()}; exec bash --login"]
        else:
            argv = ["bash", "--login"]

        # PERSISTENT (tmux): the project terminal and the « Développer » sessions
        # survive a DevPilot restart — DevPilot only attaches to them.
        self.persistent = (persist.available() and mode in ("", "claude")
                           and (sid.startswith("proj-") or sid.startswith("dev-")))
        if self.persistent:
            try:
                if not persist.exists(sid):
                    persist.create(sid, cwd, argv, self.meta, env={"TERM": "xterm-256color", "COLORTERM": "truecolor"},
                                   rows=rows, cols=cols)
                argv = persist.attach_argv(sid)
            except Exception:
                self.persistent = False          # tmux refuses: plain session, as before

        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # Child: never fall back into the Flask code, whatever happens
            try:
                os.chdir(cwd)
                # never let the shell inherit DevPilot's sockets (port 5555...): they would
                # outlive DevPilot and keep the port busy
                os.closerange(3, os.sysconf("SC_OPEN_MAX") if hasattr(os, "sysconf") else 4096)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(1)

        self.resize(rows, cols)
        threading.Thread(target=self._read_loop, daemon=True).start()

    def resize(self, rows, cols):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.kill(self.pid, signal.SIGWINCH)
        except Exception:
            pass

    def _read_loop(self):
        while True:
            try:
                data = os.read(self.fd, 16384)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.buffer += data
                if len(self.buffer) > TERM_BUFFER_MAX:
                    del self.buffer[:len(self.buffer) - TERM_BUFFER_MAX]
                clients = list(self.clients)
            text = data.decode("utf-8", errors="replace")
            for ws in clients:
                try:
                    ws.send(text)
                except Exception:
                    self.detach(ws)
        self.alive = False
        self._reap()
        for ws in list(self.clients):
            try:
                ws.send("\r\n\x1b[2m[processus termine]\x1b[0m\r\n")
                ws.close()
            except Exception:
                pass
        with _term_lock:
            if _term_sessions.get(self.sid) is self:
                del _term_sessions[self.sid]

    def attach(self, ws):
        with self.lock:
            self.clients.add(ws)
            backlog = bytes(self.buffer)
        if backlog:
            ws.send(backlog.decode("utf-8", errors="replace"))

    def detach(self, ws):
        with self.lock:
            self.clients.discard(ws)

    def write(self, data):
        if self.persistent:
            # tmux asks the browser terminal who it is (Device Attributes) when it attaches; xterm.js's
            # answers must not reach the shell as typed text (« 1;2c0;276;0c » before the first command)
            data = _DA_REPLY.sub(b"", data)
            if not data:
                return
        os.write(self.fd, data)

    def _reap(self):
        # Runs in the reader thread once the PTY is closed: blocking is fine
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def pids(self):
        """The shells running the session (tmux: its panes; otherwise our child)."""
        return persist.pane_pids(self.sid) if self.persistent else [self.pid]

    def detach_client(self):
        """DevPilot stops: persistent sessions stay in tmux, only our client goes."""
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except OSError:
                return
            time.sleep(0.05)

    def kill(self):
        if self.persistent:                      # for good: what runs in it, then the tmux session
            persist.kill(self.sid, ports_mod.kill_tree)
        # First what the shell started (a dev server keeps its own process group:
        # SIGHUP to bash would not reach it), then bash itself.
        try:
            for child in psutil.Process(self.pid).children(recursive=True):
                ports_mod.kill_tree(child.pid, grace=3)
        except Exception:
            pass
        # bash ignores SIGTERM when interactive: SIGHUP (like closing a
        # terminal window), then SIGKILL if it is still there.
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            for _ in range(10):
                try:
                    if os.waitpid(self.pid, os.WNOHANG)[0]:
                        return
                except ChildProcessError:
                    return
                time.sleep(0.05)


def _project_sessions(project):
    """Terminal sessions belonging to a project (by id, import name or folder)."""
    root = P.resolve(project["path"]) if project.get("path") else None
    with _term_lock:
        return [s for s in _term_sessions.values()
                if s.sid in (f"proj-{project['id']}", f"import-{project['name']}")
                or (root and P.resolve(s.cwd).is_relative_to(root))]


_EN_SORTIE = [False]       # DevPilot is stopping: persistent sessions are detached, not killed


def _close_project_sessions(project):
    sessions = _project_sessions(project)
    for s in sessions:
        with _term_lock:
            _term_sessions.pop(s.sid, None)
        if _EN_SORTIE[0] and getattr(s, "persistent", False):
            s.detach_client()
        else:
            s.kill()
    return len(sessions)


# ports.py sees the project's terminals (their shells) and can close them
ports_mod.session_pids = lambda project: [pid for s in _project_sessions(project) if s.alive for pid in s.pids()]
ports_mod.close_sessions = _close_project_sessions


def _stop_everything_on_exit(*_):
    """DevPilot stops: consoles it started and all terminals stop with it (setting stop_on_exit)."""
    try:
        _EN_SORTIE[0] = True
        if db.get_setting("stop_on_exit", "1") != "1":
            return
        ports_mod.close_all(db.get_projects())
    except Exception:
        pass


import atexit
atexit.register(_stop_everything_on_exit)
for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, lambda *_: (_stop_everything_on_exit(), os._exit(0)))
    except (ValueError, OSError):
        pass


# DevPilot's own idle shells don't block a move/delete (what runs in them does)
P.ignored_pids.append(lambda: [s.pid for s in list(_term_sessions.values())])
P.on_removed.append(_close_project_sessions)


def _restore_sessions():
    """At startup: the tmux sessions left by the previous DevPilot are attached again."""
    if not persist.available():
        return 0
    n = 0
    for x in persist.list_sessions():
        sid = x["sid"]
        if sid in _term_sessions:
            continue
        meta = {k: v for k, v in x["meta"].items() if k != "sid"}
        mode = "claude" if meta.get("kind") == "claude" else ""
        try:
            sess = TermSession(sid, x["cwd"] or str(DEVPILOT_PROJECTS), mode, None, meta=meta)
            sess.created = x["created"] or sess.created
            with _term_lock:
                _term_sessions[sid] = sess
            n += 1
        except Exception:
            continue
    return n


_restore_sessions()


@sock.route("/api/terminal/ws")
def terminal_ws(ws):
    """Real terminal via WebSocket + PTY.

    ?sid=<id>   persistent session: reattached if it exists, kept alive when
                the socket closes. Without sid the shell dies with the socket.
    ?mode=import  bash with import_rc.sh (env vars follow the current dir).
    """
    sid = request.args.get("sid", "").strip()
    mode = request.args.get("mode", "")
    cwd = os.path.expanduser(request.args.get("cwd", "") or str(DEVPILOT_PROJECTS))
    if not os.path.isdir(cwd):
        cwd = str(DEVPILOT_PROJECTS)

    argv = None
    if mode == "ssh":
        # the command comes from the stored server config, never from the request
        try:
            pid, server_id = int(request.args.get("project", 0)), request.args.get("server", "")
            argv, _srv = servers.terminal_argv(pid, server_id)
            sid = f"srv-{pid}-{server_id}"
        except (ValueError, P.ProjectError) as e:
            ws.send(f"\r\n\x1b[31m{getattr(e, 'message', e)}\x1b[0m\r\n")
            return

    with _term_lock:
        session = _term_sessions.get(sid) if sid else None
        if session is None or not session.alive:
            session = TermSession(sid or f"tmp-{time.time_ns()}", cwd, mode, argv,
                                  size=(request.args.get("rows"), request.args.get("cols")))
            if sid:
                _term_sessions[sid] = session

    from simple_websocket import ConnectionClosed
    session.attach(ws)
    try:
        while session.alive:
            try:
                data = ws.receive(timeout=5)
            except ConnectionClosed:
                break
            if data is None:
                continue  # timeout — keep waiting, don't kill terminal
            if isinstance(data, str):
                if data.startswith("\x1b[RESIZE:"):
                    try:
                        rows, cols = data.split(":")[1].rstrip("]").split(",")
                        session.resize(int(rows), int(cols))
                    except Exception:
                        pass
                    continue
                data = data.encode("utf-8")
            session.write(data)
    except Exception:
        pass
    finally:
        session.detach(ws)
        if not sid:
            session.kill()


def _session_info(s, projects_by_path):
    """What a session is, for humans: which project, which repo, which branch."""
    pid = s.meta.get("pid")
    proj = None
    if pid is None:
        for root, p in projects_by_path:
            if s.cwd == root or s.cwd.startswith(root + os.sep):
                proj = p
                break
    else:
        proj = next((p for _, p in projects_by_path if p["id"] == pid), None)
    repo = s.meta.get("repo")
    if repo is None and proj:
        rel = os.path.relpath(s.cwd, proj["_root"])
        repo = "." if rel == "." else rel
    br = ""
    if os.path.isdir(os.path.join(s.cwd, ".git")) or os.path.isdir(s.cwd):
        try:
            br = subprocess.run(["git", "-C", s.cwd, "branch", "--show-current"], capture_output=True,
                                text=True, timeout=3).stdout.strip()
        except Exception:
            br = ""
    kind = s.meta.get("kind") or {"claude": "claude", "ssh": "ssh", "import": "import", "link": "import"}.get(s.mode, "terminal")
    if s.sid.startswith("proj-"):
        kind, label = "terminal", "Terminal du projet"
    else:
        label = s.meta.get("label") or {"claude": "Claude", "ssh": "SSH", "import": "Import"}.get(kind, "Terminal")
    return {"sid": s.sid, "cwd": s.cwd, "mode": s.mode, "kind": kind, "label": label, "created": s.created,
            "persistent": bool(getattr(s, "persistent", False)),
            "clients": len(s.clients), "pid": proj["id"] if proj else None, "project": proj["name"] if proj else None,
            "repo": repo, "branch": br}


def _projects_by_path():
    out = []
    for p in P.all_projects():
        try:
            root = str(Path(p["path"]).expanduser().resolve()) if p.get("path") else None
        except OSError:
            root = None
        if root:
            out.append((root, {**p, "_root": root}))
    return sorted(out, key=lambda x: -len(x[0]))         # the deepest project wins


@app.route("/api/terminal/sessions")
def api_terminal_sessions():
    """Live sessions — all of them, or ?pid=<project> — with project, repo and branch."""
    with _term_lock:
        live = [s for s in _term_sessions.values() if s.alive and not s.sid.startswith("tmp-")]
    bp = _projects_by_path()
    items = [_session_info(s, bp) for s in live]
    pid = request.args.get("pid")
    if pid:
        items = [i for i in items if str(i["pid"]) == str(pid)]
    return jsonify(sorted(items, key=lambda i: i["created"]))


@app.route("/api/terminal/sessions", methods=["POST"])
def api_terminal_session_new():
    """A new session IN a repo of a project: {pid, repo, kind: terminal|claude, label?}."""
    data = request.json or {}
    try:
        pid = int(data.get("pid") or 0)
        repo = data.get("repo") or "."
        path = dev_mod.repo_path(pid, repo) if repo != "." or (dev_mod.project_root(pid)[1] / ".git").exists() \
            else dev_mod.project_root(pid)[1]
    except (ValueError, P.ProjectError) as e:
        return jsonify({"success": False, "error": getattr(e, "message", str(e))}), 400
    kind = "claude" if data.get("kind") == "claude" else "terminal"
    if kind == "claude" and not dev_mod.tools()["claude"]["ok"]:
        return jsonify({"success": False, "error": f"Claude introuvable : « {dev_mod.claude_cmd()} » — Réglages"}), 400
    name = Path(path).name
    label = (data.get("label") or "").strip()[:40] or (f"Claude · {name}" if kind == "claude" else name)
    sid = f"dev-{pid}-{time.time_ns() % 10**10}"
    with _term_lock:
        _term_sessions[sid] = TermSession(sid, str(path), "claude" if kind == "claude" else "", None,
                                          meta={"pid": pid, "repo": repo, "label": label, "kind": kind},
                                          size=(data.get("rows"), data.get("cols")))
    return jsonify({"success": True, "sid": sid, "cwd": str(path), "label": label, "kind": kind})


@app.route("/api/terminal/sessions/<sid>", methods=["DELETE"])
def api_terminal_session_kill(sid):
    with _term_lock:
        session = _term_sessions.pop(sid, None)
    if session:
        session.kill()
    return jsonify({"success": True, "killed": bool(session)})


@app.route("/api/terminal/exec", methods=["POST"])
def api_terminal_exec():
    """Execute a command in a project directory and return output."""
    cmd = request.json.get("cmd", "").strip()
    cwd = request.json.get("cwd", "").strip()

    if not cmd:
        return jsonify({"output": "", "exit_code": 1, "error": "Commande vide"})

    # Security: only allow execution inside home directory
    if cwd and not cwd.startswith(str(HOME)):
        return jsonify({"output": "", "exit_code": 1, "error": "Chemin non autorise"})

    if not cwd or not os.path.isdir(cwd):
        cwd = str(DEVPILOT_PROJECTS)

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=120, cwd=cwd,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr
        return jsonify({
            "output": output,
            "exit_code": result.returncode,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"output": "Timeout (120s)", "exit_code": 124})
    except Exception as e:
        return jsonify({"output": str(e), "exit_code": 1})


# ═══════════════════════════════════════════════════════════════════════════
# AUTO-DETECT & SCAN
# ═══════════════════════════════════════════════════════════════════════════

TECH_COLORS = {
    "package.json": "#fbbf24", "pubspec.yaml": "#38bdf8", "requirements.txt": "#34d399",
    "pyproject.toml": "#34d399", "build.gradle": "#06b6d4", "Cargo.toml": "#fb923c",
    "go.mod": "#60a5fa", "composer.json": "#8b5cf6", "pom.xml": "#ef4444",
    "setup.py": "#34d399", "Gemfile": "#fb7185",
}


@app.route("/api/scan/detect")
def api_scan_detect():
    """Scan project dirs and return detected projects not yet in DB."""
    scan_dirs = db.get_setting("scan_dirs", "~/Desktop")
    manifests = ["package.json", "pubspec.yaml", "requirements.txt", "setup.py",
                 "pyproject.toml", "build.gradle", "Cargo.toml", "go.mod",
                 "composer.json", "pom.xml", "Gemfile"]
    existing = {p["name"].lower() for p in db.get_projects()}
    # Also match by path
    existing_paths = {p.get("path", "") for p in db.get_projects() if p.get("path")}

    # Directories to ignore (library internals, build outputs, virtual envs)
    skip_parents = {"node_modules", ".git", "vendor", "venv", ".venv", "env",
                    "site-packages", "__pycache__", ".tox", ".eggs", "dist",
                    ".next", "build", ".dart_tool", ".gradle", "target"}

    detected = []
    seen = set()

    for d in scan_dirs.split(","):
        base = Path(os.path.expanduser(d.strip()))
        if not base.exists():
            continue
        for mf in manifests:
            try:
                for f in base.rglob(mf):
                    # Skip if inside any blacklisted parent dir
                    parts = set(f.relative_to(base).parts)
                    if parts & skip_parents:
                        continue
                    pdir = f.parent
                    pdir_str = str(pdir)
                    if pdir_str in seen:
                        continue
                    seen.add(pdir_str)
                    name = pdir.name
                    # Skip if already exists
                    if name.lower() in existing or pdir_str in existing_paths:
                        continue
                    # Must have a .git dir or be a direct child of scan dir (real project)
                    has_git = (pdir / ".git").exists()
                    depth = len(f.relative_to(base).parts) - 1  # manifest depth
                    if not has_git and depth > 2:
                        continue  # too deep = probably a sub-dependency

                    cache_size = 0
                    for cn in BUILD_CACHES:
                        cp = pdir / cn
                        if cp.exists() and cp.is_dir():
                            try:
                                cache_size += fast_size(str(cp))
                            except (PermissionError, OSError):
                                pass

                    detected.append({
                        "name": name,
                        "path": pdir_str,
                        "manifest": mf,
                        "color": TECH_COLORS.get(mf, "#8b5cf6"),
                        "cache_size": cache_size,
                        "cache_size_h": fmt(cache_size),
                        "has_git": has_git,
                        "depth": depth,
                    })
            except (PermissionError, OSError):
                pass

    # Deduplicate by name (keep the one with more caches)
    by_name = {}
    for d in detected:
        if d["name"] not in by_name or d["cache_size"] > by_name[d["name"]]["cache_size"]:
            by_name[d["name"]] = d
    result = sorted(by_name.values(), key=lambda x: x["cache_size"], reverse=True)
    return jsonify(result)


@app.route("/api/scan/import", methods=["POST"])
def api_scan_import():
    """Import scanned folders. mode: move (into ~/devpilot/projects, default) | link (keep in place)."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "move")
    imported, errors = [], []
    for p in data.get("projects", []):
        try:
            proj = P.adopt(p.get("path", ""), mode=mode, name=p.get("name"))
            imported.append({"id": proj["id"], "name": proj["name"], "path": proj["path"]})
        except P.ProjectError as e:
            errors.append(f'{p.get("name", "?")} : {e.message}')
    return jsonify({"success": True, "imported": len(imported), "projects": imported, "errors": errors})


# ═══════════════════════════════════════════════════════════════════════════
# RESOURCES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/resources")
def api_resources():
    resources = db.get_resources(type=request.args.get("type"))
    for r in resources:
        r["size_h"] = fmt(r["size"]) if r["size"] else "—"
    return jsonify(resources)


@app.route("/api/resources/<int:rid>/link", methods=["POST"])
def api_link_resource(rid):
    db.link_resource_to_project(rid, request.json["project_id"])
    return jsonify({"success": True})


@app.route("/api/resources/<int:rid>/temporary", methods=["POST"])
def api_set_temporary(rid):
    db.set_resource_temporary(rid, expire_days=request.json.get("days", 7))
    return jsonify({"success": True})


@app.route("/api/resources/<int:rid>/permanent", methods=["POST"])
def api_set_permanent(rid):
    db.set_resource_permanent(rid)
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/events")
def api_events():
    return jsonify(db.get_events(
        limit=request.args.get("limit", 50, type=int),
        since_hours=request.args.get("hours", type=int),
    ))


@app.route("/api/events/poll")
def api_events_poll():
    return jsonify(db.get_unnotified_events())


# ═══════════════════════════════════════════════════════════════════════════
# RULES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/rules")
def api_rules():
    return jsonify(db.get_rules())


@app.route("/api/rules", methods=["POST"])
def api_create_rule():
    data = request.json
    db.add_rule(pattern=data["pattern"], project_id=data["project_id"],
                resource_type=data.get("resource_type", "any"),
                auto_temporary=data.get("auto_temporary", False),
                expire_days=data.get("expire_days", 0))
    return jsonify({"success": True})


@app.route("/api/rules/<int:rid>", methods=["DELETE"])
def api_delete_rule(rid):
    db.delete_rule(rid)
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════════
# SETTINGS
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/settings")
def api_settings():
    settings = db.get_all_settings()
    # Mask sensitive values
    sensitive = ("token", "secret", "key", "password", "passphrase")
    for k in list(settings):
        v = settings[k]
        if k == "hosting_accounts":                       # list with plaintext tokens: never sent
            try:
                accts = json.loads(v or "[]")
                settings[k] = json.dumps([{**a, "token": "****" if a.get("token") else ""} for a in accts])
            except (ValueError, TypeError):
                settings[k] = "[]"
        elif any(x in k.lower() for x in sensitive) and isinstance(v, str) and v:
            settings[k] = v[:4] + "****" if len(v) > 4 else "****"
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    for k, v in request.json.items():
        # Skip masked values (don't overwrite real values with ****)
        if isinstance(v, str) and "****" in v:
            continue
        db.set_setting(k, v)
    return jsonify({"success": True})


@app.route("/api/settings/raw/<key>")
def api_settings_raw(key):
    """Get a single setting unmasked (for internal use by test-connection flows)."""
    val = db.get_setting(key, "")
    return jsonify({"key": key, "value": val})


# ═══════════════════════════════════════════════════════════════════════════
# WATCHER STATUS
# ═══════════════════════════════════════════════════════════════════════════
# NOTE: GitHub/R2/Cloud routes are in cloud_routes.py (registered as blueprint)

@app.route("/api/watcher/status")
def api_watcher_status():
    running = run("systemctl --user is-active devpilot-watcher.service 2>/dev/null") == "active"
    if not running:
        running = run("systemctl --user is-active pc-clean-watcher.service 2>/dev/null") == "active"
    if not running:
        pid_file = Path(__file__).parent / "watcher.pid"
        if pid_file.exists():
            try:
                running = psutil.pid_exists(int(pid_file.read_text().strip()))
            except (ValueError, Exception):
                pass
    return jsonify({"running": running})


# ═══════════════════════════════════════════════════════════════════════════
# CLEANUP ACTIONS
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/clean", methods=["POST"])
def api_clean():
    action = request.json.get("action", "")
    result = {"success": False, "message": ""}

    # ── Trash ──
    if action == "trash":
        trash = HOME / ".local" / "share" / "Trash"
        before = dir_size(str(trash)) if trash.exists() else 0
        if trash.exists():
            # Try normal rm first, then sudo for root-owned files (Docker volumes etc.)
            for sub in ["files", "info", "expunged"]:
                p = trash / sub
                if p.exists():
                    run(f"rm -rf '{p}' 2>/dev/null", timeout=30)
                    if p.exists():
                        run(f"sudo rm -rf '{p}' 2>/dev/null", timeout=60)
            for item in trash.iterdir():
                run(f"rm -rf '{item}' 2>/dev/null", timeout=30)
                if item.exists():
                    run(f"sudo rm -rf '{item}' 2>/dev/null", timeout=30)
            (trash / "files").mkdir(exist_ok=True)
            (trash / "info").mkdir(exist_ok=True)
        run("gio trash --empty 2>/dev/null", timeout=30)
        after = dir_size(str(trash)) if trash.exists() else 0
        freed = before - after
        msg = f"{fmt(freed)} liberes"
        if after > 1024 * 1024:
            msg += f" ({fmt(after)} restent — fichiers root, lancer: sudo rm -rf ~/.local/share/Trash/expunged)"
        db.log_event("cleaned", "trash", "Corbeille", "", freed)
        result = {"success": True, "message": msg}

    # ── Expired ──
    elif action == "clean_expired":
        expired = db.get_expired_resources()
        n = 0
        for r in expired:
            if r.get("path") and os.path.exists(r["path"]):
                try:
                    p = Path(r["path"])
                    shutil.rmtree(str(p)) if p.is_dir() else p.unlink()
                    n += 1
                except Exception:
                    pass
            db.update_resource(r["id"], status="deleted")
        result = {"success": True, "message": f"{n} fichiers expires supprimes"}

    # ── Docker ──
    elif action == "docker_containers":
        out = run("docker container prune -f", timeout=60)
        db.log_event("cleaned", "docker", "Containers prune", "", 0)
        result = {"success": True, "message": out or "Containers nettoyes"}

    elif action == "docker_dangling":
        out = run("docker image prune -f", timeout=60)
        db.log_event("cleaned", "docker", "Images prune", "", 0)
        result = {"success": True, "message": out or "Images nettoyees"}

    elif action == "docker_volumes":
        out = run("docker volume prune -f", timeout=60)
        db.log_event("cleaned", "docker", "Volumes prune", "", 0)
        result = {"success": True, "message": out or "Volumes nettoyes"}

    elif action == "docker_cache":
        out = run("docker builder prune -a -f", timeout=120)
        db.log_event("cleaned", "docker", "Build cache", "", 0)
        result = {"success": True, "message": out or "Build cache nettoye"}

    elif action == "docker_all":
        out = run("docker system prune -a --volumes -f", timeout=180)
        db.log_event("cleaned", "docker", "System prune all", "", 0)
        result = {"success": True, "message": out or "Docker nettoye completement"}

    elif action == "docker_rm":
        kind = request.json.get("kind", "")
        name = request.json.get("name", "")
        cmds = {"container": f"docker rm -f {name}", "image": f"docker rmi -f {name}",
                "volume": f"docker volume rm -f {name}"}
        if kind in cmds and name:
            out = run(cmds[kind], timeout=30)
            res = db.get_resource_by_path(f"docker:{kind}:{name}")
            if res:
                db.update_resource(res["id"], status="deleted")
            db.log_event("deleted", f"docker_{kind}", name, "", 0)
            result = {"success": True, "message": out or f"{kind} {name} supprime"}

    # ── Caches ──
    elif action == "pip_cache":
        run("pip cache purge", timeout=30)
        result = {"success": True, "message": "Cache pip nettoye"}

    elif action == "clear_browser":
        key = request.json.get("browser", "")
        if key in BROWSER_CACHES:
            info = BROWSER_CACHES[key]
            freed = 0
            for lbl, rp in info["paths"]:
                p = HOME / rp
                if p.exists():
                    try:
                        freed += dir_size(str(p))
                        shutil.rmtree(str(p), ignore_errors=True)
                    except (PermissionError, OSError):
                        pass
            if key == "firefox":
                ff = HOME / ".mozilla" / "firefox"
                if ff.exists():
                    for profile in ff.iterdir():
                        if profile.is_dir() and not profile.is_symlink():
                            for cn in ("cache2", "shader-cache", "startupCache"):
                                cp = profile / cn
                                if cp.exists():
                                    try:
                                        freed += dir_size(str(cp))
                                        shutil.rmtree(str(cp), ignore_errors=True)
                                    except (PermissionError, OSError):
                                        pass
            db.log_event("cleaned", "browser_cache", info["label"], "", freed)
            result = {"success": True, "message": f"{info['label']}: {fmt(freed)} liberes"}

    elif action == "clear_project_cache":
        path = request.json.get("path", "")
        if path and os.path.isdir(path) and str(HOME) in path:
            s = dir_size(path)
            shutil.rmtree(path, ignore_errors=True)
            db.log_event("cleaned", "cache", Path(path).name, path, s)
            result = {"success": True, "message": f"{Path(path).name}: {fmt(s)} liberes"}

    elif action == "clear_project_all":
        pdir = request.json.get("dir", "")
        if pdir and os.path.isdir(pdir):
            freed, n = 0, 0
            for cn, ci in BUILD_CACHES.items():
                cp = Path(pdir) / cn
                if cp.exists() and cp.is_dir() and ci["safe"]:
                    try:
                        freed += dir_size(str(cp))
                        shutil.rmtree(str(cp), ignore_errors=True)
                        n += 1
                    except (PermissionError, OSError):
                        pass
            nmc = Path(pdir) / "node_modules" / ".cache"
            if nmc.exists():
                try:
                    freed += dir_size(str(nmc))
                    shutil.rmtree(str(nmc), ignore_errors=True)
                    n += 1
                except (PermissionError, OSError):
                    pass
            db.log_event("cleaned", "cache", Path(pdir).name, pdir, freed)
            result = {"success": True, "message": f"{n} caches: {fmt(freed)} liberes"}

    elif action == "clear_all_pycache":
        scan = db.get_setting("scan_dirs", "~/Desktop")
        n, freed = 0, 0
        for d in scan.split(","):
            base = Path(os.path.expanduser(d.strip()))
            if not base.exists():
                continue
            for root, dirs, _ in os.walk(str(base)):
                for dn in dirs:
                    if dn == "__pycache__":
                        p = Path(root) / dn
                        try:
                            freed += dir_size(str(p))
                            shutil.rmtree(str(p), ignore_errors=True)
                            n += 1
                        except (PermissionError, OSError):
                            pass
        db.log_event("cleaned", "cache", f"__pycache__ x{n}", "", freed)
        result = {"success": True, "message": f"{n} __pycache__: {fmt(freed)} liberes"}

    elif action == "clear_global":
        key = request.json.get("key", "")
        found = next((g for g in GLOBAL_CACHES if g["key"] == key), None)
        if not found:
            result = {"success": False, "message": "Cache inconnu"}
        elif found.get("cmd"):
            run(found["cmd"], timeout=60)
            result = {"success": True, "message": f"{found['label']} nettoye"}
        else:
            p = HOME / found["path"]
            if p.exists():
                s = dir_size(str(p))
                shutil.rmtree(str(p), ignore_errors=True)
                result = {"success": True, "message": f"{found['label']}: {fmt(s)} liberes"}

    elif action == "delete_cache_dir":
        name = request.json.get("name", "")
        p = HOME / ".cache" / name
        if name and p.exists() and p.is_dir():
            s = dir_size(str(p))
            shutil.rmtree(str(p), ignore_errors=True)
            result = {"success": True, "message": f"{name}: {fmt(s)} liberes"}

    elif action == "delete_file":
        path = request.json.get("path", "")
        if path and path.startswith(str(HOME)):
            p = Path(path)
            try:
                s = p.stat().st_size if p.is_file() else dir_size(str(p))
                shutil.rmtree(str(p)) if p.is_dir() else p.unlink()
                res = db.get_resource_by_path(path)
                if res:
                    db.update_resource(res["id"], status="deleted")
                db.log_event("deleted", "file", p.name, path, s)
                result = {"success": True, "message": f"{p.name}: {fmt(s)} liberes"}
            except Exception as e:
                result = {"success": False, "message": str(e)}

    elif action == "kill_process":
        pid = request.json.get("pid", 0)
        if pid:
            try:
                psutil.Process(pid).terminate()
                result = {"success": True, "message": f"Process {pid} termine"}
            except Exception as e:
                result = {"success": False, "message": str(e)}

    elif action.startswith("nav:"):
        result = {"success": True, "message": "", "navigate": action[4:]}

    else:
        result = {"success": False, "message": f"Action inconnue: {action}"}

    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════
# PROJECT COMPONENTS & PROFILES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/components")
def api_components():
    """List all available component definitions."""
    return jsonify(comp.ALL_COMPONENTS)


@app.route("/api/profiles")
def api_profiles():
    """List all profile definitions with their components."""
    profiles = []
    for key, comps in comp.PROFILE_COMPONENTS.items():
        profiles.append({
            "key": key,
            "label": comp.PROFILE_LABELS.get(key, key),
            "description": comp.PROFILE_DESCRIPTIONS.get(key, ""),
            "icon": comp.PROFILE_ICONS.get(key, ""),
            "components": comps,
        })
    return jsonify(profiles)


@app.route("/api/projects/<int:pid>/components")
def api_project_components(pid):
    """Get active components for a project."""
    return jsonify(comp.get_project_components(pid))


@app.route("/api/projects/<int:pid>/components", methods=["POST"])
def api_set_component(pid):
    """Enable/disable a component for a project."""
    data = request.json
    component = data.get("component", "")
    enabled = data.get("enabled", True)
    config = data.get("config")
    ok = comp.set_component(pid, component, enabled=enabled, config=config)
    if not ok:
        return jsonify({"success": False, "message": f"Composant inconnu: {component}"}), 400
    return jsonify({"success": True})


@app.route("/api/projects/<int:pid>/components/<component>", methods=["PUT"])
def api_update_component(pid, component):
    """Update a component's config."""
    data = request.json
    config = data.get("config", {})
    db.update_project_component(pid, component, config=config)
    return jsonify({"success": True})


@app.route("/api/projects/<int:pid>/components/<component>", methods=["DELETE"])
def api_delete_component(pid, component):
    """Remove a component from a project."""
    db.delete_project_component(pid, component)
    return jsonify({"success": True})


@app.route("/api/projects/<int:pid>/detect", methods=["POST"])
def api_detect_components(pid):
    """Scan a project directory and detect which components are present."""
    project = db.get_project(pid)
    if not project or not project.get("path"):
        return jsonify({"error": "Projet sans chemin"}), 400
    if not os.path.isdir(project["path"]):
        return jsonify({"error": f"Dossier introuvable: {project['path']}"}), 400
    result = comp.detect_and_suggest_profile(project["path"])
    return jsonify(result)


@app.route("/api/projects/<int:pid>/apply-profile", methods=["POST"])
def api_apply_profile(pid):
    """Apply a profile to a project, creating component rows."""
    data = request.json or {}
    profile = data.get("profile", "custom")
    project = db.get_project(pid)
    if not project:
        return jsonify({"success": False, "message": "Projet introuvable"}), 404

    # Optionally detect components to merge config
    detected = {}
    if project.get("path") and os.path.isdir(project["path"]):
        detected = comp.detect_components(project["path"])

    # If custom profile with explicit components list
    custom_components = data.get("components", [])
    if profile == "custom" and custom_components:
        # Override detected with explicit selection
        for c in custom_components:
            if c not in detected:
                detected[c] = {"detected": False, "evidence": [], "config": {}}

    applied = comp.apply_profile(pid, profile, detected=detected)
    return jsonify({"success": True, "profile": profile, "components": applied})


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import webbrowser, threading
    port = 5555
    print(f"\n  DevPilot -> http://localhost:{port}\n")
    # Open browser on launch (desktop shortcut or terminal)
    if not os.environ.get("DEVPILOT_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False)
