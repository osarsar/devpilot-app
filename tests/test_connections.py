"""Domain and professional email of a client site: expected records, real checks (faked DNS)."""
import json

import pytest


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


@pytest.fixture
def C(home):
    import importlib, dnscheck, connections
    importlib.reload(dnscheck)
    importlib.reload(connections)
    return connections


@pytest.fixture
def dns(monkeypatch):
    """Fake resolver: records[(domain, type)] = [...]"""
    import dnscheck
    records = {}
    monkeypatch.setattr(dnscheck, "_resolver", lambda d, t: list(records.get((d.lower().rstrip("."), t.upper()), [])))
    return records


@pytest.fixture
def site(P, C, home):
    p = P.create("client")
    import servers
    servers.add_server(p["id"], {"name": "VPS prod", "role": "prod", "provider": "ovh", "host": "41.1.2.3", "user": "ubuntu"}, test=False)
    return p["id"]


# ── domain ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,clean", [("Monsite.MA", "monsite.ma"), ("https://www.monsite.ma/", "monsite.ma"), ("www.a-b.co.uk", "a-b.co.uk")])
def test_clean_domain(C, raw, clean):
    assert C.clean_domain(raw) == clean


@pytest.mark.parametrize("bad", ["", "monsite", "mon site.ma", "-x.ma", "a..ma"])
def test_bad_domain(P, C, bad):
    err(P, C.clean_domain, bad)


def test_set_domain_and_expected_records_from_the_server(C, site, home):
    C.set_domain(site, {"domain": "Monsite.ma", "registrar": "ovh"})
    st = C.domain_state(site)
    assert st["config"]["domain"] == "monsite.ma" and st["registrar_label"] == "OVH"
    assert "monsite.ma" in st["config"]["dashboard_url"]                       # registrar DNS page prefilled
    recs = {(r["type"], r["name"]): r["value"] for r in st["expected"]["records"]}
    assert recs[("A", "@")] == "41.1.2.3" and recs[("A", "www")] == "41.1.2.3"
    assert st["expected"]["expected"] == {"type": "A", "value": "41.1.2.3"}
    # mirrored in the project folder
    cj = json.loads((home / "devpilot" / "projects" / "client" / ".devpilot" / "connections.json").read_text())
    assert cj["domain"]["domain"] == "monsite.ma"


def test_expected_records_from_a_platform(P, C, home):
    p = P.create("site")
    C.save(p["id"], "deploy", {"platform": "vercel", "site_url": "https://site.vercel.app"})
    C.set_domain(p["id"], {"domain": "site.ma"})
    recs = C.expected_dns(p["id"])["records"]
    types = {(r["type"], r["name"]) for r in recs}
    assert ("CNAME", "www") in types and ("A", "@") in types                   # vercel: apex A + www CNAME


def test_verify_domain_ok_and_wrong(C, site, dns):
    C.set_domain(site, {"domain": "monsite.ma"})
    dns[("monsite.ma", "A")] = ["41.1.2.3"]
    dns[("www.monsite.ma", "A")] = ["41.1.2.3"]
    r = C.verify_domain(site)
    assert r["ok"] and C.domain_state(site)["config"]["dns_verified"] is True
    dns[("monsite.ma", "A")] = ["5.5.5.5"]
    r = C.verify_domain(site)
    assert not r["ok"] and "41.1.2.3" in r["message"]
    assert C.domain_state(site)["config"]["dns_verified"] is False


def test_verify_domain_needs_hosting(P, C, home):
    p = P.create("site")
    C.set_domain(p["id"], {"domain": "site.ma"})
    e = err(P, C.verify_domain, p["id"])
    assert "hebergement" in e.message


def test_changing_the_domain_resets_the_verification(C, site, dns):
    C.set_domain(site, {"domain": "monsite.ma"})
    dns[("monsite.ma", "A")] = ["41.1.2.3"]; dns[("www.monsite.ma", "A")] = ["41.1.2.3"]
    C.verify_domain(site)
    C.set_domain(site, {"domain": "autre.ma"})
    assert C.domain_state(site)["config"]["dns_verified"] is False


# ── email ───────────────────────────────────────────────────────────────────

def test_set_email_provider_and_addresses(C, site):
    C.set_domain(site, {"domain": "monsite.ma"})
    C.set_email(site, {"provider": "google", "addresses": "Contact@monsite.ma, info@monsite.ma"})
    st = C.email_state(site)
    assert st["config"]["addresses"] == ["contact@monsite.ma", "info@monsite.ma"]
    assert st["config"]["webmail_url"] == "https://mail.google.com/" and st["provider_label"] == "Google Workspace"
    recs = [r for r in C.expected_dns(site)["records"] if r["type"] == "MX"]
    assert recs[0]["value"] == "aspmx.l.google.com" and recs[0]["priority"] == 1
    assert any(r["type"] == "TXT" and "spf1" in r["value"] for r in C.expected_dns(site)["records"])


def test_bad_address(P, C, site):
    err(P, C.set_email, site, {"addresses": "contact@monsite"})


def test_verify_email(C, site, dns):
    C.set_domain(site, {"domain": "monsite.ma"})
    C.set_email(site, {"provider": "google"})
    dns[("monsite.ma", "MX")] = ["1 aspmx.l.google.com", "5 alt1.aspmx.l.google.com"]
    dns[("monsite.ma", "TXT")] = ["v=spf1 include:_spf.google.com ~all"]
    dns[("_dmarc.monsite.ma", "TXT")] = ["v=DMARC1; p=none"]
    r = C.verify_email(site)
    assert r["ok"] and r["mx"]["detected"] == "google" and r["auth"]["ok"]
    assert C.email_state(site)["config"]["mx_verified"] is True
    dns[("monsite.ma", "MX")] = ["10 mx.zoho.com"]
    r = C.verify_email(site)
    assert not r["ok"] and r["mx"]["detected"] == "zoho"


# ── storage rules ───────────────────────────────────────────────────────────

def test_save_keeps_false_and_drops_masked_secrets(C, site):
    C.save(site, "database", {"type": "postgres", "password": "s3cret", "ssl": True})
    C.save(site, "database", {"password": "****", "ssl": False, "type": None})
    cfg = C._cfg(site, "database")
    assert cfg == {"password": "s3cret", "ssl": False}


def test_secrets_never_reach_the_project_folder(C, site, home):
    C.save(site, "database", {"type": "postgres", "password": "s3cret", "connection_string": "postgres://u:p@h/db"})
    cj = json.loads((home / "devpilot" / "projects" / "client" / ".devpilot" / "connections.json").read_text())
    assert cj["database"] == {"type": "postgres"}


def test_connections_come_back_after_reimport(P, C, home, site):
    C.set_domain(site, {"domain": "monsite.ma", "registrar": "ovh"})
    C.set_email(site, {"provider": "zoho"})
    P.remove(site)
    q = P.adopt(home / "devpilot" / "projects" / "client")
    assert C.domain_state(q["id"])["config"]["domain"] == "monsite.ma"
    assert C.email_state(q["id"])["config"]["provider"] == "zoho"
