"""DevPilot — what a project IS and what it NEEDS.

DevPilot is the root manager: every project is here. A project has a
profile (what kind of site / software it is), which gives its needs (the
connections it must have: GitHub, hosting, domain, email, database...).
A project may carry its own controller (e.g. MarocDefender's md_console):
the needs that controller manages are delegated to it, DevPilot only keeps
the folder, the local development and the access to the console.

The profile and needs live in the project folder (.devpilot/project.json),
mirrored to the DB for the rest of the app.
"""

import os

import db
import projects as P
from projects import ProjectError

# ── Vocabulary ──────────────────────────────────────────────────────────────

NEEDS = {
    "github":     {"label": "GitHub",            "desc": "Code source sauvegarde sur GitHub"},
    "hosting":    {"label": "Hebergement",       "desc": "Serveur (VPS) ou plateforme (Vercel, Netlify...) qui sert le site"},
    "domain":     {"label": "Nom de domaine",    "desc": "Le domaine du client, pointe vers l'hebergement"},
    "email":      {"label": "Email pro",         "desc": "Adresses professionnelles sur le domaine"},
    "database":   {"label": "Base de donnees",   "desc": "PostgreSQL, MySQL, SQLite..."},
    "storage":    {"label": "Stockage",          "desc": "Fichiers et medias (Cloudflare R2, S3)"},
    "backup":     {"label": "Sauvegardes",       "desc": "Copies de la base et des fichiers"},
    "docker":     {"label": "Docker",            "desc": "Conteneurs pour le dev et la prod"},
    "auth":       {"label": "Authentification",  "desc": "Comptes utilisateurs"},
    "monitoring": {"label": "Monitoring",        "desc": "Sante du site, logs"},
    "controller": {"label": "Controleur",        "desc": "Console propre au projet qui gere son exploitation"},
}

# Profiles: what kind of project, and what it needs. The old wizard keys
# (vitrine, blog_cms, webapp_saas, api_backend, mobile, custom) are kept.
PROFILES = {
    "vitrine":      {"label": "Site vitrine",        "icon": "🌐", "kind": "site",
                     "desc": "Frontend seul : landing page, portfolio, site statique",
                     "needs": ["github", "hosting", "domain", "email"]},
    "site_backend": {"label": "Site avec backend",   "icon": "🧩", "kind": "site",
                     "desc": "Frontend + backend : formulaires, espace client, API",
                     "needs": ["github", "hosting", "domain", "email", "database"]},
    "blog_cms":     {"label": "Blog / CMS",          "icon": "📝", "kind": "site",
                     "desc": "Site avec contenu dynamique et medias",
                     "needs": ["github", "hosting", "domain", "email", "database", "storage"]},
    "site_complet": {"label": "Site complet",        "icon": "🏗️", "kind": "site",
                     "desc": "Frontend + backend + stockage + sauvegardes",
                     "needs": ["github", "hosting", "domain", "email", "database", "storage", "backup"]},
    "webapp_saas":  {"label": "Application web / SaaS", "icon": "🧱", "kind": "software",
                     "desc": "Application avec comptes, base, cloud, monitoring",
                     "needs": ["github", "hosting", "domain", "email", "database", "storage", "backup",
                               "docker", "auth", "monitoring"]},
    "api_backend":  {"label": "API / Backend",       "icon": "⚡", "kind": "software",
                     "desc": "API REST/GraphQL, microservices",
                     "needs": ["github", "hosting", "database", "docker", "monitoring"]},
    "mobile":       {"label": "Mobile",              "icon": "📱", "kind": "software",
                     "desc": "Flutter, React Native...",
                     "needs": ["github", "database", "storage"]},
    "enterprise":   {"label": "Systeme d'entreprise", "icon": "🏢", "kind": "system",
                     "desc": "Gros projet sur mesure avec son propre controleur (comme MarocDefender)",
                     "needs": ["github", "controller"]},
    "custom":       {"label": "Personnalise",        "icon": "⚙️", "kind": "site",
                     "desc": "Choisir les besoins a la main",
                     "needs": []},
}

# A controller inside a project typically manages these needs
CONTROLLER_DEFAULT_MANAGES = ["hosting", "domain", "email", "backup", "monitoring"]


# ── Read / write the model ──────────────────────────────────────────────────

def _manifest(project):
    return P.read_manifest(project["path"]) if project.get("path") and os.path.isdir(project["path"]) else {}


def get_model(pid, probe=True):
    """{profile, kind, needs, controller} of a project (needs = profile needs unless customised).
    probe=False: don't contact the controller (fast, for lists)."""
    project = P.get(pid)
    m = _manifest(project)
    profile = m.get("profile") or project.get("profile") or ""
    if profile not in PROFILES:
        profile = ""
    prof = PROFILES.get(profile)
    needs = m.get("needs")
    if not isinstance(needs, list):
        needs = list(prof["needs"]) if prof else []
    needs = [n for n in needs if n in NEEDS]
    ctrl = _controller(pid, project, m, probe)
    if ctrl:
        if "controller" not in needs:
            needs.append("controller")
        # what the console manages is still a need of the project (shown as delegated)
        for k in ctrl.get("manages") or []:
            if k in NEEDS and k not in needs:
                needs.append(k)
        order = list(NEEDS)
        needs.sort(key=order.index)
    return {"profile": profile, "label": prof["label"] if prof else "Non defini",
            "kind": prof["kind"] if prof else "", "needs": needs, "controller": ctrl}


