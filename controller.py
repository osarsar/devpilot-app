"""DevPilot — a project's own management console ("controller").

Some projects ship their own console: a folder (e.g. ``md_console/``) with a
``run.sh`` or ``server.py`` and a ``config.json`` giving its port; it serves
http://127.0.0.1:<port> and manages hosting, servers, deploys... for that
project. DevPilot does not replace it: it finds it, starts/stops it and shows
its log.

The controller is stored in the project's folder, .devpilot/project.json
under "controller":
  {name, dir (relative to the project, "." for root), command (e.g. ./run.sh),
   url (local http only), manages: [hosting, servers, ...], auto: bool}
When nothing is declared, the first detected candidate with a port is used
(``auto: True``). A console started by DevPilot records its pid in
.devpilot/console.pid and logs to .devpilot/console.log; it runs in its own
session, so it survives a DevPilot restart.
"""

import http.client
import json
import os
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import psutil

import projects as P
from projects import ProjectError

KEY = "controller"
PIDFILE = "console.pid"
LOGFILE = "console.log"
DEFAULT_MANAGES = ["hosting", "servers", "deploy", "domain", "email", "backup"]

_lock = threading.Lock()          # one start/stop at a time
_children = {}                    # os pid -> Popen of consoles started by this process (to reap them)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never through a proxy


# ── Lookups ─────────────────────────────────────────────────────────────────

def _root(pid):
    project = P.get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        raise ProjectError("Le dossier du projet est introuvable")
    return project


def _ctrl_dir(path, ctrl):
    return (Path(path) / (ctrl.get("dir") or ".")).resolve()


def _port_of(url):
    try:
        u = urlparse(url or "")
        return u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None


def _config_port(folder):
    """Port declared in <folder>/config.json, or None."""
    try:
        port = int(json.loads((folder / "config.json").read_text(encoding="utf-8")).get("port"))
        return port if 1 <= port <= 65535 else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def detect(path):
    """Console candidates: the project folder and its direct subfolders that
    contain run.sh or server.py. Folders named *console* come first."""
    path = Path(path)
    if not path.is_dir():
        return []
    folders = [path]
    try:
        folders += sorted(d for d in path.iterdir()
                          if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")
                          and d.name not in P.SKIP_DIRS)
    except OSError:
        pass
    ranked = []
    for d in folders:
        has_run, has_srv = (d / "run.sh").is_file(), (d / "server.py").is_file()
        if not (has_run or has_srv):
            continue
        rel = "." if d == path else d.name
        port = _config_port(d)
        ranked.append(("console" not in d.name.lower(), {
            "dir": rel, "name": "console" if rel == "." else d.name,
            "command": "./run.sh" if has_run else "python3 server.py",
            "port": port, "url": f"http://127.0.0.1:{port}" if port else None}))
    ranked.sort(key=lambda r: r[0])                  # stable: root, then subfolders by name
    return [c for _, c in ranked]


def _declared(path):
    """The controller written in the manifest, normalised, or None."""
    c = P.read_manifest(path).get(KEY)
    if not isinstance(c, dict) or not c.get("url") or not c.get("command"):
        return None
    manages = c.get("manages")
    return {"name": str(c.get("name") or c.get("dir") or "console"), "dir": str(c.get("dir") or "."),
            "command": str(c["command"]), "url": str(c["url"]),
            "manages": [str(m) for m in manages] if isinstance(manages, list) else list(DEFAULT_MANAGES),
            "auto": False}


def get(pid):
    """The project's controller (declared, else first detected with a URL)
    with its live status, or None."""
    project = _root(pid)
    path = Path(project["path"])
    ctrl = _declared(path)
    if ctrl is None:
        cand = next((c for c in detect(path) if c["url"]), None)
        if cand is None:
            return None
        ctrl = {"name": cand["name"], "dir": cand["dir"], "command": cand["command"], "url": cand["url"],
                "manages": list(DEFAULT_MANAGES), "auto": True}
    ctrl["status"] = status(ctrl, path)
    return ctrl


# ── Declare / clear ─────────────────────────────────────────────────────────

def _clean(cfg, path):
    """Validated controller config (raises ProjectError with a clear message)."""
    cfg = cfg or {}
    root = path.resolve()
    rel = str(cfg.get("dir") or ".").strip() or "."
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise ProjectError("Le dossier de la console doit etre dans le dossier du projet")
    if not target.is_dir():
        raise ProjectError(f"Dossier de la console introuvable : {rel}")
    rel = "." if target == root else str(target.relative_to(root))

    command = cfg.get("command")
    if not isinstance(command, str) or not command.strip() or any(ch in command for ch in "\n\r\0"):
        raise ProjectError("Commande de lancement manquante ou invalide (ex. ./run.sh)")

    url = str(cfg.get("url") or "").strip()
    try:
        u = urlparse(url)
        u.port                                        # ValueError on a bad port
        local = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
    except ValueError:
        local = False
    if not url or not local:
        raise ProjectError("L'URL de la console doit etre locale : http://127.0.0.1:PORT ou http://localhost:PORT")

    manages = cfg.get("manages")
    if manages is None:
        manages = list(DEFAULT_MANAGES)
    if not isinstance(manages, (list, tuple)) or not all(isinstance(m, str) for m in manages):
        raise ProjectError("« manages » doit etre une liste de noms (hosting, servers, deploy...)")
    seen, cleaned = set(), []
    for m in (m.strip() for m in manages):
        if m and m not in seen:
            seen.add(m)
            cleaned.append(m)

    name = str(cfg.get("name") or "").strip()[:60] or ("console" if rel == "." else rel)
    return {"name": name, "dir": rel, "command": command.strip(), "url": url, "manages": cleaned, "auto": False}


