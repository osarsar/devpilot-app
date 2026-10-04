"""What a project runs: ports found, stopped cleanly, project closed, nothing left behind."""
import os
import signal
import socket
import subprocess
import time

import pytest


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def wait_port(port, up=True, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port) == up:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def PO(home):
    import importlib, controller, ports
    importlib.reload(controller)
    importlib.reload(ports)
    ports.session_pids = lambda project: []
    ports.close_sessions = lambda project: 0
    return ports


@pytest.fixture
def procs():
    started = []
    yield started
    for p in started:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def serve(cwd, port, procs):
    p = subprocess.Popen(["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=cwd,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    procs.append(p)
    assert wait_port(port), "server did not start"
    return p


def test_ports_of_the_project_folder_and_not_others(P, PO, home, procs):
    p = P.create("site")
    other = P.create("other")
    port1, port2 = free_port(), free_port()
    serve(home / "devpilot" / "projects" / "site", port1, procs)
    serve(home / "devpilot" / "projects" / "other", port2, procs)
    r = PO.project_ports(p["id"])
    ports = {x["port"]: x for x in r["ports"]}
    assert port1 in ports and port2 not in ports
    assert ports[port1]["source"] == "dossier" and ports[port1]["name"].startswith("python")


def test_stop_port_frees_it_and_refuses_foreign_ports(P, PO, home, procs):
    p = P.create("site")
    port = free_port()
    serve(home / "devpilot" / "projects" / "site", port, procs)
    r = PO.stop_port(p["id"], port)
    assert r["freed"] and wait_port(port, up=False)
    foreign = free_port()
    serve(home, foreign, procs)                                   # not in the project
    e = pytest.raises(P.ProjectError, PO.stop_port, p["id"], foreign)
    assert "n'appartient pas" in e.value.message and port_open(foreign)
    assert PO.stop_port(p["id"], free_port())["freed"]            # already free: fine


def test_kill_tree_reaches_children_in_their_own_process_group(PO, procs, tmp_path):
    # bash → setsid child (its own group, like a dev server with job control)
    flag = tmp_path / "alive"
    p = subprocess.Popen(["bash", "-c", f"setsid sleep 300 & echo $! > {flag}; wait"], start_new_session=True)
    procs.append(p)
    for _ in range(50):
        if flag.exists() and flag.read_text().strip():
            break
        time.sleep(0.1)
    child = int(flag.read_text().strip())
    assert os.getpgid(child) != os.getpgid(p.pid)
    PO.kill_tree(p.pid)
    time.sleep(0.5)
    assert p.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_terminal_sessions_count_as_project_processes(P, PO, home, procs):
    p = P.create("site")
    port = free_port()
    # a "terminal": bash that launches a server (like typing `python -m http.server` in DevPilot)
    bash = subprocess.Popen(["bash", "-c", f"python3 -m http.server {port} --bind 127.0.0.1 & wait"],
                            cwd=str(home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    procs.append(bash)
    assert wait_port(port)
    PO.session_pids = lambda project: [bash.pid] if project["id"] == p["id"] else []
    ports = {x["port"]: x for x in PO.project_ports(p["id"])["ports"]}
    assert port in ports and ports[port]["source"] == "terminal"       # cwd is NOT the project: known via the terminal
    closed = []
    PO.close_sessions = lambda project: (closed.append(project["id"]), PO.kill_tree(bash.pid), 1)[2]
    r = PO.close_project(p["id"])
    assert closed == [p["id"]] and wait_port(port, up=False) and port in r["freed"]


def test_close_project_stops_the_console_and_folder_processes(P, PO, home, procs):
    import controller
    p = P.create("site")
    d = home / "devpilot" / "projects" / "site"
    cport = free_port()
    (d / "console").mkdir()
    (d / "console" / "config.json").write_text('{"port": %d}' % cport)
    (d / "console" / "run.sh").write_text('#!/bin/bash\ncd "$(dirname "$0")"\nexec python3 -m http.server %d --bind 127.0.0.1\n' % cport)
    (d / "console" / "run.sh").chmod(0o755)
    st = controller.start(p["id"])
    assert st["running"]
    fport = free_port()
    serve(d, fport, procs)
    r = PO.project_ports(p["id"])
    assert {x["source"] for x in r["ports"]} == {"console", "dossier"}
    r = PO.close_project(p["id"])
    assert wait_port(cport, up=False) and wait_port(fport, up=False)
    assert r["still"] == [] and "console" in r["message"]
    assert not (d / ".devpilot" / "console.pid").exists()


def test_close_project_with_nothing_running(P, PO, home):
    p = P.create("site")
    r = PO.close_project(p["id"])
    assert r["log"] == [] and "Rien ne tournait" in r["message"]
