"""DevPilot — the phases of a project and what to do in each one.

Every site goes through: preparation → development → hosting → go-live →
production. Each phase has steps; which steps exist depends on the project's
needs (model.py). DevPilot tells where the project is and what the next
step is. Nothing blocks: a step can be done later (skipped), manual steps
are ticked by the user, custom steps can be added, steps managed by the
project's console are delegated, and a phase with no step disappears.

State in .devpilot/project.json "phases":
  {"skipped": [keys], "manual": {key: date}, "custom": [{key, phase, label, done}]}
Every check is defensive: a missing folder, repo or connection is just "todo".
"""

import os
import uuid
from datetime import datetime

import db
import projects as P
from projects import ProjectError

PHASES = [
    {"key": "prepare", "label": "Preparation",   "desc": "Le projet existe : type, GitHub, brief"},
    {"key": "dev",     "label": "Developpement", "desc": "Le code se construit et vit sur GitHub"},
    {"key": "hosting", "label": "Hebergement",   "desc": "Ou le site va tourner : serveur, base, stockage, email"},
    {"key": "golive",  "label": "Mise en ligne", "desc": "Le domaine pointe, le site repond en HTTPS, l'email arrive"},
    {"key": "prod",    "label": "Production",    "desc": "Le site tourne : sauvegardes, suivi, livraison"},
]

# The catalogue. need = the step exists only if the need is in the project's
# model (None = always). manual = the user ticks it. action = where it is done.
STEPS = [
    {"key": "type",         "phase": "prepare", "need": None,       "manual": False, "label": "Definir le type du projet et ses besoins",
     "hint": "Le type donne les etapes de cette feuille de route.", "action": "model"},
    {"key": "github",       "phase": "prepare", "need": "github",   "manual": False, "label": "Lier le projet a GitHub",
     "hint": "Le code source est sauvegarde et partage.", "action": "github"},
    {"key": "brief",        "phase": "prepare", "need": None,       "manual": False, "label": "Ecrire le brief du projet (prompt)",
     "hint": "Ce que le client veut : Claude et toi travaillez a partir de ce texte.", "action": "prompt"},

    {"key": "first_commit", "phase": "dev",     "need": "github",   "manual": False, "label": "Premier code commite",
     "hint": "Le depot a au moins un commit.", "action": "git"},
    {"key": "pushed",       "phase": "dev",     "need": "github",   "manual": False, "label": "Code pousse sur GitHub",
     "hint": "Rien en attente d'envoi : GitHub a la meme version que ton dossier.", "action": "git"},
    {"key": "docker_dev",   "phase": "dev",     "need": "docker",   "manual": False, "label": "Stack Docker de developpement",
     "hint": "Un docker-compose dans le projet pour tourner en local.", "action": "terminal"},
    {"key": "dev_done",     "phase": "dev",     "need": None,       "manual": True,  "label": "Site pret a etre mis en ligne",
     "hint": "Tu estimes que la version actuelle peut partir en production.", "action": "git"},

    {"key": "hosting",      "phase": "hosting", "need": "hosting",  "manual": False, "label": "Choisir l'hebergement",
     "hint": "Un serveur (OVH, Hetzner...) ou une plateforme (Vercel, Netlify...). C'est lui que le domaine visera.", "action": "hosting"},
    {"key": "database",     "phase": "hosting", "need": "database", "manual": False, "label": "Base de donnees : choisir ou elle tourne et la connecter",
     "hint": "Sur le serveur (Docker), ou un service manage (Supabase, Neon, OVH...).", "action": "connections"},
    {"key": "storage",      "phase": "hosting", "need": "storage",  "manual": False, "label": "Stockage des fichiers (R2 / S3)",
     "hint": "Un bucket pour les medias et les uploads.", "action": "connections"},
    {"key": "email_provider", "phase": "hosting", "need": "email",  "manual": False, "label": "Choisir le fournisseur d'email pro",
     "hint": "Google Workspace, Zoho, OVH... Les adresses du client.", "action": "email"},
    {"key": "deployed",     "phase": "hosting", "need": "hosting",  "manual": True,  "label": "Premier deploiement sur l'hebergement",
     "hint": "Le site tourne sur le serveur ou la plateforme (meme sans domaine).", "action": "hosting"},

    {"key": "domain",       "phase": "golive",  "need": "domain",   "manual": False, "label": "Nom de domaine du client",
     "hint": "Achete chez un registrar (OVH, Namecheap...) et enregistre ici.", "action": "domain"},
    {"key": "dns",          "phase": "golive",  "need": "domain",   "manual": False, "label": "Faire pointer le DNS vers l'hebergement",
     "hint": "DevPilot donne les enregistrements exacts et verifie qu'ils sont en place.", "action": "domain"},
    {"key": "https",        "phase": "golive",  "need": "domain",   "manual": False, "label": "Site en ligne en HTTPS",
     "hint": "Le site repond sur https://<domaine> avec un certificat valide.", "action": "domain"},
    {"key": "mx",           "phase": "golive",  "need": "email",    "manual": False, "label": "Email : MX en place (verifies)",
     "hint": "Les emails du client arrivent chez le fournisseur choisi.", "action": "email"},

    {"key": "backup",       "phase": "prod",    "need": "backup",   "manual": False, "label": "Sauvegardes en place",
     "hint": "Base et fichiers copies regulierement (R2).", "action": "connections"},
    {"key": "monitoring",   "phase": "prod",    "need": "monitoring", "manual": False, "label": "Suivi du site (monitoring)",
     "hint": "Savoir quand le site tombe.", "action": "connections"},
    {"key": "handover",     "phase": "prod",    "need": None,       "manual": True,  "label": "Livraison au client",
     "hint": "Acces, identifiants, documentation remis au client.", "action": None},
]
STEP_BY_KEY = {s["key"]: s for s in STEPS}
# a software / API project has no go-live by domain: keep the catalogue but the
# steps vanish with the needs (no 'domain' need = no domain steps)


