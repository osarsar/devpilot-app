"""DevPilot — the connections of a client site: domain, professional email.

Each one is a component of the project (DB) mirrored, without secrets, into
.devpilot/connections.json (the folder is the reference: a re-imported
project gets its connections back). What makes these real:
  - expected_dns(): the exact records the client must add at the registrar,
    derived from the project's hosting (server IP, or platform) and email provider
  - verify_domain() / verify_email() / verify_site(): real DNS / MX / HTTP checks
    (dnscheck.py), stored with their date so the needs show what was verified
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import db
import projects as P
from projects import ProjectError

SECRET_KEYS = {"password", "token", "secret", "secret_key", "access_key", "api_key", "connection_string", "private_key"}
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")

EMAIL_PROVIDERS = {
    "google":     {"label": "Google Workspace", "mx": [(1, "aspmx.l.google.com"), (5, "alt1.aspmx.l.google.com"),
                                                        (5, "alt2.aspmx.l.google.com"), (10, "alt3.aspmx.l.google.com"), (10, "alt4.aspmx.l.google.com")],
                   "spf": "v=spf1 include:_spf.google.com ~all", "webmail": "https://mail.google.com/", "admin": "https://admin.google.com/"},
    "microsoft":  {"label": "Microsoft 365", "mx": [(0, "<domaine>.mail.protection.outlook.com")],
                   "spf": "v=spf1 include:spf.protection.outlook.com -all", "webmail": "https://outlook.office.com/", "admin": "https://admin.microsoft.com/"},
    "zoho":       {"label": "Zoho Mail", "mx": [(10, "mx.zoho.com"), (20, "mx2.zoho.com"), (50, "mx3.zoho.com")],
                   "spf": "v=spf1 include:zoho.com ~all", "webmail": "https://mail.zoho.com/", "admin": "https://mailadmin.zoho.com/"},
    "ovh":        {"label": "OVH Email", "mx": [(1, "mx1.mail.ovh.net"), (5, "mx2.mail.ovh.net"), (100, "mx3.mail.ovh.net")],
                   "spf": "v=spf1 include:mx.ovh.com ~all", "webmail": "https://www.ovh.com/fr/mail/", "admin": "https://www.ovh.com/manager/"},
    "ionos":      {"label": "IONOS", "mx": [(10, "mx00.ionos.fr"), (10, "mx01.ionos.fr")],
                   "spf": "v=spf1 include:_spf-eu.ionos.com ~all", "webmail": "https://mail.ionos.fr/", "admin": "https://my.ionos.fr/"},
    "infomaniak": {"label": "Infomaniak", "mx": [(5, "mta-gw.infomaniak.ch")],
                   "spf": "v=spf1 include:spf.infomaniak.ch ~all", "webmail": "https://mail.infomaniak.com/", "admin": "https://manager.infomaniak.com/"},
    "proton":     {"label": "Proton Mail", "mx": [(10, "mail.protonmail.ch"), (20, "mailsec.protonmail.ch")],
                   "spf": "v=spf1 include:_spf.protonmail.ch ~all", "webmail": "https://mail.proton.me/", "admin": "https://account.proton.me/"},
    "hostinger":  {"label": "Hostinger", "mx": [(5, "mx1.hostinger.com"), (10, "mx2.hostinger.com")],
                   "spf": "v=spf1 include:_spf.mail.hostinger.com ~all", "webmail": "https://mail.hostinger.com/", "admin": "https://hpanel.hostinger.com/"},
    "cloudflare": {"label": "Cloudflare Email Routing", "mx": [(13, "amir.mx.cloudflare.net"), (49, "linda.mx.cloudflare.net"), (80, "isaac.mx.cloudflare.net")],
                   "spf": "v=spf1 include:_spf.mx.cloudflare.net ~all", "webmail": "", "admin": "https://dash.cloudflare.com/"},
    "other":      {"label": "Autre", "mx": [], "spf": "", "webmail": "", "admin": ""},
}

REGISTRARS = {
    "ovh":        {"label": "OVH", "dns": "https://www.ovh.com/manager/#/web/domain/<domaine>/zone"},
    "cloudflare": {"label": "Cloudflare", "dns": "https://dash.cloudflare.com/"},
    "namecheap":  {"label": "Namecheap", "dns": "https://ap.www.namecheap.com/domains/domaincontrolpanel/<domaine>/advancedns"},
    "godaddy":    {"label": "GoDaddy", "dns": "https://dcc.godaddy.com/manage/<domaine>/dns"},
    "ionos":      {"label": "IONOS", "dns": "https://my.ionos.fr/domains"},
    "gandi":      {"label": "Gandi", "dns": "https://admin.gandi.net/domain/"},
    "hostinger":  {"label": "Hostinger", "dns": "https://hpanel.hostinger.com/domains"},
    "genious":    {"label": "Genious (Maroc)", "dns": "https://www.genious.net/"},
    "other":      {"label": "Autre", "dns": ""},
}


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ── Component storage (+ mirror in the project folder) ──────────────────────

def _cfg(pid, key):
    c = db.get_project_component(pid, key)
    if not c:
        return {}
    cfg = c.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    return dict(cfg)


def save(pid, key, cfg, replace=False):
    """Merge cfg into the component (replace=True: the whole config). False and ""
    are kept (they clear a value); masked secrets ("****") are ignored; None deletes a key."""
    cur = {} if replace else _cfg(pid, key)
    for k, v in (cfg or {}).items():
        if isinstance(v, str) and v.startswith("****"):
            continue
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    if db.get_project_component(pid, key):
        db.update_project_component(pid, key, config=cur, enabled=True)
    else:
        db.add_project_component(pid, key, enabled=True, config=cur)
    mirror(pid)
    return cur


def mirror(pid):
    """Write every component config (minus secrets) to .devpilot/connections.json."""
    project = db.get_project(pid)
    if not project or not project.get("path") or not os.path.isdir(project["path"]):
        return
    data = {}
    for c in db.get_project_components(pid):
        if not c.get("enabled"):
            continue
        cfg = c.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = {}
        data[c["component"]] = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    dp = P.space_dir(project["path"])
    dp.mkdir(parents=True, exist_ok=True)
    P._write_json(dp / "connections.json", data)


def restore(pid, path):
    """A re-imported folder: its connections.json becomes the components (missing ones only)."""
    f = Path(path) / ".devpilot" / "connections.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    n = 0
    for key, cfg in data.items():
        if not isinstance(cfg, dict) or db.get_project_component(pid, key):
            continue
        db.add_project_component(pid, key, enabled=True, config=cfg)
        n += 1
    return n


# ── Domain ──────────────────────────────────────────────────────────────────

def clean_domain(domain):
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    d = d[4:] if d.startswith("www.") else d
    if not _DOMAIN_RE.match(d):
        raise ProjectError(f"Nom de domaine invalide : « {domain} » (ex. monsite.ma)")
    return d


def hosting_target(pid):
    """Where the domain must point: the prod server's IP, or the deploy platform."""
    project = P.get(pid)
    m = P.read_manifest(project["path"]) if project.get("path") and os.path.isdir(project["path"]) else {}
    servers = m.get("servers") or []
    prod = next((s for s in servers if s.get("role") == "prod"), servers[0] if servers else None)
    if prod and prod.get("host"):
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", prod["host"]):
            return {"kind": "server", "name": prod.get("name"), "ip": prod["host"]}
        return {"kind": "server", "name": prod.get("name"), "host": prod["host"]}
    dep = _cfg(pid, "deploy")
    if dep.get("platform"):
        return {"kind": "platform", "platform": dep["platform"], "site_url": dep.get("site_url") or dep.get("url", "")}
    return None


