"""Sessions de développement : un terminal ou Claude DANS un dépôt, listés avec dépôt et branche."""
import importlib
import os
import time

import pytest

from conftest import sh


@pytest.fixture
def app(home):
    import sizes, project_routes, cloud_routes, dev, dashboard
    for m in (sizes, dev, project_routes, cloud_routes, dashboard):
        importlib.reload(m)
    dashboard.app.config["TESTING"] = True
    return dashboard


@pytest.fixture
def c(app):
    app.app.config["SERVER_NAME"] = "127.0.0.1:5555"
    return app.app.test_client()


@pytest.fixture
def projet(P, home, remote):
    p = P.create("plateforme")
    root = home / "devpilot" / "projects" / "plateforme"
    sh("git", "clone", "-q", remote, str(root / "md_infra"))
    sh("git", "clone", "-q", remote, str(root / "md_infra" / "services" / "md-backend"))
    sh("git", "switch", "-q", "-c", "fix/a", cwd=root / "md_infra" / "services" / "md-backend")
    return p["id"], root


@pytest.fixture
def faux_claude(tmp_path, monkeypatch):
    d = tmp_path / "bin"; d.mkdir()
    (d / "claude").write_text("#!/bin/sh\necho CLAUDE-DEMARRE-ICI $(pwd)\n"); (d / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")


def tue_tout(app):
    for s in list(app._term_sessions.values()):
        s.kill()
    app._term_sessions.clear()


def test_session_terminal_et_claude_dans_un_depot(app, c, projet, faux_claude):
    pid, root = projet
    try:
        r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md_infra/services/md-backend"}).get_json()
        assert r["success"] and r["cwd"] == str(root / "md_infra/services/md-backend") and r["label"] == "md-backend"
        cl = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md_infra/services/md-backend", "kind": "claude"}).get_json()
        assert cl["success"] and cl["kind"] == "claude" and cl["label"] == "Claude · md-backend"
        time.sleep(1.5)
        s = app._term_sessions[cl["sid"]]
        assert b"CLAUDE-DEMARRE-ICI " + str(root / "md_infra/services/md-backend").encode() in bytes(s.buffer)
        assert s.alive                                              # le shell reste après la fin de Claude
        lst = c.get(f"/api/terminal/sessions?pid={pid}").get_json()
        assert {(x["kind"], x["repo"], x["branch"]) for x in lst} == {("terminal", "md_infra/services/md-backend", "fix/a"),
                                                                      ("claude", "md_infra/services/md-backend", "fix/a")}
        assert all(x["project"] == "plateforme" for x in lst)
        assert c.delete(f"/api/terminal/sessions/{cl['sid']}").get_json()["killed"]
        assert len(c.get(f"/api/terminal/sessions?pid={pid}").get_json()) == 1
    finally:
        tue_tout(app)


def test_session_toutes_et_filtre(app, c, projet, P):
    pid, root = projet
    autre = P.create("autre")["id"]
    try:
        c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md_infra"})
        c.post("/api/terminal/sessions", json={"pid": autre})                       # racine (pas un dépôt) : permis
        tout = c.get("/api/terminal/sessions").get_json()
        assert {x["project"] for x in tout} == {"plateforme", "autre"}
        assert [x["project"] for x in c.get(f"/api/terminal/sessions?pid={autre}").get_json()] == ["autre"]
    finally:
        tue_tout(app)


def test_session_refusee_hors_projet_ou_sans_claude(app, c, projet, monkeypatch):
    pid, _ = projet
    r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "../.."})
    assert r.status_code == 400 and "hors du projet" in r.get_json()["error"]
    import db
    db.set_setting("claude_cmd", "claude-absent")
    r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "md_infra", "kind": "claude"})
    assert r.status_code == 400 and "Claude introuvable" in r.get_json()["error"]


def test_routes_dev(c, projet):
    pid, root = projet
    r = c.get(f"/api/projects/{pid}/dev/repos").get_json()
    assert r["success"] and {x["dir"] for x in r["repos"]} == {"md_infra", "md_infra/services/md-backend"} and "editor" in r["tools"]
    r = c.post(f"/api/projects/{pid}/dev/new-branch", json={"repo": "md_infra", "name": "feat/x"}).get_json()
    assert r["success"] and r["branch"] == "feat/x"
    (root / "md_infra" / "package.json").write_text("modifié")
    r = c.post(f"/api/projects/{pid}/dev/switch", json={"repo": "md_infra", "branch": "main"})
    assert r.status_code == 409 and "non commité" in r.get_json()["message"]
    assert c.get(f"/api/projects/{pid}/dev/branches?repo=md_infra").get_json()["current"] == "feat/x"