# ── State in the project folder ─────────────────────────────────────────────

def _state(project):
    m = P.read_manifest(project["path"]) if project.get("path") and os.path.isdir(project["path"]) else {}
    st = m.get("phases") if isinstance(m.get("phases"), dict) else {}
    return {"skipped": [k for k in st.get("skipped", []) if isinstance(k, str)],
            "manual": st.get("manual") if isinstance(st.get("manual"), dict) else {},
            "custom": [c for c in st.get("custom", []) if isinstance(c, dict) and c.get("key")]}


def _save_state(pid, st):
    project = P.get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        raise ProjectError("Le dossier du projet est introuvable")
    m = P.read_manifest(project["path"])
    m["phases"] = st
    P._write_json(P.space_dir(project["path"]) / "project.json", m)


# ── Automatic checks (never raise) ──────────────────────────────────────────

def _git_facts(project):
    """{repo: bool, commits: bool, pushed: bool|None, ahead: int} for the project folder (or its first repo)."""
    out = {"repo": False, "commits": False, "pushed": None, "ahead": 0}
    try:
        path = project.get("path")
        if not path or not os.path.isdir(path):
            return out
        repos = P.find_repos(path, depth=1)
        if not repos:
            return out
        repo = next((r for r in repos if r["dir"] == "."), repos[0])["path"]
        out["repo"] = True
        rc, out_, _ = P.git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo, timeout=10)
        out["commits"] = rc == 0
        if not out["commits"]:
            return out
        rc, up, _ = P.git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=repo, timeout=10)
        if rc != 0:
            out["pushed"] = False
            return out
        rc, cnt, _ = P.git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=repo, timeout=10)
        if rc == 0 and cnt:
            ahead, _behind = (int(x) for x in cnt.split())
            out["ahead"] = ahead
            out["pushed"] = ahead == 0
    except Exception:
        pass
    return out


def _has_compose(project):
    path = project.get("path")
    if not path or not os.path.isdir(path):
        return False
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "docker-compose.dev.yml"):
        if os.path.exists(os.path.join(path, name)):
            return True
    try:
        for d in os.listdir(path):
            if not d.startswith(".") and os.path.isdir(os.path.join(path, d)):
                for name in ("docker-compose.yml", "docker-compose.yaml", "docker-compose.dev.yml"):
                    if os.path.exists(os.path.join(path, d, name)):
                        return True
    except OSError:
        pass
    return False


def _cfg(pid, key):
    try:
        import connections
        return connections._cfg(pid, key)
    except Exception:
        return {}


