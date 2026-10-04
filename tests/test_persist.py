"""Sessions persistantes : elles survivent à un redémarrage de DevPilot (tmux), et
« Fermer » / « Fermer le projet » les arrêtent pour de bon."""
import importlib
import os
import shutil
import socket
import subprocess
import time

import pytest

from conftest import sh

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux absent")


def port_libre():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def ouvert(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close(); return True
    except OSError:
        return False


def attendre(cond, delai=10):
    fin = time.time() + delai
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.2)
    return False


def charger():
    import sizes, persist, dev, project_routes, cloud_routes, ports, controller, dashboard
    for m in (sizes, persist, dev, ports, controller, project_routes, cloud_routes, dashboard):
        importlib.reload(m)
    dashboard.app.config["TESTING"] = True
    dashboard.app.config["SERVER_NAME"] = "127.0.0.1:5555"
    return dashboard


@pytest.fixture
def tmux_propre(home):
    yield
    subprocess.run(["tmux", "-L", os.environ["DEVPILOT_TMUX_SOCKET"], "kill-server"], capture_output=True)


@pytest.fixture
def projet(P, home, remote, tmux_propre):
    p = P.create("plateforme")
    root = home / "devpilot" / "projects" / "plateforme"
    sh("git", "clone", "-q", remote, str(root / "md-backend"))
    return p["id"], root


def texte(sess):
    return bytes(sess.buffer).decode("utf-8", "replace")


def test_une_session_vit_dans_tmux(projet):
    pid, root = projet
    app = charger(); c = app.app.test_client()
    import persist
    r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md-backend", "label": "travail"}).get_json()
    assert r["success"] and persist.exists(r["sid"])
    l = c.get(f"/api/terminal/sessions?pid={pid}").get_json()
    assert l[0]["persistent"] and l[0]["label"] == "travail" and l[0]["repo"] == "md-backend"
    assert {x["sid"] for x in persist.list_sessions()} == {r["sid"]}
    assert c.delete(f"/api/terminal/sessions/{r['sid']}").get_json()["killed"]
    assert attendre(lambda: not persist.exists(r["sid"]))                    # « Fermer » : pour de bon


def test_survit_au_redemarrage_de_devpilot(projet):
    pid, root = projet
    app = charger(); c = app.app.test_client()
    import persist
    port = port_libre()
    r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md-backend", "label": "serveur"}).get_json()
    sid = r["sid"]
    sess = app._term_sessions[sid]
    time.sleep(1)
    sess.write(f"python3 -m http.server {port} --bind 127.0.0.1\r".encode())
    assert attendre(lambda: ouvert(port)), texte(sess)[-300:]

    app._stop_everything_on_exit()                                          # DevPilot s'arrête (SIGTERM)
    assert attendre(lambda: not sess.alive)                                 # notre client est parti…
    assert persist.exists(sid) and ouvert(port)                             # …la session et le serveur restent

    app2 = charger(); c2 = app2.app.test_client()                           # DevPilot redémarre
    assert sid in app2._term_sessions
    l = c2.get(f"/api/terminal/sessions?pid={pid}").get_json()
    assert [(x["sid"], x["label"], x["repo"], x["persistent"]) for x in l] == [(sid, "serveur", "md-backend", True)]
    neuf = app2._term_sessions[sid]
    time.sleep(1)
    assert ouvert(port)                                                     # rien n'a été interrompu

    import ports
    ports.close_project(pid)                                                # « Fermer le projet »
    assert attendre(lambda: not ouvert(port)) and attendre(lambda: not persist.exists(sid))
    assert not neuf.alive or attendre(lambda: not neuf.alive)


def test_sortir_du_shell_termine_la_session(projet):
    pid, root = projet
    app = charger(); c = app.app.test_client()
    import persist
    sid = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md-backend"}).get_json()["sid"]
    sess = app._term_sessions[sid]
    time.sleep(1)
    sess.write(b"exit\r")
    assert attendre(lambda: not persist.exists(sid)) and attendre(lambda: sid not in app._term_sessions)


def test_sans_tmux_comme_avant(projet, monkeypatch):
    pid, root = projet
    app = charger(); c = app.app.test_client()
    import persist
    monkeypatch.setattr(persist, "available", lambda: False)
    r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md-backend"}).get_json()
    try:
        assert r["success"] and not app._term_sessions[r["sid"]].persistent and not persist.exists(r["sid"])
    finally:
        app._term_sessions[r["sid"]].kill()


def test_reponses_du_terminal_pas_tapees_dans_le_shell(projet):
    # tmux interroge le terminal du navigateur à l'attachement ; ses réponses (Device Attributes)
    # ne doivent pas arriver dans le shell comme du texte tapé
    pid, root = projet
    app = charger(); c = app.app.test_client()
    import persist
    sid = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md-backend"}).get_json()["sid"]
    sess = app._term_sessions[sid]
    time.sleep(1)
    sess.write(b"\x1b[?1;2c\x1b[>0;276;0cecho OK-$((40+2))\r")
    def ecran():
        return subprocess.run(["tmux", "-L", os.environ["DEVPILOT_TMUX_SOCKET"], "capture-pane", "-p", "-t",
                               "=" + persist.name(sid) + ":"], capture_output=True, text=True).stdout
    assert attendre(lambda: "OK-42" in ecran()), ecran()
    assert "1;2c" not in ecran() and "not found" not in ecran()
    sess.kill()


def test_creee_a_la_taille_du_terminal_du_navigateur(projet):
    # sinon tmux re-découpe l'écran à l'attachement et le début des longues lignes part dans l'historique
    pid, root = projet
    app = charger(); c = app.app.test_client()
    import persist
    sid = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md-backend", "rows": 33, "cols": 77}).get_json()["sid"]
    taille = subprocess.run(["tmux", "-L", os.environ["DEVPILOT_TMUX_SOCKET"], "display-message", "-p", "-t",
                             "=" + persist.name(sid) + ":", "#{window_height}x#{window_width}"], capture_output=True, text=True).stdout.strip()
    app._term_sessions[sid].kill()
    assert taille == "33x77"
    assert app._term_size("abc", None) == (40, 120) and app._term_size(1, 99999) == (5, 1000)
