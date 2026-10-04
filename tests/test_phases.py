"""Phases: which steps exist (from the needs), their state (from reality), the current phase and next step."""
import json

import pytest

from conftest import sh


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


@pytest.fixture
def PH(home):
    import importlib, model, connections, phases
    for m in (model, connections, phases):
        importlib.reload(m)
    return phases


def steps(r):
    return {s["key"]: s for p in r["phases"] for s in p["steps"]}


def phase_keys(r):
    return [p["key"] for p in r["phases"]]


# ── Which steps exist ───────────────────────────────────────────────────────

def test_no_type_only_preparation_steps_and_no_crash(P, PH, home):
    p = P.create("x")
    r = PH.roadmap(p["id"])
    assert phase_keys(r) == ["prepare", "dev", "prod"]           # no needs: only need-less steps
    assert r["current"] == "prepare" and r["next"]["key"] == "type"
    assert steps(r)["type"]["state"] == "todo"


def test_vitrine_steps(P, PH, home):
    import model
    p = P.create("site")
    model.set_model(p["id"], profile="vitrine")
    r = PH.roadmap(p["id"])
    assert phase_keys(r) == ["prepare", "dev", "hosting", "golive", "prod"]
    k = set(steps(r))
    assert {"github", "brief", "first_commit", "pushed", "hosting", "deployed", "email_provider", "domain", "dns", "https", "mx", "handover"} <= k
    assert "database" not in k and "docker_dev" not in k and "backup" not in k


def test_api_project_has_no_golive(P, PH, home):
    import model
    p = P.create("api")
    model.set_model(p["id"], profile="api_backend")
    r = PH.roadmap(p["id"])
    assert "golive" not in phase_keys(r)
    assert {"database", "docker_dev", "monitoring"} <= set(steps(r))


def test_adding_a_need_later_adds_its_steps(P, PH, home):
    import model
    p = P.create("site")
    model.set_model(p["id"], profile="vitrine")
    assert "database" not in steps(PH.roadmap(p["id"]))
    model.set_model(p["id"], needs=model.PROFILES["vitrine"]["needs"] + ["database", "backup"])
    k = steps(PH.roadmap(p["id"]))
    assert "database" in k and "backup" in k


# ── State follows reality ───────────────────────────────────────────────────

def test_preparation_and_dev_follow_git(P, PH, home, remote):
    import model, db
    p = P.create("site")
    model.set_model(p["id"], profile="vitrine")
    r = PH.roadmap(p["id"])
    s = steps(r)
    assert s["type"]["state"] == "done" and s["github"]["state"] == "todo" and r["next"]["key"] == "github"
    P.link_git(p["id"], remote)                                  # repo with a commit, upstream set
    db.save_project_specs(p["id"], {}, [], "Site vitrine pour Hassouni Pro Trans")
    r = PH.roadmap(p["id"])
    s = steps(r)
    assert s["github"]["state"] == "done" and s["brief"]["state"] == "done"
    assert s["first_commit"]["state"] == "done" and s["pushed"]["state"] == "done"
    assert r["current"] == "dev" and r["next"]["key"] == "dev_done"     # only the manual step is left in dev
    path = home / "devpilot" / "projects" / "site"
    (path / "new.txt").write_text("x")
    sh("git", "add", "-A", cwd=path); sh("git", "commit", "-qm", "work", cwd=path)
    s = steps(PH.roadmap(p["id"]))
    assert s["pushed"]["state"] == "partial" and "1 commit" in s["pushed"]["detail"]


def test_hosting_and_golive_follow_connections(P, PH, home, monkeypatch):
    import model, connections, servers, dnscheck
    p = P.create("site")
    model.set_model(p["id"], profile="site_backend")
    s = steps(PH.roadmap(p["id"]))
    assert s["hosting"]["state"] == "todo" and s["database"]["state"] == "todo"
    servers.add_server(p["id"], {"name": "VPS", "role": "prod", "host": "41.1.2.3", "user": "ubuntu"}, test=False)
    connections.save(p["id"], "database", {"type": "postgres"})
    s = steps(PH.roadmap(p["id"]))
    assert s["hosting"]["state"] == "partial" and s["database"]["state"] == "partial"
    connections.save(p["id"], "database", {"host": "db.internal"})
    connections.set_domain(p["id"], {"domain": "monsite.ma"})
    connections.set_email(p["id"], {"provider": "google"})
    s = steps(PH.roadmap(p["id"]))
    assert s["database"]["state"] == "done" and s["domain"]["state"] == "done"
    assert s["dns"]["state"] == "todo" and s["mx"]["state"] == "todo" and s["email_provider"]["state"] == "done"
    records = {("monsite.ma", "A"): ["41.1.2.3"], ("www.monsite.ma", "A"): ["41.1.2.3"],
               ("monsite.ma", "MX"): ["1 aspmx.l.google.com"], ("monsite.ma", "TXT"): [], ("_dmarc.monsite.ma", "TXT"): []}
    monkeypatch.setattr(dnscheck, "_resolver", lambda d, t: list(records.get((d.lower().rstrip("."), t.upper()), [])))
    connections.verify_domain(p["id"])
    connections.verify_email(p["id"])
    s = steps(PH.roadmap(p["id"]))
    assert s["dns"]["state"] == "done" and s["mx"]["state"] == "done"
    records[("monsite.ma", "A")] = ["9.9.9.9"]
    connections.verify_domain(p["id"])
    assert steps(PH.roadmap(p["id"]))["dns"]["state"] == "partial"


