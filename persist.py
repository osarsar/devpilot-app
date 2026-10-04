"""DevPilot — sessions that survive a DevPilot restart (tmux underneath).

Each persistent session (a project's terminal, the « Développer » sessions:
terminal or Claude) runs inside its OWN tmux session, on a tmux server that
belongs to DevPilot only (socket « devpilot », no user config). DevPilot
just attaches to it through a PTY. When DevPilot stops or restarts, tmux
keeps running: Claude, a dev server, a build — nothing is lost; DevPilot
re-attaches at startup.

Without tmux, nothing changes: sessions live as long as DevPilot (and the UI
says how to get persistence: sudo apt install tmux).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

PREFIX = "dp_"


def socket_name():
    """DevPilot's own tmux server (read each time: the tests use their own)."""
    return os.environ.get("DEVPILOT_TMUX_SOCKET", "devpilot")


def available():
    return bool(shutil.which("tmux"))


def _t(*args, timeout=10, inp=None):
    try:
        r = subprocess.run(["tmux", "-L", socket_name(), "-f", "/dev/null", *args], capture_output=True, text=True,
                           timeout=timeout, input=inp)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def name(sid):
    return PREFIX + re.sub(r"[^A-Za-z0-9_-]", "_", sid)


def exists(sid):
    return _t("has-session", "-t", "=" + name(sid))[0] == 0


def _server_options():
    # the look of a plain terminal: no status bar; the wheel scrolls tmux's history
    for opt in (("status", "off"), ("mouse", "on"), ("history-limit", "50000"), ("escape-time", "10"),
                ("default-terminal", "xterm-256color"), ("set-titles", "off")):
        _t("set-option", "-g", *opt)


def create(sid, cwd, argv, meta, env=None, rows=40, cols=120):
    """rows × cols : la taille du PTY de DevPilot au départ (sinon tmux re-découpe l'écran à l'attachement)."""
    args = ["new-session", "-d", "-s", name(sid), "-x", str(cols), "-y", str(rows), "-c", cwd]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    rc, _, err = _t(*args, "--", *argv)
    if rc != 0:
        raise RuntimeError("tmux : " + err)
    _server_options()
    # « =nom: » : set-option refuse « =nom » (vérifié avec tmux 3.4)
    rc, _, err = _t("set-option", "-t", "=" + name(sid) + ":", "@dp_meta", json.dumps({**meta, "sid": sid}, ensure_ascii=False))
    if rc != 0:
        raise RuntimeError("tmux : " + err)
    return True


def attach_argv(sid):
    return ["tmux", "-L", socket_name(), "-f", "/dev/null", "attach-session", "-t", "=" + name(sid)]


def list_sessions():
    """[{sid, created, meta, cwd}] — what survives in tmux (also after a DevPilot restart)."""
    rc, out, _ = _t("list-sessions", "-F", "#{session_name}\t#{session_created}\t#{pane_current_path}\t#{@dp_meta}")
    res = []
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split("\t", 3)
        if len(parts) < 4 or not parts[0].startswith(PREFIX):
            continue
        try:
            meta = json.loads(parts[3]) if parts[3] else {}
        except ValueError:
            meta = {}
        res.append({"sid": meta.get("sid") or parts[0][len(PREFIX):], "created": float(parts[1] or 0),
                    "cwd": parts[2], "meta": meta})
    return res


def pane_pids(sid):
    rc, out, _ = _t("list-panes", "-s", "-t", "=" + name(sid), "-F", "#{pane_pid}")
    return [int(x) for x in out.split() if x.isdigit()] if rc == 0 else []


def current_path(sid):
    rc, out, _ = _t("display-message", "-p", "-t", "=" + name(sid) + ":", "#{pane_current_path}")
    return out if rc == 0 else ""


def kill(sid, kill_tree=None):
    """End the session for good: what runs in it (dev servers…), then the session itself."""
    if kill_tree:
        for pid in pane_pids(sid):
            try:
                import psutil
                for child in psutil.Process(pid).children(recursive=True):
                    kill_tree(child.pid, grace=3)
            except Exception:
                pass
    _t("kill-session", "-t", "=" + name(sid))