def _save_manifest(path, mutate):
    dp = P.space_dir(path)
    dp.mkdir(parents=True, exist_ok=True)
    m = P.read_manifest(path)
    mutate(m)
    P._write_json(dp / "project.json", m)


def declare(pid, cfg):
    """Record the project's console in its manifest. Returns get(pid)."""
    project = _root(pid)
    path = Path(project["path"])
    ctrl = _clean(cfg, path)
    _save_manifest(path, lambda m: m.__setitem__(KEY, ctrl))
    return get(pid)


def clear(pid):
    """Forget the declared console (detection takes over). Returns get(pid)."""
    project = _root(pid)
    _save_manifest(Path(project["path"]), lambda m: m.pop(KEY, None))
    return get(pid)


# ── Status ──────────────────────────────────────────────────────────────────

def _answers(url, timeout=2):
    """True when ANY HTTP response comes back from the URL (even 401/404)."""
    if not url:
        return False
    try:
        with _opener.open(urllib.request.Request(url, headers={"User-Agent": "DevPilot"}), timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        return False


def _reap(os_pid):
    """Collect the exit status of a console we started (no zombie left behind)."""
    proc = _children.get(os_pid)
    if proc is not None and proc.poll() is not None:
        _children.pop(os_pid, None)


def _alive(os_pid):
    _reap(os_pid)
    try:
        alive = psutil.Process(os_pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        alive = False
    except psutil.AccessDenied:
        alive = True
    if not alive:
        _reap(os_pid)                  # it died between the two checks: collect it now
    return alive


def _pid_from_file(path):
    try:
        os_pid = int((P.space_dir(path) / PIDFILE).read_text().strip())
    except (OSError, ValueError):
        return None
    return os_pid if os_pid > 0 and _alive(os_pid) else None


def _find_pid(url, ctrl_dir):
    """A process listening on the URL's port, else one running server.py/run.sh
    from the console folder. Never DevPilot itself."""
    me, port = os.getpid(), _port_of(url)
    try:
        for c in psutil.net_connections(kind="inet"):
            if (c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port
                    and c.pid and c.pid != me):
                return c.pid
    except (psutil.Error, OSError):
        pass
    for p in psutil.process_iter(["pid", "cwd", "cmdline"]):
        try:
            cwd, cmd = p.info.get("cwd"), " ".join(p.info.get("cmdline") or [])
            if (p.info["pid"] != me and cwd and Path(cwd) == ctrl_dir
                    and ("server.py" in cmd or "run.sh" in cmd)):
                return p.info["pid"]
        except (psutil.Error, ValueError):
            continue
    return None


def status(ctrl, path):
    """{running, pid, started_by_devpilot, url}: running = the URL answers;
    pid from our pidfile (then started_by_devpilot) or found via psutil."""
    url = ctrl.get("url") or ""
    os_pid = _pid_from_file(path)
    ours = os_pid is not None
    if os_pid is None:
        os_pid = _find_pid(url, _ctrl_dir(path, ctrl))
    stale, cwd = False, ""
    if os_pid:
        try:
            proc = psutil.Process(os_pid)
            cwd = proc.cwd()
            started = proc.create_time()
            stale = _code_mtime(_ctrl_dir(Path(path), ctrl)) > started + 1
        except (psutil.Error, OSError):
            pass
    return {"running": _answers(url), "pid": os_pid, "started_by_devpilot": ours, "url": url,
            "stale": stale, "cwd": cwd}


def _code_mtime(cdir):
    """Newest source file of the console (what a restart would load)."""
    newest = 0.0
    try:
        for root, dirs, files in os.walk(cdir):
            dirs[:] = [d for d in dirs if d not in (".git", ".venv", "venv", "node_modules", "__pycache__", "data")]
            for f in files:
                if f.endswith((".py", ".html", ".js", ".css", ".sh", ".json")):
                    try:
                        newest = max(newest, os.stat(os.path.join(root, f)).st_mtime)
                    except OSError:
                        pass
            if Path(root).relative_to(cdir).parts[:1] and len(Path(root).relative_to(cdir).parts) > 3:
                dirs[:] = []
    except OSError:
        pass
    return newest


# ── Start / stop ────────────────────────────────────────────────────────────

def _tail(file, n):
    """Last ``n`` lines of a (possibly big) log file."""
    try:
        with open(file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:] if n else []


def _terminate(os_pid, grace=5):
    """SIGTERM the process group (SIGKILL after ``grace`` s). Never DevPilot's own group."""
    if os_pid == os.getpid():
        return
    try:
        pgid = os.getpgid(os_pid)
    except ProcessLookupError:
        _reap(os_pid)
        return
    group = pgid != os.getpgid(0)

    def send(sig):
        try:
            os.killpg(pgid, sig) if group else os.kill(os_pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            raise ProjectError(f"Impossible d'arreter le process {os_pid} (permission refusee)")

    send(signal.SIGTERM)
    end = time.monotonic() + grace
    while time.monotonic() < end:
        if not _alive(os_pid):
            return
        time.sleep(0.1)
    send(signal.SIGKILL)
    end = time.monotonic() + 3
    while time.monotonic() < end and _alive(os_pid):
        time.sleep(0.1)


def _controller(pid):
    ctrl = get(pid)
    if ctrl is None:
        raise ProjectError("Aucune console detectee dans ce projet (dossier avec run.sh ou server.py + config.json)")
    return ctrl


def start(pid, wait=20):
    """Launch the console in its folder (own session, log in .devpilot/console.log)
    and wait up to ``wait`` s for its URL. Already running: nothing is launched.
    A console that exits, or never answers in time (then it is killed), raises."""
    project = _root(pid)
    path = Path(project["path"])
    ctrl = _controller(pid)
    url = ctrl["url"]
    with _lock:
        st = status(ctrl, path)
        if st["running"]:
            return st
        cwd = _ctrl_dir(path, ctrl)
        if st["pid"]:
            own = st["started_by_devpilot"] or _same_dir(st.get("cwd"), cwd)
            if not own:
                try:
                    who = " ".join(psutil.Process(st["pid"]).cmdline())[:120]
                except psutil.Error:
                    who = "?"
                raise ProjectError(f"Le port de la console est tenu par un autre programme (pid {st['pid']} : {who}). "
                                   f"Arrete-le, ou change l'URL de la console.")
            _terminate(st["pid"])             # an old instance that no longer answers: replaced
            (P.space_dir(path) / PIDFILE).unlink(missing_ok=True)
        if not cwd.is_dir():
            raise ProjectError(f"Dossier de la console introuvable : {ctrl.get('dir') or '.'}")
        dp = P.space_dir(path)
        dp.mkdir(parents=True, exist_ok=True)
        log, pidfile = dp / LOGFILE, dp / PIDFILE
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        with log.open("ab") as out:
            out.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} devpilot : {ctrl['command']} (dans {cwd})\n"
                      .encode("utf-8"))
            try:
                proc = subprocess.Popen(["bash", "-c", ctrl["command"]], cwd=str(cwd), env=env,
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        start_new_session=True)
            except OSError as e:
                raise ProjectError(f"Impossible de lancer la console : {e}")
        _children[proc.pid] = proc
        pidfile.write_text(f"{proc.pid}\n")

        end = time.monotonic() + wait
        while True:
            if _answers(url):
                break
            if proc.poll() is not None:
                if _answers(url):            # answered just before exiting? (a daemon that forks)
                    break
                _children.pop(proc.pid, None)
                pidfile.unlink(missing_ok=True)
                raise ProjectError("La console s'est arretee : " + "\n".join(_tail(log, 10)))
            if time.monotonic() >= end:
                _terminate(proc.pid)
                pidfile.unlink(missing_ok=True)
                raise ProjectError(f"La console ne repond pas sur {url} apres {wait}s, elle a ete arretee : "
                                   + "\n".join(_tail(log, 10)))
            time.sleep(0.3)
        return status(ctrl, path)


def _same_dir(a, b):
    try:
        return bool(a) and Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return False


def restart(pid, wait=20):
    """Stop then start: the console loads the current code (after a git pull, an edit...)."""
    stop(pid)
    end = time.monotonic() + 5
    while time.monotonic() < end and _answers(_controller(pid)["url"]):
        time.sleep(0.2)
    return start(pid, wait=wait)


def stop(pid):
    """Stop the console (its whole process group) and forget its pidfile."""
    project = _root(pid)
    path = Path(project["path"])
    ctrl = _controller(pid)
    with _lock:
        st = status(ctrl, path)
        if st["pid"]:
            _terminate(st["pid"])
        (P.space_dir(path) / PIDFILE).unlink(missing_ok=True)
        return status(ctrl, path)


def log_tail(pid, n=50):
    """Last ``n`` lines of .devpilot/console.log ([] when there is no log)."""
    project = _root(pid)
    return _tail(P.space_dir(project["path"]) / LOGFILE, n)


def install_hint(ctrl):
    """How to launch the console by hand, in one line."""
    if not ctrl:
        return ""
    d, cmd = ctrl.get("dir") or ".", ctrl.get("command") or "./run.sh"
    run = cmd if d == "." else f"cd {shlex.quote(d)} && {cmd}"
    hint = f"Lancer a la main : {run}"
    if ctrl.get("url"):
        hint += f" (puis ouvrir {ctrl['url']})"
    return hint
