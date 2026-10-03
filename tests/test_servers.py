"""Servers of a project — real SSH against a throwaway sshd container (image devpilot-test-sshd)."""
import json
import os
import pty
import select
import shutil
import socket
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker needed for the sshd container")


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


@pytest.fixture
def S(home):
    import importlib, servers
    importlib.reload(servers)
    return servers


class Sshd:
    def __init__(self):
        self.port = free_port()
        self.name = f"devpilot-test-sshd-{self.port}"
        self.start()

    def start(self):
        subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, "-p", f"127.0.0.1:{self.port}:22",
                        "devpilot-test-sshd"], check=True, capture_output=True)
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5) as c:
                    if c.recv(4).startswith(b"SSH"):
                        return
            except OSError:
                time.sleep(0.2)

    def authorize(self, pubkey):
        subprocess.run(["docker", "exec", self.name, "sh", "-c",
                        f"echo '{pubkey}' >> /home/dev/.ssh/authorized_keys && chown -R dev:dev /home/dev/.ssh "
                        "&& chmod 700 /home/dev/.ssh && chmod 600 /home/dev/.ssh/authorized_keys"], check=True)

    def reinstall(self):
        """New host keys, like a reinstalled server."""
        subprocess.run(["docker", "exec", self.name, "sh", "-c",
                        "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A >/dev/null && kill -HUP 1"], check=True)
        time.sleep(1)

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


@pytest.fixture
def sshd():
    d = Sshd()
    yield d
    d.stop()


@pytest.fixture
def linked(P, S, home, sshd):
    """A project + a key authorized on the test server."""
    p = P.create("site")
    k = S.generate_key(p["id"], "prod")
    sshd.authorize(k["public"])
    cfg = {"name": "VPS prod", "role": "prod", "provider": "ovh", "host": "127.0.0.1", "user": "dev",
           "port": sshd.port, "key_path": k["path"], "remote_path": "~/app", "urls": "https://site.example"}
    return p["id"], cfg, k


# ── validation / keys / ~/.ssh/config ───────────────────────────────────────

@pytest.mark.parametrize("field,value,msg", [
    ("host", "-oProxyCommand=touch /tmp/x", "Adresse invalide"), ("host", "a b", "Adresse invalide"),
    ("user", "root;id", "Utilisateur"), ("port", "99999", "Port"), ("name", "", "nom"),
    ("key_path", "/etc/passwd", "Cle introuvable")])
def test_validation(P, S, field, value, msg):
    cfg = {"name": "x", "host": "1.2.3.4", "user": "ubuntu", "port": 22, field: value}
    assert msg in err(P, S._clean, cfg).message


def test_generate_key(P, S, home):
    p = P.create("site")
    k = S.generate_key(p["id"], "prod")
    assert k["path"].startswith(str(home / ".ssh" / "devpilot")) and k["public"].startswith("ssh-ed25519 ")
    assert oct(os.stat(k["path"]).st_mode)[-3:] == "600"
    assert S.public_key(k["path"]) == k["public"]
    assert S.generate_key(p["id"], "prod")["path"].endswith("-2")          # never overwrites a key
    assert any(x["path"] == k["path"] for x in S.ssh_keys())


def test_ssh_config_hosts(S, home):
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh" / "config").write_text(
        "Host md-vps 203.0.113.10\n  HostName 203.0.113.10\n  User ubuntu\n  IdentityFile ~/.ssh/md/id_machine\n"
        "Host github.com\n  IdentityFile ~/.ssh/x\nHost *\n  ServerAliveInterval 30\n"
        "Host gpu\n  HostName 149.202.57.21\n  Port 2222\n")
    hosts = {h["alias"]: h for h in S.ssh_config_hosts()}
    assert set(hosts) == {"md-vps", "gpu"}
    assert (hosts["md-vps"]["host"], hosts["md-vps"]["user"]) == ("203.0.113.10", "ubuntu")
    assert hosts["md-vps"]["key_path"].endswith(".ssh/md/id_machine") and hosts["gpu"]["port"] == 2222


