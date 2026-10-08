"""HTTP level: the routes the UI calls, the dashboard guard, cleanup bounds."""
import importlib
import time

import pytest


@pytest.fixture
def app(home):
    import sizes, project_routes, cloud_routes, dashboard
    for m in (sizes, project_routes, cloud_routes, dashboard):         # sizes: cache in the test HOME
        importlib.reload(m)
    dashboard.app.config["TESTING"] = True
    return dashboard


@pytest.fixture
def c(app):
    app.app.config["SERVER_NAME"] = "127.0.0.1:5555"   # the test client's Host
    return app.app.test_client()


def wait_job(c, jid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").get_json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job still running")


# ── Guard ───────────────────────────────────────────────────────────────────

def test_guard_rejects_foreign_host_and_origin(c):
    assert c.get("/api/projects").status_code == 200
    assert c.get("/api/projects", headers={"Host": "evil.example:5555"}).status_code == 403
    assert c.post("/api/projects", json={"name": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/projects", json={"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/projects", json={"name": "ok"}, headers={"Origin": "http://localhost:5555"}).status_code == 200


# ── Add ─────────────────────────────────────────────────────────────────────

def test_create_list_states(c, home):
    r = c.post("/api/projects", json={"name": "site"}).get_json()
    assert r["success"] and r["path"] == str(home / "devpilot" / "projects" / "site")
    (home / "devpilot" / "projects" / "site").rename(home / "gone")
    lst = c.get("/api/projects").get_json()
    assert lst[0]["state"] == "missing" and lst[0]["managed"]


def test_create_errors_are_409_with_message(c):
    r = c.post("/api/projects", json={"name": "mon projet"})
    assert r.status_code == 409 and "Nom invalide" in r.get_json()["message"]
    r = c.post("/api/projects", json={})
    assert r.status_code == 409


def test_clone_job(c, home, remote):
    r = c.post("/api/projects/clone", json={"url": remote}).get_json()
    j = wait_job(c, r["job_id"])
    assert j["status"] == "done", j
    assert j["result"]["path"] == str(home / "devpilot" / "projects" / "hello")
    assert j["log"]


def test_clone_job_failure(c, tmp_path):
    r = c.post("/api/projects/clone", json={"url": f"file://{tmp_path}/nope.git"}).get_json()
    j = wait_job(c, r["job_id"])
    assert j["status"] == "error" and "Clone echoue" in j["error"]


def test_clone_conflict_answers_immediately(c, remote):
    c.post("/api/projects", json={"name": "hello"})
    r = c.post("/api/projects/clone", json={"url": remote})
    assert r.status_code == 409


def test_legacy_target_dir(c, home, remote):
    r = c.post("/api/projects/clone", json={"url": remote, "target_dir": "~/work"}).get_json()
    assert wait_job(c, r["job_id"])["result"]["path"] == str(home / "work" / "hello")


def test_adopt_and_unregistered(c, home):
    (home / "devpilot" / "projects" / "manual").mkdir(parents=True)
    assert c.get("/api/projects/unregistered").get_json()[0]["name"] == "manual"
    (home / "Desktop" / "app").mkdir(parents=True)
    r = c.post("/api/projects/adopt", json={"path": "~/Desktop/app"}).get_json()
    assert r["path"] == str(home / "devpilot" / "projects" / "app")
    r = c.post("/api/projects/from-path", json={"path": "~/devpilot/projects/manual"}).get_json()
    assert r["success"] and c.get("/api/projects/unregistered").get_json() == []


def test_import_prepare_finish(c, home):
    r = c.post("/api/projects/import/prepare", json={"name": "md"}).get_json()
    assert r["sid"] == "import-md"
    assert c.post("/api/projects/import/finish", json={"name": "md"}).status_code == 409   # empty
    (home / "devpilot" / "projects" / "md" / "md_console").mkdir()
    r = c.post("/api/projects/import/finish", json={"name": "md"}).get_json()
    assert r["created"]
    # prepare again on the same folder is allowed (import continues), finish updates
    assert c.post("/api/projects/import/prepare", json={"name": "md"}).get_json()["project_id"] == r["id"]
    assert c.post("/api/projects/import/finish", json={"name": "md"}).get_json()["created"] is False


# ── Change / remove ─────────────────────────────────────────────────────────

def test_put_metadata_rename_and_legacy_path(c, home):
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    c.put(f"/api/projects/{pid}", json={"name": "site2", "color": "#fff"})
    p = c.get("/api/projects").get_json()[0]
    assert (p["name"], p["color"], p["path"]) == ("site2", "#fff", str(home / "devpilot" / "projects" / "site"))
    r = c.put(f"/api/projects/{pid}", json={"path": "~/work/site", "path_mode": "move"}).get_json()
    assert r["project"]["path"] == str(home / "work" / "site")


def test_delete_default_and_files(c, home):
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    rep = c.get(f"/api/projects/{pid}/inspect").get_json()
    assert rep["exists"] and "Aucun depot git" in rep["risks"][0]
    assert c.delete(f"/api/projects/{pid}?delete_files=1&confirm=nope").status_code == 409
    c.delete(f"/api/projects/{pid}?delete_files=1&confirm=site")
    assert not (home / "devpilot" / "projects" / "site").exists()
    assert (home / ".local" / "share" / "Trash" / "files" / "site").is_dir()
    pid = c.post("/api/projects", json={"name": "keep"}).get_json()["id"]
    c.delete(f"/api/projects/{pid}")
    assert (home / "devpilot" / "projects" / "keep").is_dir()


def test_cleanup_bounds(c, home):
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    root = home / "devpilot" / "projects" / "site"
    (root / "node_modules").mkdir()
    outside = home / "precious"
    outside.mkdir()
    r = c.post(f"/api/projects/{pid}/cleanup", json={"items": [
        {"category": "cache", "path": str(root / "node_modules"), "name": "nm"},
        {"category": "cache", "path": str(outside), "name": "evil"},
        {"category": "file", "path": str(outside), "name": "evil2"},
        {"category": "docker", "detail": "container", "name": "-rf", "path": ""},
    ]}).get_json()
    assert not (root / "node_modules").exists()
    assert outside.is_dir() and len(r["errors"]) == 3
    assert c.post(f"/api/projects/{pid}/cleanup", json={"delete_project_dir": True}).status_code == 409
    assert root.is_dir()


def test_link_git_route(c, home, remote):
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    r = c.post(f"/api/projects/{pid}/link-git", json={"url": remote}).get_json()
    assert r["action"] == "cloned"
    assert (home / "devpilot" / "projects" / "site" / "package.json").exists()


def test_claude_md_never_overwrites_repo_file(c, app, home):
    pid = c.post("/api/projects", json={"name": "site", "git_init": True}).get_json()["id"]
    root = home / "devpilot" / "projects" / "site"
    (root / "CLAUDE.md").write_text("MINE")
    c.post(f"/api/projects/{pid}/init-devpilot")
    assert (root / "CLAUDE.md").read_text() == "MINE"
    assert (root / ".devpilot" / "CLAUDE.md").exists()
    (root / "CLAUDE.md").unlink()
    c.post(f"/api/projects/{pid}/init-devpilot")
    assert app.P.is_devpilot_file(root / "CLAUDE.md")
    excl = (root / ".git" / "info" / "exclude").read_text()
    assert ".devpilot/" in excl and "/CLAUDE.md" in excl


def test_workspace_writes_go_to_the_project_folder(c, home):
    import json
    pid = c.post("/api/projects", json={"name": "site", "color": "#abcdef"}).get_json()["id"]
    dp = home / "devpilot" / "projects" / "site" / ".devpilot"
    assert json.loads((dp / "project.json").read_text())["color"] == "#abcdef"
    c.post(f"/api/projects/{pid}/specs", json={"prompt": "P1", "checklist": [{"text": "a", "done": False}]})
    assert (dp / "prompt.md").read_text() == "P1"
    c.post(f"/api/projects/{pid}/checklist", json={"checklist": [{"text": "a", "done": True}]})
    assert json.loads((dp / "checklist.json").read_text())[0]["done"] is True
    c.post(f"/api/projects/{pid}/lifecycle", json={"status": "paused"})
    assert json.loads((dp / "project.json").read_text())["status"] == "paused"
    c.post(f"/api/projects/{pid}/sessions", json={"title": "jour1", "content": "fait"})
    assert list((dp / "sessions").glob("*.md"))
    st = c.get(f"/api/projects/{pid}/status").get_json()
    assert st["state"] == "ok" and st["repos"] == []


# ── Hosting: a server (SSH) or a platform (Vercel...) ───────────────────────

def _deploy_card(c, pid):
    conns = c.get(f"/api/projects/{pid}/connections").get_json()["connections"]
    return next((x for x in conns if x["type"] == "deploy"), None)


def test_platform_is_offered_when_a_site_has_no_hosting_yet(c):
    """Without this card the platform picker had no way in (only the SSH form)."""
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    c.put(f"/api/projects/{pid}/model", json={"profile": "vitrine"})
    card = _deploy_card(c, pid)
    assert card["status"] == "non_connecte" and "Vercel" in card["label"]
    assert [a["action_type"] for a in card["actions"]] == ["setup_deploy"]


def test_no_platform_card_once_a_server_is_linked_or_hosting_not_needed(c, home):
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    c.put(f"/api/projects/{pid}/model", json={"profile": "mobile"})      # no hosting need
    assert _deploy_card(c, pid) is None
    c.put(f"/api/projects/{pid}/model", json={"profile": "vitrine"})
    assert _deploy_card(c, pid) is not None
    r = c.post(f"/api/projects/{pid}/servers",
               json={"name": "VPS", "role": "prod", "provider": "ovh", "host": "203.0.113.10",
                     "user": "ubuntu", "skip_test": True})
    assert r.get_json()["success"]
    assert _deploy_card(c, pid) is None                                  # the server card covers it


def test_platform_name_is_normalised_and_drives_the_dns_records(c, home):
    import json
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    c.post(f"/api/projects/{pid}/connections",
           json={"type": "deploy", "config": {"platform": "https://Vercel.com/", "site_url": "https://site.vercel.app"}})
    card = _deploy_card(c, pid)
    assert card["details"]["plateforme"] == "vercel"
    assert card["status"] == "configured" and card["url"] == "https://site.vercel.app"   # no token needed
    c.post(f"/api/projects/{pid}/domain", json={"domain": "site.ma"})
    exp = c.get(f"/api/projects/{pid}/domain").get_json()["expected"]
    assert exp["target"] == {"kind": "platform", "platform": "vercel", "site_url": "https://site.vercel.app"}
    assert {(r["type"], r["name"]) for r in exp["records"]} >= {("A", "@"), ("CNAME", "www")}
    cj = json.loads((home / "devpilot" / "projects" / "site" / ".devpilot" / "connections.json").read_text())
    assert cj["deploy"]["platform"] == "vercel"


def test_vps_from_the_old_wizard_is_not_a_platform(c):
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    c.post(f"/api/projects/{pid}/specs", json={"wizard_data": {"frontend_hosting": "vps"}})
    card = _deploy_card(c, pid)
    assert card is None or card["actions"][0]["action_type"] == "setup_deploy"


def test_linking_a_hosting_account_is_mirrored_in_the_project_folder(c, home):
    import json
    pid = c.post("/api/projects", json={"name": "site"}).get_json()["id"]
    import db
    db.set_setting("hosting_accounts", json.dumps(
        [{"id": "a1", "platform": "vercel", "name": "Moi", "email": "moi@x.ma", "token": "t", "projects": []}]))
    r = c.post(f"/api/projects/{pid}/hosting", json={"account_id": "a1", "site_url": "https://site.vercel.app"})
    assert r.get_json()["platform"] == "vercel"
    cj = json.loads((home / "devpilot" / "projects" / "site" / ".devpilot" / "connections.json").read_text())
    assert cj["deploy"]["platform"] == "vercel" and cj["deploy"]["site_url"] == "https://site.vercel.app"
