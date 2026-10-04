"""DevPilot — a project made of several repos of one GitHub organisation
(e.g. MarocDefender: md_console, md_infra, 5 services, landing...).

The project is linked to the ORGANISATION. DevPilot lists its repos, finds
where each one is already cloned inside the project folder (by its origin,
at any depth: md_infra/marocdefender/md-backend is md_backend), shows their
state, clones a missing one on request, and pulls all clean ones at once.
The map "repo -> place(s)" is kept in .devpilot/project.json (github.repos).
"""

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import db
import projects as P
from projects import ProjectError, git

_ORG_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:github\.com/)?(?:orgs/)?(?P<org>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))"
                     r"(?:/(?:repositories|people|teams|projects|packages|settings)?)?/?$")


def parse_org(text):
    m = _ORG_RE.match((text or "").strip())
    return m.group("org") if m else None


# ── GitHub side ─────────────────────────────────────────────────────────────

def list_repos(org):
    """Repos of an organisation (or user) via gh; falls back to the API with the DevPilot token."""
    try:
        r = subprocess.run(["gh", "repo", "list", org, "--limit", "200", "--json",
                            "name,description,defaultBranchRef,isPrivate,isArchived,sshUrl,url"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return sorted(({"name": x["name"], "description": x.get("description") or "",
                            "default_branch": (x.get("defaultBranchRef") or {}).get("name") or "main",
                            "private": x.get("isPrivate", False), "archived": x.get("isArchived", False),
                            "ssh": x["sshUrl"], "web": x["url"]} for x in json.loads(r.stdout or "[]")),
                          key=lambda x: x["name"].lower())
        err = r.stderr.strip()
    except FileNotFoundError:
        err = "gh n'est pas installe"
    except subprocess.TimeoutExpired:
        err = "gh : timeout"
    token = db.get_setting("github_token", "")
    if token:
        import requests
        out, page = [], 1
        while True:
            resp = requests.get(f"https://api.github.com/orgs/{org}/repos", params={"per_page": 100, "page": page},
                                headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if resp.status_code == 404:
                resp = requests.get(f"https://api.github.com/users/{org}/repos",
                                    params={"per_page": 100, "page": page},
                                    headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if resp.status_code != 200:
                break
            batch = resp.json()
            out += [{"name": x["name"], "description": x.get("description") or "",
                     "default_branch": x.get("default_branch") or "main", "private": x.get("private"),
                     "archived": x.get("archived"), "ssh": x["ssh_url"], "web": x["html_url"]} for x in batch]
            if len(batch) < 100:
                return sorted(out, key=lambda x: x["name"].lower())
            page += 1
    raise ProjectError(f"Impossible de lister github.com/{org} : {err or 'acces refuse'}. "
                       "Verifie le nom de l'organisation (gh auth status).")


# ── Local side ──────────────────────────────────────────────────────────────

def local_clones(path, org, depth=4):
    """{repo_name_lower: [relative paths]} for every clone of org/<repo> inside the folder."""
    out = {}
    for r in P.find_repos(path, depth=depth):
        gh = P.parse_github(r["remote"]) if r["remote"] else None
        if gh and gh[0].lower() == org.lower():
            out.setdefault(gh[1].lower(), []).append(r["dir"])
    return out


def _space_github(path):
    return (P.read_manifest(path).get("github") or {}) if path else {}


def _save_space_github(pid, data):
    project = P.get(pid)
    P.write_space(pid)
    m = P.read_manifest(project["path"])
    m["github"] = data
    P._write_json(P.space_dir(project["path"]) / "project.json", m)


def _auto_description(pid, org, total, here):
    """Keep a generated description true; never touch one the user wrote."""
    desc = (P.get(pid).get("description") or "").strip()
    if not desc or desc.startswith("Depots :") or desc.startswith("Organisation GitHub "):
        db.update_project(pid, description=f"Organisation GitHub {org} : {total} depots, {here} sur ce PC")


def link_org(pid, text):
    """Link a multi-repo project to its GitHub organisation."""
    project = P.get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        raise ProjectError("Le dossier du projet est introuvable")
    org = parse_org(text)
    if not org:
        raise ProjectError("Nom d'organisation invalide (ex. marocdefender ou github.com/marocdefender)")
    repos = list_repos(org)
    clones = local_clones(project["path"], org)
    old = _space_github(project["path"])
    data = {"org": org, "web": f"https://github.com/{org}",
            "roles": old.get("roles", {}) if old.get("org") == org else {},
            "places": {r["name"]: clones.get(r["name"].lower(), []) for r in repos}}
    _save_space_github(pid, data)
    db.update_project(pid, git_remote=data["web"])
    _auto_description(pid, org, len(repos), sum(1 for v in data["places"].values() if v))
    comp = db.get_project_component(pid, "git")
    cfg = {"org": org, "web": data["web"]}
    if comp:
        db.update_project_component(pid, "git", config={**(comp.get("config") or {}), **cfg}, enabled=True)
    else:
        db.add_project_component(pid, "git", enabled=True, config=cfg)
    found = sum(1 for v in data["places"].values() if v)
    return {"org": org, "repos": len(repos), "cloned": found,
            "message": f"Lie a github.com/{org} : {len(repos)} depots, {found} deja sur ton PC."}


def overview(pid, fetch=True):
    """Every repo of the organisation: role, place(s), state."""
    project = P.get(pid)
    gh = _space_github(project.get("path"))
    if not gh.get("org"):
        raise ProjectError("Ce projet n'est pas lie a une organisation GitHub")
    org, root = gh["org"], Path(project["path"])
    repos = list_repos(org)
    clones = local_clones(root, org)                  # re-scan: places follow reality

    def state(rel):
        path = root / rel if rel != "." else root
        if fetch:
            git(["fetch", "-q", "origin"], cwd=path, timeout=45)
        st = P.git_state(path)
        rc, up, _ = git(["rev-parse", "--abbrev-ref", "@{u}"], cwd=path)
        ahead = behind = 0
        if rc == 0:
            rc2, out, _ = git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=path)
            if rc2 == 0 and out:
                ahead, behind = (int(x) for x in out.split())
        return {"dir": rel, "path": str(path), "branch": st.get("branch", ""), "modified": st.get("modified", 0),
                "untracked": st.get("untracked", 0), "ahead": ahead, "behind": behind, "upstream": up if rc == 0 else None}

    places = [(r["name"], rel) for r in repos for rel in clones.get(r["name"].lower(), [])]
    with ThreadPoolExecutor(max_workers=8) as ex:
        states = dict(zip(places, ex.map(lambda x: state(x[1]), places)))
    out = []
    for r in repos:
        out.append({**{k: r[k] for k in ("name", "description", "private", "archived", "web", "default_branch")},
                    "role": gh.get("roles", {}).get(r["name"]) or r["description"],
                    "clones": [states[(r["name"], rel)] for rel in clones.get(r["name"].lower(), [])]})
    _auto_description(pid, org, len(repos), sum(1 for r in repos if clones.get(r["name"].lower())))
    if {r["name"]: clones.get(r["name"].lower(), []) for r in repos} != gh.get("places"):
        gh["places"] = {r["name"]: clones.get(r["name"].lower(), []) for r in repos}
        _save_space_github(pid, gh)
    return {"org": org, "web": gh["web"], "repos": out}


def set_role(pid, name, role):
    project = P.get(pid)
    gh = _space_github(project["path"])
    if not gh.get("org"):
        raise ProjectError("Ce projet n'est pas lie a une organisation GitHub")
    gh.setdefault("roles", {})[name] = (role or "").strip()
    _save_space_github(pid, gh)
    return {"name": name, "role": gh["roles"][name]}


def clone_repo(pid, name, dest=None):
    """Clone a missing repo of the organisation inside the project folder."""
    project = P.get(pid)
    gh = _space_github(project["path"])
    if not gh.get("org"):
        raise ProjectError("Ce projet n'est pas lie a une organisation GitHub")
    repo = next((r for r in list_repos(gh["org"]) if r["name"] == name), None)
    if not repo:
        raise ProjectError(f"{name} n'existe pas dans github.com/{gh['org']}")
    root = Path(project["path"]).resolve()
    target = (root / (dest or name)).resolve()
    if target == root or not target.is_relative_to(root):
        raise ProjectError("La place choisie doit etre dans le dossier du projet")
    if target.exists() and any(target.iterdir()):
        raise ProjectError(f"{target.relative_to(root)} existe deja et n'est pas vide")
    target.parent.mkdir(parents=True, exist_ok=True)
    created = not target.exists()
    rc, _, err = git(["clone", "--", repo["ssh"], str(target)], timeout=900)
    if rc != 0:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise ProjectError("Clone echoue : " + P._explain_git_error(err))
    rel = str(target.relative_to(root))
    gh.setdefault("places", {}).setdefault(name, [])
    if rel not in gh["places"][name]:
        gh["places"][name].append(rel)
    _save_space_github(pid, gh)
    return {"name": name, "dir": rel, "message": f"{name} clone dans {rel}"}


def pull_all(pid):
    """git pull --ff-only in every clean clone. Never touches a repo with changes."""
    ov = overview(pid, fetch=True)
    report = []
    for r in ov["repos"]:
        for c in r["clones"]:
            path, label = c["path"], f'{r["name"]} ({c["dir"]})'
            if c["modified"] or c["untracked"]:
                report.append({"repo": label, "result": "ignore", "detail": "changements locaux : utilise Synchroniser"})
            elif not c["upstream"]:
                report.append({"repo": label, "result": "ignore", "detail": "branche pas suivie sur GitHub"})
            elif c["ahead"] and c["behind"]:
                report.append({"repo": label, "result": "ignore", "detail": "divergent : utilise Synchroniser"})
            elif not c["behind"]:
                report.append({"repo": label, "result": "a jour", "detail": ""})
            else:
                rc, _, err = git(["pull", "--ff-only", "-q"], cwd=path, timeout=300)
                report.append({"repo": label, "result": "recupere" if rc == 0 else "echec",
                               "detail": f'{c["behind"]} commit(s)' if rc == 0 else err[-200:]})
    return {"report": report}