# ── Skip / manual / custom ──────────────────────────────────────────────────

def test_skip_does_not_block_and_can_be_taken_back(P, PH, home):
    import model
    p = P.create("site")
    model.set_model(p["id"], profile="vitrine")
    r = PH.roadmap(p["id"])
    assert r["next"]["key"] == "github"
    r = PH.skip(p["id"], "github")
    assert r["next"]["key"] == "brief" and steps(r)["github"]["skipped"]
    m = json.loads((home / "devpilot" / "projects" / "site" / ".devpilot" / "project.json").read_text())
    assert m["phases"]["skipped"] == ["github"]
    r = PH.skip(p["id"], "github", later=False)
    assert r["next"]["key"] == "github"
    err(P, PH.skip, p["id"], "nope")


def test_manual_steps_are_ticked_automatic_ones_are_not(P, PH, home):
    import model
    p = P.create("site")
    model.set_model(p["id"], profile="vitrine")
    r = PH.mark(p["id"], "dev_done")
    assert steps(r)["dev_done"]["state"] == "done" and "fait le" in steps(r)["dev_done"]["detail"]
    r = PH.mark(p["id"], "dev_done", done=False)
    assert steps(r)["dev_done"]["state"] == "todo"
    e = err(P, PH.mark, p["id"], "github")
    assert "verifie toute seule" in e.message


def test_custom_steps(P, PH, home):
    import model
    p = P.create("site")
    model.set_model(p["id"], profile="vitrine")
    r = PH.add_custom(p["id"], "hosting", "Configurer Stripe", "Cles API du client")
    custom = [s for s in steps(r).values() if s["custom"]]
    assert len(custom) == 1 and custom[0]["label"] == "Configurer Stripe" and custom[0]["state"] == "todo"
    key = custom[0]["key"]
    r = PH.mark(p["id"], key)
    assert steps(r)[key]["state"] == "done"
    r = PH.skip(p["id"], key)
    assert steps(r)[key]["skipped"]
    r = PH.remove_custom(p["id"], key)
    assert key not in steps(r)
    err(P, PH.add_custom, p["id"], "nope", "x")
    err(P, PH.add_custom, p["id"], "dev", "  ")


# ── Current phase progression and completion ────────────────────────────────

def test_progression_to_production(P, PH, home, remote):
    import model, db
    p = P.create("app")
    model.set_model(p["id"], profile="custom", needs=["github"])
    r = PH.roadmap(p["id"])
    assert phase_keys(r) == ["prepare", "dev", "prod"] and r["current"] == "prepare"
    P.link_git(p["id"], remote)
    db.save_project_specs(p["id"], {}, [], "brief")
    r = PH.roadmap(p["id"])
    assert r["current"] == "dev" and r["next"]["key"] == "dev_done"
    PH.mark(p["id"], "dev_done")
    r = PH.roadmap(p["id"])
    assert r["current"] == "prod" and r["next"]["key"] == "handover"
    r = PH.mark(p["id"], "handover")
    assert r["complete"] and r["next"] is None and r["current"] == "prod"
    assert r["done"] == r["total"]


def test_controller_delegates_its_steps(P, PH, home):
    import model
    p = P.create("md")
    path = home / "devpilot" / "projects" / "md"
    m = P.read_manifest(path)
    m["controller"] = {"name": "MD Console", "dir": ".", "command": "./run.sh", "url": "http://127.0.0.1:8800",
                       "manages": ["hosting", "domain", "email", "backup"], "auto": False}
    P._write_json(P.space_dir(path) / "project.json", m)
    model.set_model(p["id"], profile="enterprise")
    r = PH.roadmap(p["id"])
    s = steps(r)
    assert s["hosting"]["state"] == "delegated" and s["dns"]["state"] == "delegated" and s["backup"]["state"] == "delegated"
    assert all(x["state"] != "delegated" for x in s.values() if x["need"] is None)   # type/brief/handover stay ours


def test_missing_folder_never_crashes(P, PH, home):
    import model
    p = P.create("gone")
    model.set_model(p["id"], profile="vitrine")
    (home / "devpilot" / "projects" / "gone").rename(home / "elsewhere")
    r = PH.roadmap(p["id"])
    assert r["phases"] and r["next"]
    assert PH.summary(p["id"])["current"]
    err(P, PH.skip, p["id"], "github")                           # cannot write the state without the folder


def test_summary_for_lists(P, PH, home):
    import model
    p = P.create("site")
    model.set_model(p["id"], profile="vitrine")
    s = PH.summary(p["id"])
    assert s["current"] == "prepare" and s["next"] == "Lier le projet a GitHub" and s["total"] > 5