def _auto_state(pid, project, step, needs, m):
    """(state, detail) for an automatic step: done | partial | todo."""
    k = step["key"]
    byneed = {x["need"]: x for x in needs}
    try:
        if k == "type":
            return ("done", m["label"]) if m["profile"] else ("todo", "aucun type choisi")
        if k == "github":
            n = byneed.get("github")
            return ("done", n["detail"]) if n and n["state"] == "ok" else ("todo", "pas encore lie")
        if k == "brief":
            specs = db.get_project_specs(pid) or {}
            return ("done", "brief present") if (specs.get("prompt") or "").strip() else ("todo", "pas de brief")
        if k == "first_commit":
            g = _git_facts(project)
            return ("done", "au moins un commit") if g["commits"] else ("todo", "aucun commit" if g["repo"] else "pas de depot")
        if k == "pushed":
            g = _git_facts(project)
            if not g["commits"]:
                return "todo", "rien a pousser encore"
            if g["pushed"] is None:
                return "todo", "etat inconnu"
            return ("done", "a jour avec GitHub") if g["pushed"] else ("partial", f"{g['ahead']} commit(s) a envoyer")
        if k == "docker_dev":
            return ("done", "docker-compose present") if _has_compose(project) else ("todo", "pas de docker-compose")
        if k == "hosting":
            n = byneed.get("hosting")
            if not n:
                return "todo", ""
            return {"ok": "done", "partial": "partial"}.get(n["state"], "todo"), n["detail"]
        if k == "database":
            c = _cfg(pid, "database")
            if c.get("host") or c.get("url") or c.get("connection_string") or c.get("provider"):
                return "done", c.get("provider") or c.get("type") or c.get("host") or "configuree"
            return ("partial", f"{c['type']} choisi, pas encore connectee") if c.get("type") else ("todo", "ou tourne la base ?")
        if k == "storage":
            c = _cfg(pid, "storage")
            return ("done", c.get("bucket")) if c.get("bucket") else ("todo", "pas de bucket")
        if k == "email_provider":
            c = _cfg(pid, "email")
            return ("done", c.get("provider")) if c.get("provider") else ("todo", "pas de fournisseur")
        if k == "domain":
            c = _cfg(pid, "domain")
            return ("done", c.get("domain")) if c.get("domain") else ("todo", "pas de domaine")
        if k == "dns":
            c = _cfg(pid, "domain")
            if not c.get("domain"):
                return "todo", "domaine d'abord"
            if c.get("dns_verified"):
                return "done", "DNS verifie"
            return ("partial", "DNS incorrect : voir les enregistrements") if c.get("dns_checked_at") else ("todo", "a verifier")
        if k == "https":
            c = _cfg(pid, "domain")
            if not c.get("domain"):
                return "todo", "domaine d'abord"
            if c.get("site_ok") and (c.get("site_result") or {}).get("https", True):
                return "done", "site en ligne en HTTPS"
            if c.get("site_ok"):
                return "partial", "en ligne mais pas en HTTPS"
            return ("partial", "site hors ligne") if c.get("site_checked_at") else ("todo", "a verifier")
        if k == "mx":
            c = _cfg(pid, "email")
            if c.get("mx_verified"):
                return "done", "MX verifies"
            return ("partial", "MX incorrects") if c.get("mx_checked_at") else ("todo", "a verifier")
        if k == "backup":
            c = _cfg(pid, "backup")
            if c.get("bucket") or c.get("destination") or c.get("frequency"):
                return "done", c.get("frequency") or c.get("bucket") or "configurees"
            try:
                if db.get_last_sync(pid, "r2", "backup"):
                    return "done", "derniere sauvegarde faite"
            except Exception:
                pass
            return "todo", "pas de sauvegarde"
        if k == "monitoring":
            c = _cfg(pid, "monitoring")
            return ("done", "configure") if c else ("todo", "pas de suivi")
    except Exception as e:                 # a check must never take the roadmap down
        return "todo", f"verification impossible ({type(e).__name__})"
    return "todo", ""


# ── The roadmap ─────────────────────────────────────────────────────────────