def expected_dns(pid):
    """The records to add at the registrar: site (A / CNAME), www, MX, SPF."""
    import dnscheck
    dom = _cfg(pid, "domain")
    domain = dom.get("domain", "")
    target = hosting_target(pid)
    records, expected = [], None
    if target and target["kind"] == "server":
        if target.get("ip"):
            expected = {"type": "A", "value": target["ip"]}
            records.append({"type": "A", "name": "@", "value": target["ip"], "why": f"le site, servi par {target.get('name') or 'ton serveur'}"})
            records.append({"type": "A", "name": "www", "value": target["ip"], "why": "www.<domaine> aussi"})
        else:
            expected = {"type": "CNAME", "value": target["host"]}
            records.append({"type": "CNAME", "name": "@", "value": target["host"], "why": "le site"})
            records.append({"type": "CNAME", "name": "www", "value": target["host"], "why": "www.<domaine> aussi"})
    elif target and target["kind"] == "platform":
        tgt = ""
        site = target.get("site_url", "")
        if site:
            tgt = re.sub(r"^https?://", "", site).split("/")[0]
        expected = dnscheck.expected_for(target["platform"], target=tgt or None)
        if expected:
            if expected.get("apex_a"):
                for ip in expected["apex_a"]:
                    records.append({"type": "A", "name": "@", "value": ip, "why": f"le site ({target['platform']})"})
            if expected.get("type") == "CNAME":
                records.append({"type": "CNAME", "name": "www", "value": expected["value"], "why": f"www ({target['platform']})"})
                if not expected.get("apex_a"):
                    records.append({"type": "CNAME", "name": "@", "value": expected["value"], "why": f"le site ({target['platform']})"})
    em = _cfg(pid, "email")
    prov = EMAIL_PROVIDERS.get(em.get("provider") or "")
    if prov and prov["mx"]:
        for prio, host in prov["mx"]:
            records.append({"type": "MX", "name": "@", "value": host.replace("<domaine>", domain.replace(".", "-") if domain else "<domaine>"),
                            "priority": prio, "why": f"recevoir les emails ({prov['label']})"})
        if prov["spf"]:
            records.append({"type": "TXT", "name": "@", "value": prov["spf"], "why": "SPF : les emails envoyes ne partent pas en spam"})
        records.append({"type": "TXT", "name": "_dmarc", "value": f"v=DMARC1; p=none; rua=mailto:postmaster@{domain or '<domaine>'}", "why": "DMARC : rapports de livraison"})
    return {"domain": domain, "target": target, "expected": expected, "records": records}


