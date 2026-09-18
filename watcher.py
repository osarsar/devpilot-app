#!/usr/bin/env python3
"""DevPilot Watcher — Project-aware filesystem, Docker, and port monitoring."""

import os
import sys
import re
import json
import subprocess
import threading
import time
import signal
from pathlib import Path
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import db

HOME = Path.home()

TEMP_EXTENSIONS = {
    ".deb", ".rpm", ".exe", ".msi", ".dmg", ".AppImage",
    ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".rar", ".7z",
    ".tmp", ".log", ".bak", ".swp",
}

TEMP_EXPIRE_DAYS = int(db.get_setting("temp_expire_days", "7"))


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def notify(title, body, icon="dialog-information", urgency="normal"):
    if db.get_setting("notify_enabled", "1") != "1":
        return
    try:
        subprocess.Popen(
            ["notify-send", f"--icon={icon}", f"--urgency={urgency}",
             "--app-name=DevPilot", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def get_file_size(path):
    try:
        p = Path(path)
        if p.is_file():
            return p.stat().st_size
        elif p.is_dir():
            total = 0
            for entry in os.scandir(path):
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except (PermissionError, OSError):
                    pass
            return total
    except (PermissionError, OSError):
        pass
    return 0


def fmt_size(b):
    for u in ["B", "KB", "MB", "GB"]:
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


def is_temp_file(path):
    p = Path(path)
    ext = p.suffix.lower()
    if p.name.endswith(".tar.gz"):
        ext = ".tar.gz"
    return ext in TEMP_EXTENSIONS


# ═══════════════════════════════════════════════════════════════════════════
# PROJECT DETECTION — path-based (replaces hardcoded regex)
# ═══════════════════════════════════════════════════════════════════════════

def guess_project_for_file(full_path):
    """Detect project for a file using path matching, then rules.
    Returns (project_id, is_temporary, expire_days)."""
    name = os.path.basename(full_path)

    # 1. Check user-defined rules first
    rule = db.match_rules(name, "file")
    if rule:
        return rule["project_id"], rule.get("auto_temporary", False), rule.get("expire_days", TEMP_EXPIRE_DAYS)

    # 2. Path-based matching (the smart way)
    pid, pname = db.find_project_by_path(full_path)
    if pid:
        return pid, False, 0

    return None, False, 0


def guess_project_for_docker(resource_name):
    """Detect project for a Docker resource using name matching.
    Returns project_id or None."""
    # 1. Check user rules
    rule = db.match_rules(resource_name, "docker")
    if rule:
        return rule["project_id"]

    # 2. Dynamic name-based matching against all projects
    pid, _ = db.find_project_by_name_hint(resource_name)
    return pid


def auto_register_project(dir_path):
    """If a new directory has .git, auto-register it as a project.
    Returns project_id or None."""
    p = Path(dir_path)
    git_dir = p / ".git"

    if not git_dir.exists():
        return None

    name = p.name

    # Check if already registered
    existing = db.get_projects()
    for proj in existing:
        if proj["name"].lower() == name.lower() or proj.get("path") == str(p):
            return proj["id"]

    # Read git remote
    git_remote = ""
    try:
        config = git_dir / "config"
        if config.exists():
            text = config.read_text()
            match = re.search(r'url\s*=\s*(.+)', text)
            if match:
                git_remote = match.group(1).strip()
    except (PermissionError, OSError):
        pass

    # Detect tech/color from manifest
    color = "#8b5cf6"
    tech_colors = {
        "package.json": "#fbbf24", "pubspec.yaml": "#38bdf8",
        "requirements.txt": "#34d399", "pyproject.toml": "#34d399",
        "build.gradle": "#06b6d4", "Cargo.toml": "#fb923c",
        "go.mod": "#60a5fa", "pom.xml": "#ef4444",
    }
    for mf, c in tech_colors.items():
        if (p / mf).exists():
            color = c
            break

    # Create project
    pid = db.create_project(
        name=name,
        color=color,
        description=f"Auto-detected from git clone",
        path=str(p),
        git_remote=git_remote,
    )

    # Auto-create rule
    pattern = re.escape(name.lower())
    db.add_rule(pattern=pattern, project_id=pid, resource_type="any")

    print(f"[watcher] Auto-registered project: {name} ({git_remote or 'no remote'})")
    notify(
        "Nouveau projet detecte",
        f"{name}\n{git_remote or dir_path}",
        icon="folder-new",
    )

    return pid


# ═══════════════════════════════════════════════════════════════════════════
# FILE SYSTEM WATCHER
# ═══════════════════════════════════════════════════════════════════════════

class FileHandler(FileSystemEventHandler):
    def __init__(self, watch_dir):
        self.watch_dir = watch_dir
        self._debounce = {}

    def _should_process(self, path):
        now = time.time()
        last = self._debounce.get(path, 0)
        if now - last < 2.0:
            return False
        self._debounce[path] = now
        self._debounce = {k: v for k, v in self._debounce.items() if now - v < 10}
        return True

    def on_created(self, event):
        path = event.src_path
        name = os.path.basename(path)

        if name.startswith(".") or name.endswith(".crdownload") or name.endswith(".part"):
            return

        if not self._should_process(path):
            return

        # Wait for file to finish writing
        time.sleep(1.0)

        if not os.path.exists(path):
            return

        # If it's a directory with .git, auto-register as project
        if os.path.isdir(path):
            # Check .git after a short delay (git clone creates dir then populates)
            def check_git():
                time.sleep(3.0)
                if (Path(path) / ".git").exists():
                    auto_register_project(path)
            threading.Thread(target=check_git, daemon=True).start()

        size = get_file_size(path)
        is_dir = os.path.isdir(path)
        resource_type = "directory" if is_dir else "file"
        temp = is_temp_file(path) if not is_dir else False

        # Project detection (path-based)
        project_id, rule_temp, rule_expire = guess_project_for_file(path)
        if rule_temp:
            temp = True
        expire_days = rule_expire if rule_expire > 0 else TEMP_EXPIRE_DAYS

        rid = db.add_resource(
            type=resource_type, name=name, path=path, size=size,
            project_id=project_id, is_temporary=temp,
            expire_days=expire_days if temp else 0,
        )

        # Update project activity
        if project_id:
            db.update_project_activity(project_id)

        project_name = ""
        if project_id:
            try:
                project_name = db.get_project(project_id)["name"]
            except Exception:
                pass

        db.log_event(
            type="created", resource_type=resource_type,
            resource_name=name, resource_path=path, size=size,
            project_id=project_id,
            details=f"{'Temp' if temp else 'Perm'}" + (f" | {project_name}" if project_name else ""),
        )

        msg_parts = []
        if size > 0:
            msg_parts.append(fmt_size(size))
        msg_parts.append(f"{'Temp' if temp else 'Perm'}")
        if project_name:
            msg_parts.append(project_name)

        notify(
            f"{'📁' if is_dir else '📄'} {name}",
            " — ".join(msg_parts),
            icon="folder" if is_dir else "document-new",
        )

    def on_deleted(self, event):
        path = event.src_path
        if not self._should_process(path):
            return
        name = os.path.basename(path)
        if name.startswith("."):
            return
        resource = db.get_resource_by_path(path)
        if resource:
            db.update_resource(resource["id"], status="deleted")
            db.log_event(
                type="deleted", resource_type=resource["type"],
                resource_name=name, resource_path=path,
                project_id=resource.get("project_id"),
            )


# ═══════════════════════════════════════════════════════════════════════════
# DOCKER WATCHER
# ═══════════════════════════════════════════════════════════════════════════

def watch_docker():
    if db.get_setting("watch_docker", "1") != "1":
        return

    try:
        proc = subprocess.Popen(
            ["docker", "events", "--format", "{{json .}}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except FileNotFoundError:
        return

    for line in proc.stdout:
        try:
            event = json.loads(line.strip())
        except json.JSONDecodeError:
            continue

        etype = event.get("Type", "")
        action = event.get("Action", "")
        actor = event.get("Actor", {})
        attrs = actor.get("Attributes", {})
        name = attrs.get("name", "") or attrs.get("image", "") or actor.get("ID", "")[:12]

        if etype == "container" and action == "create":
            project_id = guess_project_for_docker(name)
            image = attrs.get("image", "")
            db.add_resource(type="docker_container", name=name,
                            path=f"docker:container:{name}", project_id=project_id)
            if project_id:
                db.update_project_activity(project_id)
            db.log_event(type="created", resource_type="docker_container",
                         resource_name=name, project_id=project_id, details=f"Image: {image}")
            notify("Container cree", f"{name}\n{image}", icon="docker")

        elif etype == "container" and action in ("destroy", "die"):
            resource = db.get_resource_by_path(f"docker:container:{name}")
            if resource:
                db.update_resource(resource["id"], status="deleted")
            db.log_event(type="deleted", resource_type="docker_container", resource_name=name)

        elif etype == "image" and action == "pull":
            image_name = actor.get("ID", name)
            project_id = guess_project_for_docker(image_name)
            db.add_resource(type="docker_image", name=image_name,
                            path=f"docker:image:{image_name}", project_id=project_id)
            db.log_event(type="created", resource_type="docker_image",
                         resource_name=image_name, project_id=project_id)

        elif etype == "image" and action in ("delete", "untag"):
            db.log_event(type="deleted", resource_type="docker_image", resource_name=name)

        elif etype == "volume" and action == "create":
            project_id = guess_project_for_docker(name)
            db.add_resource(type="docker_volume", name=name,
                            path=f"docker:volume:{name}", project_id=project_id)
            db.log_event(type="created", resource_type="docker_volume",
                         resource_name=name, project_id=project_id)


# ═══════════════════════════════════════════════════════════════════════════
# PORT SCANNER — links ports to projects via process cwd
# ═══════════════════════════════════════════════════════════════════════════

def scan_ports():
    """Periodically scan listening ports, link them to projects via /proc/pid/cwd."""
    while True:
        try:
            import psutil
            active_ports = set()

            for conn in psutil.net_connections(kind='tcp'):
                if conn.status != 'LISTEN':
                    continue
                port = conn.laddr.port
                pid = conn.pid
                active_ports.add(port)

                if not pid:
                    continue

                # Resolve process info
                try:
                    proc = psutil.Process(pid)
                    pname = proc.name()
                    cwd = proc.cwd()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pname = ""
                    cwd = ""

                # Try to link to project via cwd
                project_id = None
                if cwd:
                    project_id, _ = db.find_project_by_path(cwd)

                db.add_or_update_port(port, pid=pid, process_name=pname, project_id=project_id)

            # Close stale ports
            db.close_stale_ports(active_ports)

        except Exception as e:
            print(f"[port-scanner] Error: {e}", file=sys.stderr)

        time.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════
# SCAN EXISTING DOCKER RESOURCES
# ═══════════════════════════════════════════════════════════════════════════

def scan_existing_docker():
    try:
        # Containers
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=10,
        )
        for line in out.stdout.strip().splitlines():
            parts = line.split("\t")
            if parts:
                name = parts[0]
                project_id = guess_project_for_docker(name)
                db.add_resource(type="docker_container", name=name,
                                path=f"docker:container:{name}", project_id=project_id)

        # Images
        out = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, timeout=10,
        )
        for line in out.stdout.strip().splitlines():
            name = line.strip()
            if name and name != "<none>:<none>":
                project_id = guess_project_for_docker(name)
                db.add_resource(type="docker_image", name=name,
                                path=f"docker:image:{name}", project_id=project_id)

        # Volumes
        out = subprocess.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            capture_output=True, text=True, timeout=10,
        )
        for line in out.stdout.strip().splitlines():
            name = line.strip()
            if name:
                project_id = guess_project_for_docker(name)
                db.add_resource(type="docker_volume", name=name,
                                path=f"docker:volume:{name}", project_id=project_id)

    except Exception as e:
        print(f"[scan-docker] Error: {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════════
# EXPIRY CHECKER
# ═══════════════════════════════════════════════════════════════════════════

def check_expired():
    while True:
        try:
            expired = db.get_expired_resources()
            if expired:
                total_size = sum(r.get("size", 0) for r in expired)
                notify(
                    f"{len(expired)} fichiers expires",
                    f"{fmt_size(total_size)} recuperables",
                    urgency="normal",
                )
                for r in expired:
                    db.log_event(
                        type="expired", resource_type=r["type"],
                        resource_name=r["name"], resource_path=r.get("path", ""),
                        size=r.get("size", 0), project_id=r.get("project_id"),
                    )
        except Exception as e:
            print(f"[expiry-checker] Error: {e}", file=sys.stderr)
        time.sleep(3600)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("[devpilot] Starting v3...")

    # Scan existing Docker resources
    print("[devpilot] Scanning Docker...")
    scan_existing_docker()

    # File watchers
    observer = Observer()
    watch_dirs = []

    if db.get_setting("watch_downloads", "1") == "1":
        dl = HOME / "Downloads"
        if dl.exists():
            watch_dirs.append(str(dl))

    if db.get_setting("watch_desktop", "1") == "1":
        dt = HOME / "Desktop"
        if dt.exists():
            watch_dirs.append(str(dt))

    for wd in watch_dirs:
        handler = FileHandler(wd)
        observer.schedule(handler, wd, recursive=False)
        print(f"[devpilot] Watching: {wd}")

    observer.start()

    # Docker event stream
    docker_thread = threading.Thread(target=watch_docker, daemon=True)
    docker_thread.start()
    print("[devpilot] Docker watcher started")

    # Port scanner
    port_thread = threading.Thread(target=scan_ports, daemon=True)
    port_thread.start()
    print("[devpilot] Port scanner started")

    # Expiry checker
    expiry_thread = threading.Thread(target=check_expired, daemon=True)
    expiry_thread.start()
    print("[devpilot] Expiry checker started")

    notify("DevPilot actif", "Surveillance fichiers, Docker, ports", icon="security-high")

    def shutdown(sig, frame):
        print("\n[devpilot] Shutting down...")
        observer.stop()
        observer.join()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