def roadmap(pid):
    """Phases → steps with their state; the current phase and the next step."""
    import model as M
    project = P.get(pid)
    try:
        status = M.connections_status(pid, probe=False)
    except Exception:
        status = {"profile": "", "label": "Non defini", "needs": [], "controller": None}
    needs = status["needs"]
    need_keys = {x["need"] for x in needs}
    ctrl = status.get("controller")
    managed = set((ctrl or {}).get("manages") or []) if ctrl else set()
    st = _state(project)
    m = {"profile": status["profile"], "label": status["label"]}

    phases = []
    for ph in PHASES:
        steps = []
        for s in [x for x in STEPS if x["phase"] == ph["key"]]:
            if s["need"] and s["need"] not in need_keys:
                continue
            k = s["key"]
            entry = {"key": k, "label": s["label"], "hint": s["hint"], "action": s["action"], "manual": s["manual"],
                     "need": s["need"], "custom": False, "skipped": k in st["skipped"], "detail": ""}
            if s["need"] and s["need"] in managed:
                entry.update(state="delegated", detail=f"gere par {ctrl.get('name') or 'la console'}")
            elif s["manual"]:
                done_at = st["manual"].get(k)
                entry.update(state="done" if done_at else "todo", detail=("fait le " + done_at[:10]) if done_at else "a cocher quand c'est fait")
            else:
                state, detail = _auto_state(pid, project, s, needs, m)
                entry.update(state=state, detail=detail)
            steps.append(entry)
        for c in st["custom"]:
            if c.get("phase") == ph["key"]:
                steps.append({"key": c["key"], "label": c.get("label", "Etape"), "hint": c.get("hint", ""), "action": None, "manual": True,
                              "need": None, "custom": True, "skipped": c["key"] in st["skipped"],
                              "state": "done" if c.get("done") else "todo", "detail": ("fait le " + c["done"][:10]) if c.get("done") else ""})
        if not steps:
            continue                                # a phase with nothing to do is not shown
        open_steps = [x for x in steps if x["state"] in ("todo", "partial") and not x["skipped"]]
        done = sum(1 for x in steps if x["state"] in ("done", "delegated") or x["skipped"])
        phases.append({"key": ph["key"], "label": ph["label"], "desc": ph["desc"], "steps": steps,
                       "done": done, "total": len(steps), "open": len(open_steps),
                       "state": "done" if not open_steps else "upcoming"})

    current = next((p for p in phases if p["open"]), None)
    for p in phases:
        if current and p["key"] == current["key"]:
            p["state"] = "current"
    next_step = None
    if current:
        next_step = next((x for x in current["steps"] if x["state"] in ("todo", "partial") and not x["skipped"]), None)
    total = sum(p["total"] for p in phases)
    done = sum(p["done"] for p in phases)
    return {"profile": status["profile"], "label": status["label"], "phases": phases,
            "current": current["key"] if current else ("prod" if phases else None),
            "current_label": current["label"] if current else ("Production" if phases else ""),
            "next": next_step, "done": done, "total": total, "complete": bool(phases) and current is None}


# ── Acting on steps ─────────────────────────────────────────────────────────

def _known_step(pid, key):
    st = _state(P.get(pid))
    if key in STEP_BY_KEY or any(c["key"] == key for c in st["custom"]):
        return st
    raise ProjectError(f"Etape inconnue : {key}")


def skip(pid, key, later=True):
    """later=True: do it later (not blocking); later=False: take it back."""
    st = _known_step(pid, key)
    if later and key not in st["skipped"]:
        st["skipped"].append(key)
    if not later:
        st["skipped"] = [k for k in st["skipped"] if k != key]
    _save_state(pid, st)
    return roadmap(pid)


def mark(pid, key, done=True):
    """Tick a manual (or custom) step. Automatic steps reflect reality: they can't be ticked."""
    st = _known_step(pid, key)
    custom = next((c for c in st["custom"] if c["key"] == key), None)
    if custom is not None:
        custom["done"] = datetime.now().isoformat(timespec="seconds") if done else None
    else:
        if not STEP_BY_KEY[key]["manual"]:
            raise ProjectError("Cette etape se verifie toute seule : fais l'action, DevPilot la verra")
        if done:
            st["manual"][key] = datetime.now().isoformat(timespec="seconds")
        else:
            st["manual"].pop(key, None)
    _save_state(pid, st)
    return roadmap(pid)


def add_custom(pid, phase, label, hint=""):
    if phase not in {p["key"] for p in PHASES}:
        raise ProjectError(f"Phase inconnue : {phase}")
    label = (label or "").strip()[:120]
    if not label:
        raise ProjectError("Donne un nom a l'etape")
    st = _state(P.get(pid))
    st["custom"].append({"key": "c-" + uuid.uuid4().hex[:8], "phase": phase, "label": label, "hint": (hint or "").strip()[:300], "done": None})
    _save_state(pid, st)
    return roadmap(pid)


def remove_custom(pid, key):
    st = _state(P.get(pid))
    if not any(c["key"] == key for c in st["custom"]):
        raise ProjectError("Etape personnalisee introuvable")
    st["custom"] = [c for c in st["custom"] if c["key"] != key]
    st["skipped"] = [k for k in st["skipped"] if k != key]
    _save_state(pid, st)
    return roadmap(pid)


def summary(pid):
    """For lists: current phase + progress, cheap and never raising."""
    try:
        r = roadmap(pid)
        return {"current": r["current"], "current_label": r["current_label"], "done": r["done"], "total": r["total"],
                "next": r["next"]["label"] if r["next"] else None, "complete": r["complete"]}
    except Exception:
        return None