# ── real SSH ────────────────────────────────────────────────────────────────

def test_connection_ok(P, S, linked, sshd):
    pid, cfg, _ = linked
    subprocess.run(["docker", "exec", sshd.name, "mkdir", "-p", "/home/dev/app"], check=True)
    t = S.test_connection(S._clean(cfg))
    assert t["ok"] and t["hostname"] and "Alpine" in t["os"] and t["path_exists"] is True
    t = S.test_connection(S._clean({**cfg, "remote_path": "~/nope"}))
    assert t["ok"] and t["path_exists"] is False


def test_connection_key_refused(P, S, home, sshd):
    p = P.create("other")
    k = S.generate_key(p["id"], "not-authorized")
    t = S.test_connection(S._clean({"name": "x", "host": "127.0.0.1", "user": "dev", "port": sshd.port, "key_path": k["path"]}))
    assert not t["ok"] and "cle n'est pas autorisee" in t["error"]


def test_connection_unreachable(S):
    t = S.test_connection(S._clean({"name": "x", "host": "127.0.0.1", "user": "dev", "port": free_port()}))
    assert not t["ok"] and "refusee sur le port" in t["error"]


def test_server_identity_change_is_refused(P, S, linked, sshd):
    pid, cfg, k = linked
    assert S.test_connection(S._clean(cfg))["ok"]           # first connection records the identity
    sshd.reinstall()                                          # new host keys
    t = S.test_connection(S._clean(cfg))
    assert not t["ok"] and "a change" in t["error"]


def test_add_update_history_restore_remove(P, S, linked, home):
    pid, cfg, _ = linked
    s = S.add_server(pid, cfg)
    assert s["test"]["ok"]
    m = json.loads((home / "devpilot" / "projects" / "site" / ".devpilot" / "project.json").read_text())
    assert m["servers"][0]["host"] == "127.0.0.1" and "PRIVATE" not in json.dumps(m)
    assert m["servers"][0]["dashboard_url"] == "https://www.ovh.com/manager/"
    err(P, S.add_server, pid, cfg, test=False)                                    # same name
    s2 = S.update_server(pid, s["id"], {"host": "10.0.0.9", "provider": "hetzner"}, test=False)
    assert (s2["host"], s2["history"][0]["config"]["host"]) == ("10.0.0.9", "127.0.0.1")
    s3 = S.restore_server(pid, s["id"], 0)
    assert s3["host"] == "127.0.0.1" and s3["test"]["ok"]
    import db
    assert db.get_project_component(pid, "server")["config"]["servers"][0]["name"] == "VPS prod"
    S.remove_server(pid, s["id"])
    assert S.list_servers(pid) == []


def test_several_servers_with_roles(P, S, linked):
    pid, cfg, _ = linked
    S.add_server(pid, cfg, test=False)
    S.add_server(pid, {**cfg, "name": "GPU", "role": "gpu", "host": "149.202.57.21"}, test=False)
    assert [(s["name"], s["role"]) for s in S.list_servers(pid)] == [("VPS prod", "prod"), ("GPU", "gpu")]


def test_terminal_lands_in_project_folder(P, S, linked, sshd):
    pid, cfg, _ = linked
    subprocess.run(["docker", "exec", sshd.name, "mkdir", "-p", "/home/dev/app"], check=True)
    s = S.add_server(pid, cfg)
    argv, _ = S.terminal_argv(pid, s["id"])
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    out, end = b"", time.time() + 15
    os.write(master, b"echo WHERE=$PWD; exit\n")
    while time.time() < end and proc.poll() is None:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                out += os.read(master, 4096)
            except OSError:
                break
    proc.wait(timeout=10)
    assert b"WHERE=/home/dev/app" in out, out[-500:]
