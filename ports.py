"""DevPilot — what a project is RUNNING, and stopping it cleanly.

A project's processes are: its console (controller.py), what was started in
its DevPilot terminals (a dev server, a watcher...), and any process whose
working directory is inside the project folder. They own ports. When the
project is closed, they are stopped and the ports are freed, so the next
start is never "address already in use" or an old version still answering.
"""

import os
import signal
import time
from pathlib import Path

import psutil

import projects as P
from projects import ProjectError

# set by dashboard.py: project dict -> [bash pids of its terminal sessions]
session_pids = lambda project: []          # noqa: E731
# set by dashboard.py: project dict -> number of sessions closed
close_sessions = lambda project: 0         # noqa: E731


def _inside(cwd, root):
    try:
        return bool(cwd) and Path(cwd).resolve().is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False


def listening():
    """Every LISTEN socket on this machine: {port, pid, name, cmd, cwd, create_time}."""
    out, seen = [], set()
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        conns = []
    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.laddr or not c.pid:
            continue
        key = (c.laddr.port, c.pid)
        if key in seen:
            continue
        seen.add(key)
        try:
            p = psutil.Process(c.pid)
            out.append({"port": c.laddr.port, "addr": c.laddr.ip, "pid": c.pid, "name": p.name(),
                        "cmd": " ".join(p.cmdline())[:160], "cwd": p.cwd() if hasattr(p, "cwd") else "",
                        "create_time": p.create_time()})
        except (psutil.Error, OSError):
            out.append({"port": c.laddr.port, "addr": c.laddr.ip, "pid": c.pid, "name": "?", "cmd": "", "cwd": "",
                        "create_time": 0})
    return sorted(out, key=lambda x: x["port"])


def _descendants(pid):
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return []


def kill_tree(pid, grace=5.0):
    """Stop a process and everything it started (children first), SIGTERM then SIGKILL.
    Never touches DevPilot itself. Returns the pids that were stopped."""
    me = os.getpid()
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return []
    procs = [root] + _descendants(pid)
    procs = [p for p in procs if p.pid != me]
    stopped = []
    for p in reversed(procs):                 # leaves first: a parent must not respawn them
        try:
            p.terminate()
            stopped.append(p.pid)
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=grace)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=2)
    return stopped


def _known_pids(project):
    """pids DevPilot knows for this project: its console, its terminals and their children."""
    pids = set()
    try:
        import controller
        ctrl = controller.get(project["id"])
        if ctrl and (ctrl.get("status") or {}).get("pid"):
            pids.add(ctrl["status"]["pid"])
    except Exception:
        pass
    for bpid in session_pids(project) or []:
        pids.add(bpid)
        pids.update(p.pid for p in _descendants(bpid))
    return pids


def project_ports(pid):
    """Ports held by this project's processes, with where they come from."""
    project = P.get(pid)
    root = P.resolve(project["path"]) if project.get("path") and os.path.isdir(project["path"]) else None
    known = _known_pids(project)
    console_pid = None
    try:
        import controller
        ctrl = controller.get(pid)
        console_pid = (ctrl.get("status") or {}).get("pid") if ctrl else None
    except Exception:
        ctrl = None
    term_pids = set()
    for bpid in session_pids(project) or []:
        term_pids.add(bpid)
        term_pids.update(p.pid for p in _descendants(bpid))
    own_ports = {l["port"] for l in listening() if l["pid"] == os.getpid()}     # DevPilot's own port(s)
    out = []
    for l in listening():
        if l["pid"] == os.getpid() or l["port"] in own_ports:
            continue
        mine = l["pid"] in known or (root and _inside(l["cwd"], root))
        if not mine:
            continue
        source = "console" if l["pid"] == console_pid else "terminal" if l["pid"] in term_pids else "dossier"
        out.append({**l, "source": source, "url": f"http://127.0.0.1:{l['port']}",
                    "label": {"console": "console du projet", "terminal": "lance dans un terminal DevPilot",
                              "dossier": "lance depuis le dossier du projet"}[source]})
    docker = []
    try:
        import purge
        if root:
            for g in purge.docker_plan(root):
                for c in g["containers"]:
                    if c["running"] and c["ports"]:
                        docker.append({"name": c["name"], "ports": c["ports"], "compose": g["name"]})
    except Exception:
        pass
    return {"ports": out, "docker": docker, "terminals": len(session_pids(project) or []),
            "console": ctrl and {"name": ctrl.get("name"), "running": (ctrl.get("status") or {}).get("running"),
                                 "pid": console_pid, "url": ctrl.get("url")}}


def stop_port(pid, port):
    """Free a port held by this project (and only by it): stops the process tree."""
    port = int(port)
    entry = next((x for x in project_ports(pid)["ports"] if x["port"] == port), None)
    if not entry:
        held = next((x for x in listening() if x["port"] == port), None)
        if held:
            raise ProjectError(f"Le port {port} est tenu par « {held['name']} » (pid {held['pid']}) qui n'appartient pas "
                               f"a ce projet : DevPilot ne l'arrete pas. Commande : kill {held['pid']}")
        return {"port": port, "freed": True, "message": f"Le port {port} est deja libre"}
    if entry["source"] == "console":
        import controller
        controller.stop(pid)
    else:
        kill_tree(entry["pid"])
    freed = _wait_free(port)
    return {"port": port, "freed": freed, "stopped": entry["name"],
            "message": f"Port {port} libere ({entry['name']} arrete)" if freed else f"Le port {port} est encore occupe"}


def _wait_free(port, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(x["port"] == port for x in listening()):
            return True
        time.sleep(0.2)
    return not any(x["port"] == port for x in listening())


def close_project(pid, stop_console=True, stop_terminals=True, stop_folder=True):
    """Close the project: stop its console, its terminals (and what they run), the
    processes running from its folder. Docker stacks are left alone (see « Supprimer »)."""
    project = P.get(pid)
    before = project_ports(pid)
    log = []
    if stop_console and before["console"] and before["console"]["running"]:
        try:
            import controller
            controller.stop(pid)
            log.append(f"console {before['console']['name']} arretee")
        except Exception as e:
            log.append(f"console : {getattr(e, 'message', e)}")
    if stop_terminals:
        n = close_sessions(project)
        if n:
            log.append(f"{n} terminal(aux) ferme(s)")
    if stop_folder:
        root = P.resolve(project["path"]) if project.get("path") and os.path.isdir(project["path"]) else None
        for l in listening():
            if l["pid"] != os.getpid() and root and _inside(l["cwd"], root) and psutil.pid_exists(l["pid"]):
                kill_tree(l["pid"])
                log.append(f"{l['name']} (port {l['port']}) arrete")
    freed = [x["port"] for x in before["ports"] if not any(y["port"] == x["port"] for y in listening())]
    time.sleep(0.3)
    after = project_ports(pid)
    return {"log": log, "freed": sorted(set(freed)), "still": [x["port"] for x in after["ports"]],
            "message": ("Projet ferme : " + ", ".join(log)) if log else "Rien ne tournait pour ce projet"}


def close_all(projects):
    """DevPilot exits: what it started follows it (consoles started by DevPilot, terminals)."""
    import controller
    for project in projects:
        try:
            ctrl = controller.get(project["id"])
            st = (ctrl or {}).get("status") or {}
            if st.get("running") and st.get("started_by_devpilot"):
                controller.stop(project["id"])
        except Exception:
            pass
        try:
            close_sessions(project)
        except Exception:
            pass
