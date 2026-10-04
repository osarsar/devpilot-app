"""Project model: profile → needs → state of each need; delegation to a controller."""
import json

import pytest


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


@pytest.fixture
def M(home):
    import importlib, model
    importlib.reload(model)
    return model


def test_profiles_cover_the_business_cases(M):
    assert {"vitrine", "site_backend", "site_complet", "webapp_saas", "enterprise", "custom"} <= set(M.PROFILES)
    for p in M.PROFILES.values():
        assert set(p["needs"]) <= set(M.NEEDS)
    assert M.PROFILES["vitrine"]["needs"] == ["github", "hosting", "domain", "email"]
    assert "database" in M.PROFILES["site_backend"]["needs"] and "storage" in M.PROFILES["site_complet"]["needs"]
    assert "controller" in M.PROFILES["enterprise"]["needs"]


def test_new_project_has_no_profile(P, M, home):
    p = P.create("site")
    m = M.get_model(p["id"])
    assert m["profile"] == "" and m["needs"] == [] and m["controller"] is None


def test_set_profile_gives_default_needs_and_lives_in_the_folder(P, M, home):
    p = P.create("site")
    m = M.set_model(p["id"], profile="vitrine")
    assert m["needs"] == ["github", "hosting", "domain", "email"]
    manifest = json.loads((home / "devpilot" / "projects" / "site" / ".devpilot" / "project.json").read_text())
    assert manifest["profile"] == "vitrine" and "needs" not in manifest       # defaults: not frozen in the file
    import db
    assert db.get_project(p["id"])["profile"] == "vitrine"


def test_custom_needs(P, M, home):
    p = P.create("site")
    M.set_model(p["id"], profile="vitrine")
    m = M.set_model(p["id"], needs=["github", "hosting", "domain", "email", "storage", "storage"])
    assert m["needs"] == ["github", "hosting", "domain", "email", "storage"]
    manifest = json.loads((home / "devpilot" / "projects" / "site" / ".devpilot" / "project.json").read_text())
    assert manifest["needs"] == m["needs"]
    assert "storage" in M.set_model(p["id"], profile="site_backend", needs=["github", "storage"])["needs"]
    assert M.set_model(p["id"], profile="site_backend")["needs"] == M.PROFILES["site_backend"]["needs"]  # reset
    err(P, M.set_model, p["id"], profile="nope")
    err(P, M.set_model, p["id"], needs=["github", "laser"])


def test_model_survives_reimport(P, M, home):
    p = P.create("site")
    M.set_model(p["id"], profile="site_complet", needs=["github", "hosting"])
    P.remove(p["id"])
    q = P.adopt(home / "devpilot" / "projects" / "site")
    m = M.get_model(q["id"])
    assert (m["profile"], m["needs"]) == ("site_complet", ["github", "hosting"])


def test_connections_status_follows_reality(P, M, home, remote):
    import db
    p = P.create("site")
    M.set_model(p["id"], profile="site_backend")
    st = M.connections_status(p["id"])
    assert st["total"] == 5 and st["done"] == 0
    assert [x["need"] for x in st["missing"]] == ["github", "hosting", "domain", "email", "database"]
    P.link_git(p["id"], remote)
    db.add_project_component(p["id"], "domain", enabled=True, config={"domain": "client.ma"})
    db.add_project_component(p["id"], "email", enabled=True, config={"provider": "google", "mx_verified": True})
    db.add_project_component(p["id"], "database", enabled=True, config={"type": "postgres", "host": "db"})
    import servers
    servers.add_server(p["id"], {"name": "VPS", "host": "1.2.3.4", "user": "ubuntu"}, test=False)
    st = {x["need"]: x for x in M.connections_status(p["id"])["needs"]}
    assert st["github"]["state"] == "ok"
    assert st["hosting"]["state"] == "partial"                 # server not tested yet
    assert st["domain"]["state"] == "partial" and "DNS" in st["domain"]["detail"]
    assert st["email"]["state"] == "ok" and st["database"]["state"] == "ok"


def test_needs_managed_by_a_controller_are_delegated(P, M, home):
    p = P.create("md")
    path = home / "devpilot" / "projects" / "md"
    m = P.read_manifest(path)
    m["controller"] = {"name": "MD Console", "dir": "md_console", "command": "./run.sh",
                       "url": "http://127.0.0.1:8800", "manages": ["hosting", "domain", "email"], "auto": False}
    P._write_json(P.space_dir(path) / "project.json", m)
    M.set_model(p["id"], profile="enterprise", needs=["github", "hosting", "domain", "email", "controller"])
    st = {x["need"]: x for x in M.connections_status(p["id"])["needs"]}
    assert st["hosting"]["state"] == "delegated" and "MD Console" in st["hosting"]["detail"]
    assert st["domain"]["state"] == "delegated" and st["email"]["state"] == "delegated"
    assert st["controller"]["state"] == "ok"
    assert st["github"]["state"] == "missing"                   # not managed by the console: still DevPilot's job


def test_needs_without_a_type_make_it_custom(P, M, home):
    p = P.create("site")
    m = M.set_model(p["id"], needs=["github", "domain"])
    assert m["profile"] == "custom" and m["label"] == "Personnalise" and m["needs"] == ["github", "domain"]
