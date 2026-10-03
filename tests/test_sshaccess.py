"""Server access with the provider's password: expired password, new password, key, new PC.
Real SSH against a throwaway sshd with PAM (image devpilot-test-sshd-pw: user ubuntu / Ovh-Initial-123)."""
import shutil
import socket
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker needed")

OVH_PW = "Ovh-Initial-123"
MY_PW = "Mon-Mot-De-Passe-2026!"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Box:
    def __init__(self):
        self.port = free_port()
        self.name = f"devpilot-test-pw-{self.port}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, "-p", f"127.0.0.1:{self.port}:22",
                        "devpilot-test-sshd-pw"], check=True, capture_output=True)
        for _ in range(60):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5) as c:
                    if c.recv(4).startswith(b"SSH"):
                        return
            except OSError:
                time.sleep(0.2)

    def sh(self, cmd):
        return subprocess.run(["docker", "exec", self.name, "sh", "-c", cmd], capture_output=True, text=True)

    def expire(self):
        self.sh("chage -d 0 ubuntu")

    def no_password_login(self):
        """like an OVH image whose cloud-init disables passwords"""
        self.sh("printf 'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\n' > /etc/ssh/sshd_config.d/50-cloud-init.conf"
                " && rm -f /etc/ssh/sshd_config.d/10-test.conf && printf 'UsePAM yes\\n' > /etc/ssh/sshd_config.d/10-test.conf"
                " && kill -HUP 1")
        time.sleep(1)

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


@pytest.fixture
def box():
    b = Box()
    yield b
    b.stop()


@pytest.fixture
def SA(home):
    import importlib, servers, sshaccess
    importlib.reload(servers)
    importlib.reload(sshaccess)
    return sshaccess


@pytest.fixture
def srv(P, SA, home, box):
    import servers
    p = P.create("client")
    s = servers.add_server(p["id"], {"name": "VPS OVH", "role": "prod", "provider": "ovh", "host": "127.0.0.1",
                                     "user": "ubuntu", "port": box.port}, test=False)
    return p["id"], s["id"]


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


def can_login(port, pw):
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect("127.0.0.1", port=port, username="ubuntu", password=pw, allow_agent=False, look_for_keys=False, timeout=10)
        i, o, e = c.exec_command("true")
        return o.channel.recv_exit_status() == 0
    except Exception:
        return False
    finally:
        c.close()


def test_expired_password_asks_for_a_new_one(P, SA, box, srv):
    box.expire()
    pid, sid = srv
    r = SA.setup_access(pid, sid, OVH_PW)
    assert r.get("needs_new") is True


def test_expired_password_changed_key_installed(P, SA, box, srv):
    box.expire()
    pid, sid = srv
    r = SA.setup_access(pid, sid, OVH_PW, new=MY_PW)
    assert r["ok"] and r["password_changed"] and r["server"]["test"]["ok"]
    assert can_login(box.port, MY_PW) and not can_login(box.port, OVH_PW)
    assert any("expire" in l for l in r["log"])
    assert "never" in box.sh("chage -l ubuntu").stdout.lower()


def test_normal_password_changed_with_passwd(P, SA, box, srv):
    pid, sid = srv
    r = SA.setup_access(pid, sid, OVH_PW, new=MY_PW)
    assert r["ok"] and can_login(box.port, MY_PW) and not can_login(box.port, OVH_PW)


def test_key_only_without_changing_the_password(P, SA, box, srv):
    pid, sid = srv
    r = SA.setup_access(pid, sid, OVH_PW)
    assert r["ok"] and not r["password_changed"] and can_login(box.port, OVH_PW)


@pytest.mark.parametrize("bad", ["court", "aaaaaaaaaaaaaa", OVH_PW])
def test_weak_new_password_refused_before_touching_the_server(P, SA, box, srv, bad):
    pid, sid = srv
    err(P, SA.setup_access, pid, sid, OVH_PW, new=bad)
    assert can_login(box.port, OVH_PW)


def test_wrong_password(P, SA, box, srv):
    pid, sid = srv
    e = err(P, SA.setup_access, pid, sid, "pas-le-bon")
    assert "Mot de passe incorrect" in e.message


def test_root_password_too(P, SA, box, srv):
    pid, sid = srv
    before = box.sh("grep '^root:' /etc/shadow").stdout
    SA.setup_access(pid, sid, OVH_PW, new=MY_PW, also_root=True)
    after = box.sh("grep '^root:' /etc/shadow").stdout
    assert before != after and after.split(":")[1] not in ("", "*", "!")


def test_password_login_reenabled_then_a_new_pc_adds_its_key(P, SA, box, srv, home):
    import servers
    pid, sid = srv
    # first access while passwords still work, then the image disables them (cloud-init style)
    SA.setup_access(pid, sid, OVH_PW, new=MY_PW)
    box.no_password_login()
    assert can_login(box.port, MY_PW)                      # the recorded choice (00-devpilot.conf) wins
    box.sh("rm -f /etc/ssh/sshd_config.d/00-devpilot.conf && kill -HUP 1")   # choice file lost → passwords off
    time.sleep(1)
    assert not can_login(box.port, MY_PW)
    # DevPilot (with the key) re-enables password logins
    c = SA.connect("127.0.0.1", box.port, "ubuntu", key_path=servers.list_servers(pid)[0]["key_path"])
    try:
        assert SA.password_login_state(c, MY_PW)["password_login"] is False
        SA.enable_password_login(c, MY_PW)
    finally:
        c.close()
    time.sleep(1)
    assert can_login(box.port, MY_PW)
    # "new PC": another key, only the password known
    newkey = servers.generate_key(pid, "nouveau-pc")
    servers.update_server(pid, sid, {"key_path": newkey["path"]}, test=False)
    r = SA.setup_access(pid, sid, MY_PW)
    assert r["ok"] and r["server"]["test"]["ok"]
    keys = box.sh("cat /home/ubuntu/.ssh/authorized_keys").stdout
    assert newkey["public"].split()[1] in keys and keys.count("ssh-ed25519") == 2


def test_password_choice_recorded_even_when_already_allowed(P, SA, box, srv):
    """Passwords already accepted (cloud-init): the choice is still WRITTEN, so a later
    « PasswordAuthentication no » (hardening, cloud-init) does not silently cut it."""
    pid, sid = srv
    SA.setup_access(pid, sid, OVH_PW)
    assert "PasswordAuthentication yes" in box.sh("cat /etc/ssh/sshd_config.d/00-devpilot.conf").stdout
    box.no_password_login()
    assert can_login(box.port, OVH_PW)


def test_password_login_state_seen_through_the_key(P, SA, box, srv):
    """Key-only server: DevPilot sees (with its key) that passwords are refused."""
    pid, sid = srv
    SA.setup_access(pid, sid, OVH_PW, allow_password_login=False)
    box.no_password_login()
    import servers
    srv_ = servers.list_servers(pid)[0]
    c = SA.connect("127.0.0.1", box.port, "ubuntu", key_path=srv_["key_path"])
    try:
        state = SA.password_login_state(c, None)
    finally:
        c.close()
    assert state["known"] and state["password_login"] is False