def set_domain(pid, cfg):
    cur = _cfg(pid, "domain")
    new = {}
    if "domain" in cfg:
        new["domain"] = clean_domain(cfg["domain"])
        if new["domain"] != cur.get("domain"):
            new.update(dns_verified=False, dns_result=None, site_ok=None)
    for k in ("registrar", "dashboard_url", "ssl", "notes"):
        if k in cfg:
            new[k] = (cfg[k] or "").strip()
    if new.get("registrar") and new["registrar"] not in REGISTRARS:
        new["registrar"] = "other"
    if new.get("registrar") and not new.get("dashboard_url") and not cur.get("dashboard_url"):
        new["dashboard_url"] = REGISTRARS[new["registrar"]]["dns"].replace("<domaine>", new.get("domain") or cur.get("domain", ""))
    return save(pid, "domain", new)


def verify_domain(pid):
    """Real DNS check of the domain against the hosting target; stored with its date."""
    import dnscheck
    dom = _cfg(pid, "domain")
    if not dom.get("domain"):
        raise ProjectError("Pas de domaine enregistre pour ce projet")
    exp = expected_dns(pid)
    if not exp["expected"]:
        raise ProjectError("Ajoute d'abord l'hebergement (serveur ou plateforme) : DevPilot saura alors ou le domaine doit pointer")
    res = dnscheck.check_domain(dom["domain"], exp["expected"])
    save(pid, "domain", {"dns_verified": bool(res.get("ok")), "dns_checked_at": _now(), "dns_result": res})
    return res