def set_model(pid, profile=None, needs=None):
    """Change the profile and/or the needs. Written in the project folder and in the DB."""
    project = P.get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        raise ProjectError("Le dossier du projet est introuvable")
    m = P.read_manifest(project["path"])
    if profile is not None:
        if profile and profile not in PROFILES:
            raise ProjectError(f"Type de projet inconnu : {profile}")
        m["profile"] = profile
        db.set_project_profile(pid, profile)
        if needs is None:                       # a new profile resets the needs to its defaults
            m.pop("needs", None)
    if needs is not None:
        bad = [n for n in needs if n not in NEEDS]
        if bad:
            raise ProjectError("Besoin inconnu : " + ", ".join(bad))
        if not (profile or m.get("profile")):          # needs chosen by hand, no type: that's "Personnalise"
            m["profile"] = "custom"
            db.set_project_profile(pid, "custom")
        order = list(NEEDS)
        m["needs"] = sorted(dict.fromkeys(needs), key=order.index)      # always in the NEEDS order
    P._write_json(P.space_dir(project["path"]) / "project.json", m)
    P.write_space(pid)
    return get_model(pid)


def _controller(pid, project, m, probe=True):
    """The project's controller: declared in the folder, or auto-detected (controller.py)."""
    declared = m.get("controller") if isinstance(m.get("controller"), dict) else None
    if not probe:
        if declared:
            return {**declared, "status": None}
        try:
            import controller
            cands = controller.detect(project["path"]) if project.get("path") else []
            c = next((x for x in cands if x.get("url")), None)
            return {**c, "auto": True, "manages": list(CONTROLLER_DEFAULT_MANAGES), "status": None} if c else None
        except Exception:
            return None
    try:
        import controller
        return controller.get(pid)
    except ImportError:
        return declared
    except ProjectError:
        return declared


# ── State of each need ──────────────────────────────────────────────────────

def _component(pid, key):
    c = db.get_project_component(pid, key)
    if not c or not c.get("enabled"):
        return None
    cfg = c.get("config") or {}
    if isinstance(cfg, str):
        try:
            import json
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    return cfg


def _need_state(pid, project, m, need):
    """(state, detail): state = ok | partial | missing"""
    if need == "github":
        if m.get("github", {}).get("org") if isinstance(m.get("github"), dict) else False:
            return "ok", f"organisation {m['github']['org']}"
        if project.get("git_remote"):
            return "ok", project["git_remote"]
        return "missing", "pas encore lie a GitHub"
    if need == "hosting":
        servers = m.get("servers") or []
        if servers:
            ok = [s for s in servers if (s.get("test") or {}).get("ok")]
            return ("ok" if ok else "partial"), (f"{len(servers)} serveur(s)" + ("" if ok else " — connexion a verifier"))
        dep = _component(pid, "deploy") or {}
        if dep.get("site_url") or dep.get("url"):
            return "ok", dep.get("platform") or dep.get("site_url") or dep.get("url")
        if dep.get("platform") or dep.get("account_id"):
            return "partial", f"{dep.get('platform', 'plateforme')} choisie, site pas encore en ligne"
        return "missing", "aucun serveur ni plateforme"
    if need == "domain":
        dom = _component(pid, "domain") or {}
        if dom.get("domain"):
            if dom.get("dns_verified") or dom.get("dns_configured"):
                return "ok", dom["domain"]
            return "partial", f"{dom['domain']} — DNS a verifier"
        return "missing", "pas de domaine"
    if need == "email":
        em = _component(pid, "email") or {}
        if em.get("provider") or em.get("addresses"):
            if em.get("mx_verified"):
                return "ok", em.get("provider") or em.get("addresses")
            return "partial", f"{em.get('provider') or 'email'} — MX a verifier"
        return "missing", "pas d'email pro"
    if need == "controller":
        ctrl = m.get("controller") or _controller(pid, project, m, probe=False)
        if ctrl:
            return "ok", ctrl.get("name") or ctrl.get("dir") or "console"
        return "missing", "pas de controleur detecte"
    cfg = _component(pid, need)
    if cfg is not None:
        return ("ok" if cfg else "partial"), ("configure" if cfg else "active, a configurer")
    return "missing", "a configurer"


def connections_status(pid, probe=True):
    """For each need of the project: ok / partial / missing / delegated (+ detail)."""
    project = P.get(pid)
    m = _manifest(project)
    model = get_model(pid, probe=probe)
    ctrl = model["controller"]
    managed = set((ctrl or {}).get("manages") or [])
    out = []
    for need in model["needs"]:
        if need in managed and need != "controller":
            state, detail = "delegated", f"gere par {ctrl.get('name') or 'la console du projet'}"
        else:
            state, detail = _need_state(pid, project, m, need)
        out.append({"need": need, "label": NEEDS[need]["label"], "state": state, "detail": detail})
    done = sum(1 for x in out if x["state"] in ("ok", "delegated"))
    return {"profile": model["profile"], "label": model["label"], "kind": model["kind"],
            "controller": ctrl, "needs": out, "done": done, "total": len(out),
            "missing": [x for x in out if x["state"] == "missing"]}
