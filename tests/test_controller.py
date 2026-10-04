"""A project's own console — against a fake console (python http.server behind a run.sh)."""
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import psutil
import pytest

RUN_SH = ('#!/bin/bash\ncd "$(dirname "$0")"\n'
          'exec python3 -m http.server $(python3 -c \'import json;print(json.load(open("config.json"))["port"])\')'
          ' --bind 127.0.0.1\n')


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def answers(url):
    try:
        with urllib.request.urlopen(url, timeout=1):
            return True
    except OSError:
        return False


def make_console(folder, port=None, script=RUN_SH):
    folder.mkdir(parents=True, exist_ok=True)
    if port:
        (folder / "config.json").write_text(json.dumps({"port": port, "name": "test"}))
    (folder / "run.sh").write_text(script)
    (folder / "run.sh").chmod(0o755)


def kill_group(os_pid):
    try:
        os.killpg(os.getpgid(os_pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@pytest.fixture
def C(home):
    import importlib, controller
    importlib.reload(controller)
    yield controller
    for os_pid, proc in list(controller._children.items()):      # never leave a stray http.server
        kill_group(os_pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    controller._children.clear()


@pytest.fixture
def site(P, C):
    """Project "site" with a fake console in <project>/console/ on a free port."""
    p = P.create("site")
    root, port = Path(p["path"]), free_port()
    make_console(root / "console", port)
    yield p["id"], root, port
    pidfile = root / ".devpilot" / "console.pid"
    if pidfile.exists():
        try:
            kill_group(int(pidfile.read_text()))
        except ValueError:
            pass


# ── detection ───────────────────────────────────────────────────────────────

def test_detect(C, site):
    pid, root, port = site
    found = C.detect(root)
    assert len(found) == 1
    c = found[0]
    assert (c["dir"], c["name"], c["command"], c["port"]) == ("console", "console", "./run.sh", port)
    assert c["url"] == f"http://127.0.0.1:{port}"
    assert C.detect(root / "nope") == []


def test_detect_console_first_and_skips(P, C):
    root = Path(P.create("tools")["path"])
    make_console(root / "api", script="#!/bin/bash\n")                # alphabetically before md_console
    (root / "api" / "run.sh").unlink()
    (root / "api" / "server.py").write_text("print('hi')\n")
    make_console(root / "md_console", 8800)
    make_console(root / ".hidden", 8801)
    make_console(root / "node_modules", 8802)
    make_console(root / "console" / "deep", 8803)                     # two levels down: not a candidate
    found = C.detect(root)
    assert [c["dir"] for c in found] == ["md_console", "api"]
    assert found[0]["url"] == "http://127.0.0.1:8800" and found[0]["name"] == "md_console"
    assert found[1]["command"] == "python3 server.py" and found[1]["url"] is None


def test_detect_root(P, C):
    root = Path(P.create("flat")["path"])
    (root / "server.py").write_text("")
    (root / "config.json").write_text('{"port": "9100"}')
    found = C.detect(root)
    assert found[0]["dir"] == "." and found[0]["name"] == "console"
    assert found[0]["command"] == "python3 server.py" and found[0]["port"] == 9100
    (root / "config.json").write_text('{"port": 0}')                   # invalid port -> no url
    assert C.detect(root)[0]["url"] is None


# ── get / status ────────────────────────────────────────────────────────────

def test_get_auto_not_running(C, site):
    pid, root, port = site
    c = C.get(pid)
    assert c["auto"] is True and c["manages"] == C.DEFAULT_MANAGES and c["dir"] == "console"
    assert c["url"] == f"http://127.0.0.1:{port}" and c["command"] == "./run.sh"
    assert c["status"] == {"running": False, "pid": None, "started_by_devpilot": False, "url": c["url"], "stale": False, "cwd": ""}
    assert C.log_tail(pid) == []


def test_get_none_without_console(P, C):
    p = P.create("empty")
    assert C.get(p["id"]) is None
    assert "Aucune console" in err(P, C.start, p["id"]).message
    assert "introuvable" in err(P, C.get, "nope").message


# ── start / log / stop ──────────────────────────────────────────────────────

def test_start_log_stop(P, C, site):
    pid, root, port = site
    url = f"http://127.0.0.1:{port}"
    st = C.start(pid)
    assert st["running"] is True and isinstance(st["pid"], int) and st["started_by_devpilot"] is True
    pidfile = root / ".devpilot" / "console.pid"
    assert pidfile.exists() and int(pidfile.read_text()) == st["pid"]
    assert psutil.Process(st["pid"]).cwd() == str(root / "console")

    again = C.start(pid)                                              # idempotent
    assert again["pid"] == st["pid"] and again["running"]
    assert C.get(pid)["status"] == st

    with urllib.request.urlopen(url + "/config.json", timeout=2) as r:
        assert json.load(r)["port"] == port
    assert wait_for(lambda: any("GET /config.json" in l for l in C.log_tail(pid)))
    assert any("devpilot : ./run.sh" in l for l in C.log_tail(pid, 200))
    assert len(C.log_tail(pid, 1)) == 1

    off = C.stop(pid)
    assert off["running"] is False and off["pid"] is None and off["started_by_devpilot"] is False
    assert not pidfile.exists()
    assert wait_for(lambda: not psutil.pid_exists(st["pid"]), 5)
    assert not answers(url)
    assert C.stop(pid)["running"] is False                            # stopping twice is fine


def test_status_finds_a_console_started_by_hand(P, C, site):
    pid, root, port = site
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(["./run.sh"], cwd=str(root / "console"), start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: answers(url))
        st = C.status(C.get(pid), root)
        assert st["running"] is True and st["pid"] == proc.pid and st["started_by_devpilot"] is False
        assert C.start(pid)["pid"] == proc.pid                        # nothing launched twice
        off = C.stop(pid)                                             # DevPilot can still stop it
        assert off["running"] is False
        assert proc.wait(timeout=5) != 0 and not answers(url)
    finally:
        kill_group(proc.pid)
        proc.wait(timeout=5)


def test_start_failure_reports_the_log(P, C, site):
    pid, root, port = site
    (root / "console" / "run.sh").write_text("#!/bin/bash\necho 'boom: port deja pris' >&2\nexit 3\n")
    e = err(P, C.start, pid)
    assert "arretee" in e.message and "boom: port deja pris" in e.message
    assert not (root / ".devpilot" / "console.pid").exists()
    assert C.get(pid)["status"]["pid"] is None


def test_start_timeout_kills_the_process(P, C, site):
    pid, root, port = site
    (root / "console" / "run.sh").write_text("#!/bin/bash\nexec sleep 30\n")
    t = time.time()
    e = err(P, C.start, pid, wait=1)
    assert "ne repond pas" in e.message and time.time() - t < 10
    assert not (root / ".devpilot" / "console.pid").exists()
    assert C._children == {} and C.get(pid)["status"]["pid"] is None


# ── declare / clear ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value,msg", [
    ("url", "http://evil.com:8800", "locale"), ("url", "https://127.0.0.1:8800", "locale"),
    ("url", "http://127.0.0.1.evil.com:8800", "locale"), ("url", "", "locale"),
    ("url", "http://127.0.0.1:99999", "locale"),
    ("dir", "../..", "dans le dossier du projet"), ("dir", "/etc", "dans le dossier du projet"),
    ("dir", "missing", "introuvable"),
    ("command", "", "Commande"), ("command", None, "Commande"), ("command", "a\nb", "Commande"),
    ("manages", "hosting", "liste"), ("manages", [1], "liste")])
def test_declare_validation(P, C, site, field, value, msg):
    pid, root, port = site
    cfg = {"name": "MD console", "dir": "console", "command": "./run.sh", "url": f"http://127.0.0.1:{port}",
           field: value}
    assert msg in err(P, C.declare, pid, cfg).message
    assert C.KEY not in P.read_manifest(root)


def test_declare_persists_and_clear(P, C, site, home):
    pid, root, port = site
    url = f"http://localhost:{free_port()}/"           # a free port: a real console may sit on 8800
    cfg = {"name": "MD console", "dir": "console/", "command": "./run.sh", "url": url,
           "manages": ["hosting", " deploy ", "hosting", ""]}
    c = C.declare(pid, cfg)
    assert c["auto"] is False and c["dir"] == "console" and c["manages"] == ["hosting", "deploy"]
    assert c["url"] == url and c["status"]["running"] is False
    saved = {"name": "MD console", "dir": "console", "command": "./run.sh", "url": url,
             "manages": ["hosting", "deploy"], "auto": False}
    mf = home / "devpilot" / "projects" / "site" / ".devpilot" / "project.json"
    assert json.loads(mf.read_text())["controller"] == saved
    assert P.write_space(pid) and json.loads(mf.read_text())["controller"] == saved     # survives a rewrite
    assert json.loads(mf.read_text())["name"] == "site"
    got = C.get(pid)
    got.pop("status")
    assert got == saved

    other = f"http://127.0.0.1:{free_port()}"
    assert C.declare(pid, {"dir": ".", "command": "python3 server.py", "url": other})["name"] == "console"
    assert C.declare(pid, {"dir": "console", "command": "./run.sh", "url": other})["manages"] == C.DEFAULT_MANAGES

    back = C.clear(pid)
    assert "controller" not in json.loads(mf.read_text())
    assert back["auto"] is True and back["url"] == f"http://127.0.0.1:{port}"
    assert C.clear(pid)["auto"] is True                               # clearing twice is fine


def test_declared_controller_is_used_to_start(P, C, site):
    pid, root, port = site
    port2 = free_port()
    make_console(root / "admin", port2)
    c = C.declare(pid, {"name": "admin", "dir": "admin", "command": "./run.sh", "url": f"http://127.0.0.1:{port2}"})
    st = C.start(pid)
    assert st["running"] and psutil.Process(st["pid"]).cwd() == str(root / "admin")
    assert not answers(f"http://127.0.0.1:{port}")
    assert C.stop(pid)["running"] is False


def test_install_hint(C):
    assert C.install_hint(None) == ""
    h = C.install_hint({"dir": "md_console", "command": "./run.sh", "url": "http://127.0.0.1:8800"})
    assert "cd md_console && ./run.sh" in h and "http://127.0.0.1:8800" in h
    assert "cd" not in C.install_hint({"dir": ".", "command": "python3 server.py"})
    assert "cd 'my dir' &&" in C.install_hint({"dir": "my dir", "command": "./run.sh"})


# ── stale instance, restart, code changed ───────────────────────────────────

def test_start_replaces_a_stale_instance_of_the_same_console(P, C, home, site):
    """An old instance holds the port but no longer answers: DevPilot replaces it instead of refusing."""
    import subprocess, socket, time, os, signal
    pid, d, port = site
    # a dead-end process bound to the port, living in the console dir (like a hung old server)
    hung = subprocess.Popen(["python3", "-c", f"import socket,time; s=socket.socket(); s.bind(('127.0.0.1',{port})); s.listen(1); time.sleep(300)"],
                            cwd=str(d / "console"), start_new_session=True)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close(); break
            except OSError:
                time.sleep(0.1)
        st = C.start(pid)
        assert st["running"] and st["pid"] != hung.pid and st["started_by_devpilot"]
        assert hung.poll() is not None                              # the stale one is gone
    finally:
        try:
            os.killpg(os.getpgid(hung.pid), signal.SIGKILL)
        except Exception:
            pass
        C.stop(pid)


def test_start_refuses_a_foreign_process_on_the_port(P, C, home, site):
    import subprocess, socket, time, os, signal
    pid, d, port = site
    foreign = subprocess.Popen(["python3", "-c", f"import socket,time; s=socket.socket(); s.bind(('127.0.0.1',{port})); s.listen(1); time.sleep(300)"],
                               cwd=str(home), start_new_session=True)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close(); break
            except OSError:
                time.sleep(0.1)
        with pytest.raises(P.ProjectError) as e:
            C.start(pid)
        assert "autre programme" in e.value.message and foreign.poll() is None
    finally:
        os.killpg(os.getpgid(foreign.pid), signal.SIGKILL)


def test_restart_loads_new_code_and_flags_stale(P, C, home, site):
    import time, os
    pid, d, port = site
    st = C.start(pid)
    first = st["pid"]
    assert C.get(pid)["status"]["stale"] is False
    time.sleep(1.1)
    (d / "console" / "server_change.py").write_text("# new code\n")
    os.utime(d / "console" / "server_change.py", None)
    assert C.get(pid)["status"]["stale"] is True
    st = C.restart(pid)
    assert st["running"] and st["pid"] != first and C.get(pid)["status"]["stale"] is False
    C.stop(pid)


def test_bouton_console_relance_une_ancienne_version(P, C, home, site):
    """Le bouton « Console » : à jour → ouverte telle quelle ; code changé depuis (git pull,
    fusion) → relancée avec le code actuel, sinon on ouvrait l'ancienne (ou une page cassée)."""
    import time, os
    pid, d, port = site
    st, relancee = C.open_fresh(pid)                     # arrêtée → lancée
    first = st["pid"]
    assert st["running"] and not relancee
    st, relancee = C.open_fresh(pid)                     # à jour → rien ne bouge
    assert st["pid"] == first and not relancee
    time.sleep(1.1)
    (d / "console" / "nouveau.py").write_text("# fusion\n"); os.utime(d / "console" / "nouveau.py", None)
    st, relancee = C.open_fresh(pid)                     # ancienne version → relancée
    assert relancee and st["running"] and st["pid"] != first and C.get(pid)["status"]["stale"] is False
    C.stop(pid)