def verify_site(pid):
    import dnscheck
    dom = _cfg(pid, "domain")
    if not dom.get("domain"):
        raise ProjectError("Pas de domaine enregistre pour ce projet")
    res = dnscheck.check_http("https://" + dom["domain"])
    if not res.get("ok"):
        alt = dnscheck.check_http("http://" + dom["domain"])
        if alt.get("ok"):
            res = {**alt, "https": False, "message": alt.get("message", "") + " (en HTTP seulement : pas de certificat HTTPS)"}
    save(pid, "domain", {"site_ok": bool(res.get("ok")), "site_checked_at": _now(), "site_result": res})
    return res


def domain_state(pid):
    dom = _cfg(pid, "domain")
    exp = expected_dns(pid) if dom.get("domain") else {"records": [], "target": hosting_target(pid), "expected": None, "domain": ""}
    reg = REGISTRARS.get(dom.get("registrar") or "", {})
    return {"config": {k: v for k, v in dom.items() if k not in SECRET_KEYS}, "registrar_label": reg.get("label", ""),
            "expected": exp, "registrars": {k: v["label"] for k, v in REGISTRARS.items()},
            "registrars_order": list(REGISTRARS)}


# ── Email ───────────────────────────────────────────────────────────────────

def set_email(pid, cfg):
    cur = _cfg(pid, "email")
    new = {}
    if "provider" in cfg:
        p = (cfg["provider"] or "").strip().lower()
        if p and p not in EMAIL_PROVIDERS:
            p = "other"
        new["provider"] = p
        if p != cur.get("provider"):
            new.update(mx_verified=False, mx_result=None)
        prov = EMAIL_PROVIDERS.get(p)
        if prov:
            if not cur.get("webmail_url") and prov["webmail"]:
                new["webmail_url"] = prov["webmail"]
            if not cur.get("dashboard_url") and prov["admin"]:
                new["dashboard_url"] = prov["admin"]
    if "addresses" in cfg:
        raw = cfg["addresses"] if isinstance(cfg["addresses"], list) else re.split(r"[\s,;]+", cfg["addresses"] or "")
        addrs = [a.strip().lower() for a in raw if a.strip()]
        bad = [a for a in addrs if not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", a)]
        if bad:
            raise ProjectError("Adresse invalide : " + ", ".join(bad))
        new["addresses"] = addrs
    for k in ("dashboard_url", "webmail_url", "notes"):
        if k in cfg and (cfg[k] or "").strip():
            new[k] = cfg[k].strip()
        elif k in cfg and k not in new and cur.get(k):
            new[k] = ""                                   # explicitly cleared
    return save(pid, "email", new)


def verify_email(pid):
    """Real MX (+ SPF / DMARC) check of the project's domain for its provider."""
    import dnscheck
    em = _cfg(pid, "email")
    dom = _cfg(pid, "domain")
    domain = dom.get("domain") or (em.get("addresses") or [""])[0].split("@")[-1]
    if not domain:
        raise ProjectError("Enregistre d'abord le domaine (ou une adresse) pour verifier les MX")
    provider = em.get("provider") or None
    mx = dnscheck.check_mx(domain, provider if provider in dnscheck.KNOWN_MX else None)
    auth = dnscheck.check_spf_dmarc(domain)
    res = {"domain": domain, "mx": mx, "auth": auth, "ok": bool(mx.get("ok")), "checked_at": _now()}
    save(pid, "email", {"mx_verified": res["ok"], "mx_checked_at": res["checked_at"], "mx_result": res})
    return res


def email_state(pid):
    em = _cfg(pid, "email")
    prov = EMAIL_PROVIDERS.get(em.get("provider") or "", {})
    dom = _cfg(pid, "domain").get("domain", "")
    return {"config": {k: v for k, v in em.items() if k not in SECRET_KEYS}, "provider_label": prov.get("label", ""),
            "domain": dom, "providers": {k: v["label"] for k, v in EMAIL_PROVIDERS.items()},
            "providers_order": list(EMAIL_PROVIDERS),
            "suggested": [f"contact@{dom}", f"info@{dom}"] if dom else []}
