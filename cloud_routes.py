"""DevPilot — Cloud integration API routes (Flask Blueprint)."""

import os
import re
import json
from pathlib import Path
from flask import Blueprint, jsonify, request

import db
import github_ops
import cloudflare_r2
import cloud_sync

cloud_bp = Blueprint("cloud", __name__)
HOME = Path.home()
DEVPILOT_PROJECTS = HOME / "devpilot" / "projects"


def _fmt(b):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


# ═══════════════════════════════════════════════════════════════════════════
# GITHUB API ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@cloud_bp.route("/api/github/test")
def api_github_test():
    token = db.get_setting("github_token", "")
    if not token:
        return jsonify({"connected": False, "message": "Pas de token configure"})
    return jsonify(github_ops.github_test_connection(token))


@cloud_bp.route("/api/github/repos")
def api_github_repos():
    token = db.get_setting("github_token", "")
    if not token:
        return jsonify([])
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    sort = request.args.get("sort", "updated")
    visibility = request.args.get("visibility")
    return jsonify(github_ops.github_list_repos(token, page, per_page, sort, visibility))


@cloud_bp.route("/api/github/repos", methods=["POST"])
def api_github_create_repo():
    token = db.get_setting("github_token", "")
    if not token:
        return jsonify({"success": False, "message": "Pas de token GitHub"})
    data = request.json
    result = github_ops.github_create_repo(
        token,
        name=data.get("name", ""),
        private=data.get("private", True),
        description=data.get("description", ""),
    )
    return jsonify(result)


@cloud_bp.route("/api/github/repos/<owner>/<repo>/branches")
def api_github_branches(owner, repo):
    token = db.get_setting("github_token", "")
    if not token:
        return jsonify([])
    return jsonify(github_ops.github_list_branches(token, owner, repo))


@cloud_bp.route("/api/github/repos/<owner>/<repo>/commits")
def api_github_commits(owner, repo):
    token = db.get_setting("github_token", "")
    if not token:
        return jsonify([])
    branch = request.args.get("branch", "main")
    limit = request.args.get("limit", 20, type=int)
    return jsonify(github_ops.github_list_commits(token, owner, repo, branch, limit))


@cloud_bp.route("/api/github/clone", methods=["POST"])
def api_github_clone():
    """Clone a GitHub repo using stored token for private repos."""
    token = db.get_setting("github_token", "")
    repo_url = request.json.get("url", "").strip()
    name = request.json.get("name", "").strip()

    if not repo_url:
        return jsonify({"success": False, "message": "URL requise"})

    if not name:
        name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")

    clone_path = str(DEVPILOT_PROJECTS / name)
    if os.path.exists(clone_path):
        return jsonify({"success": False, "message": f"Le dossier {name} existe deja"})

    ok, output = github_ops.git_clone(repo_url, clone_path, token)

    if not ok or not os.path.exists(clone_path):
        return jsonify({"success": False, "message": f"Clone echoue: {output}"})

    # Detect color
    tech_colors = {
        "package.json": "#fbbf24", "pubspec.yaml": "#38bdf8",
        "requirements.txt": "#34d399", "pyproject.toml": "#34d399",
        "build.gradle": "#06b6d4", "Cargo.toml": "#fb923c", "go.mod": "#60a5fa",
    }
    color = "#8b5cf6"
    for mf, c in tech_colors.items():
        if (Path(clone_path) / mf).exists():
            color = c
            break

    pid = db.create_project(name=name, color=color, path=clone_path, git_remote=repo_url, description="")
    db.add_rule(pattern=re.escape(name.lower()), project_id=pid, resource_type="any")

    return jsonify({"success": True, "message": f"{name} clone", "path": clone_path, "project_id": pid})


# ═══════════════════════════════════════════════════════════════════════════
# GIT OPERATIONS (per project)
# ═══════════════════════════════════════════════════════════════════════════

def _get_project_path(pid):
    """Get project path or return error response."""
    project = db.get_project(pid)
    if not project:
        return None, jsonify({"success": False, "message": "Projet introuvable"}), 404
    ppath = project.get("path", "")
    if not ppath or not os.path.isdir(ppath):
        return None, jsonify({"success": False, "message": "Dossier projet introuvable"}), 404
    return ppath, None, None


@cloud_bp.route("/api/projects/<int:pid>/git/status")
def api_git_status(pid):
    ppath, err, code = _get_project_path(pid)
    if err:
        return err, code
    status = github_ops.git_status_detailed(ppath)
    if not status:
        return jsonify({"error": "Pas un depot Git"})
    return jsonify(status)


@cloud_bp.route("/api/projects/<int:pid>/git/pull", methods=["POST"])
def api_git_pull(pid):
    ppath, err, code = _get_project_path(pid)
    if err:
        return err, code
    remote = (request.json or {}).get("remote", "origin")
    branch = (request.json or {}).get("branch", "")
    ok, output = github_ops.git_pull(ppath, remote, branch)
    if ok:
        db.log_event("synced", "github", db.get_project(pid)["name"], ppath, 0, pid, details="Git pull")
        db.update_project_activity(pid)
    return jsonify({"success": ok, "output": output})


@cloud_bp.route("/api/projects/<int:pid>/git/push", methods=["POST"])
def api_git_push(pid):
    ppath, err, code = _get_project_path(pid)
    if err:
        return err, code
    data = request.json or {}
    remote = data.get("remote", "origin")
    branch = data.get("branch", "")
    set_upstream = data.get("set_upstream", False)
    ok, output = github_ops.git_push(ppath, remote, branch, set_upstream)
    if ok:
        db.log_sync(pid, "push", "github", "source", status="success", details=output)
        db.log_event("synced", "github", db.get_project(pid)["name"], ppath, 0, pid, details="Git push")
        db.update_project_activity(pid)
    return jsonify({"success": ok, "output": output})


@cloud_bp.route("/api/projects/<int:pid>/git/commit", methods=["POST"])
def api_git_commit(pid):
    ppath, err, code = _get_project_path(pid)
    if err:
        return err, code
    data = request.json or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"success": False, "message": "Message de commit requis"})
    add_all = data.get("add_all", False)
    files = data.get("files")
    ok, output = github_ops.git_commit(ppath, message, add_all, files)
    if ok:
        db.log_event("committed", "github", db.get_project(pid)["name"], ppath, 0, pid, details=message)
        db.update_project_activity(pid)
    return jsonify({"success": ok, "output": output})


@cloud_bp.route("/api/projects/<int:pid>/git/checkout", methods=["POST"])
def api_git_checkout(pid):
    ppath, err, code = _get_project_path(pid)
    if err:
        return err, code
    data = request.json or {}
    branch = data.get("branch", "").strip()
    if not branch:
        return jsonify({"success": False, "message": "Nom de branche requis"})
    create = data.get("create", False)
    ok, output = github_ops.git_checkout(ppath, branch, create)
    return jsonify({"success": ok, "output": output})


@cloud_bp.route("/api/projects/<int:pid>/git/log")
def api_git_log(pid):
    ppath, err, code = _get_project_path(pid)
    if err:
        return err, code
    limit = request.args.get("limit", 20, type=int)
    return jsonify(github_ops.git_log(ppath, limit))


@cloud_bp.route("/api/projects/<int:pid>/git/stash", methods=["POST"])
def api_git_stash(pid):
    ppath, err, code = _get_project_path(pid)
    if err:
        return err, code
    pop = (request.json or {}).get("pop", False)
    ok, output = github_ops.git_stash(ppath, pop)
    return jsonify({"success": ok, "output": output})


# ═══════════════════════════════════════════════════════════════════════════
# CLOUDFLARE R2 ROUTES
# ═══════════════════════════════════════════════════════════════════════════

def _get_r2_client():
    """Get R2 client from stored settings."""
    account_id = db.get_setting("r2_account_id", "")
    access_key = db.get_setting("r2_access_key_id", "")
    secret_key = db.get_setting("r2_secret_access_key", "")
    if not account_id or not access_key or not secret_key:
        return None
    return cloudflare_r2.r2_get_client(account_id, access_key, secret_key)


@cloud_bp.route("/api/r2/test")
def api_r2_test():
    account_id = db.get_setting("r2_account_id", "")
    access_key = db.get_setting("r2_access_key_id", "")
    secret_key = db.get_setting("r2_secret_access_key", "")
    return jsonify(cloudflare_r2.r2_test_connection(account_id, access_key, secret_key))


@cloud_bp.route("/api/r2/buckets")
def api_r2_buckets():
    client = _get_r2_client()
    if not client:
        return jsonify([])
    return jsonify(cloudflare_r2.r2_list_buckets(client))


@cloud_bp.route("/api/r2/buckets", methods=["POST"])
def api_r2_create_bucket():
    client = _get_r2_client()
    if not client:
        return jsonify({"success": False, "message": "R2 non configure"})
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"success": False, "message": "Nom de bucket requis"})
    return jsonify(cloudflare_r2.r2_create_bucket(client, name))


@cloud_bp.route("/api/r2/buckets/<name>/objects")
def api_r2_objects(name):
    client = _get_r2_client()
    if not client:
        return jsonify([])
    prefix = request.args.get("prefix", "")
    max_keys = request.args.get("max_keys", 200, type=int)
    return jsonify(cloudflare_r2.r2_list_objects(client, name, prefix, max_keys))


@cloud_bp.route("/api/r2/upload", methods=["POST"])
def api_r2_upload():
    client = _get_r2_client()
    if not client:
        return jsonify({"success": False, "message": "R2 non configure"})
    data = request.json or {}
    bucket = data.get("bucket", "").strip()
    local_path = data.get("local_path", "").strip()
    remote_key = data.get("remote_key", "").strip()

    if not all([bucket, local_path, remote_key]):
        return jsonify({"success": False, "message": "bucket, local_path et remote_key requis"})

    # Security: only allow uploads from home directory
    if not local_path.startswith(str(HOME)):
        return jsonify({"success": False, "message": "Chemin non autorise"})

    result = cloudflare_r2.r2_upload_file(client, bucket, local_path, remote_key)
    return jsonify(result)


@cloud_bp.route("/api/r2/download", methods=["POST"])
def api_r2_download():
    client = _get_r2_client()
    if not client:
        return jsonify({"success": False, "message": "R2 non configure"})
    data = request.json or {}
    bucket = data.get("bucket", "").strip()
    remote_key = data.get("remote_key", "").strip()
    local_path = data.get("local_path", "").strip()

    if not all([bucket, remote_key, local_path]):
        return jsonify({"success": False, "message": "bucket, remote_key et local_path requis"})

    if not local_path.startswith(str(HOME)):
        return jsonify({"success": False, "message": "Chemin non autorise"})

    result = cloudflare_r2.r2_download_file(client, bucket, remote_key, local_path)
    return jsonify(result)


@cloud_bp.route("/api/r2/objects", methods=["DELETE"])
def api_r2_delete_object():
    client = _get_r2_client()
    if not client:
        return jsonify({"success": False, "message": "R2 non configure"})
    data = request.json or {}
    bucket = data.get("bucket", "").strip()
    key = data.get("key", "").strip()
    if not bucket or not key:
        return jsonify({"success": False, "message": "bucket et key requis"})
    return jsonify(cloudflare_r2.r2_delete_object(client, bucket, key))


@cloud_bp.route("/api/r2/presign")
def api_r2_presign():
    client = _get_r2_client()
    if not client:
        return jsonify({"success": False, "message": "R2 non configure"})
    bucket = request.args.get("bucket", "")
    key = request.args.get("key", "")
    expires = request.args.get("expires", 3600, type=int)
    if not bucket or not key:
        return jsonify({"success": False, "message": "bucket et key requis"})
    return jsonify(cloudflare_r2.r2_get_presigned_url(client, bucket, key, expires))


# ═══════════════════════════════════════════════════════════════════════════
# CLOUD SYNC ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@cloud_bp.route("/api/projects/<int:pid>/cloud/summary")
def api_cloud_summary(pid):
    return jsonify(cloud_sync.get_project_cloud_summary(pid))


@cloud_bp.route("/api/projects/<int:pid>/cloud/sync-status")
def api_cloud_sync_status(pid):
    return jsonify(cloud_sync.get_sync_status(pid))


@cloud_bp.route("/api/projects/<int:pid>/cloud/sync", methods=["POST"])
def api_cloud_sync(pid):
    data = request.json or {}
    backend = data.get("backend", "github")
    message = data.get("message", "")

    if backend == "github":
        return jsonify(cloud_sync.sync_project_to_github(pid, message=message, add_all=data.get("add_all", True)))
    elif backend == "r2":
        asset_type = data.get("asset_type")
        return jsonify(cloud_sync.sync_project_to_r2(pid, asset_type=asset_type))
    else:
        return jsonify({"success": False, "message": f"Backend inconnu: {backend}"})


@cloud_bp.route("/api/projects/<int:pid>/cloud/backup", methods=["POST"])
def api_cloud_backup(pid):
    data = request.json or {}
    asset_type = data.get("asset_type")
    return jsonify(cloud_sync.sync_project_to_r2(pid, asset_type=asset_type))


@cloud_bp.route("/api/projects/<int:pid>/cloud/restore", methods=["POST"])
def api_cloud_restore(pid):
    data = request.json or {}
    asset_type = data.get("asset_type")
    return jsonify(cloud_sync.restore_from_r2(pid, asset_type=asset_type))


@cloud_bp.route("/api/projects/<int:pid>/cloud/history")
def api_cloud_history(pid):
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_sync_history(pid, limit))


@cloud_bp.route("/api/cloud/history")
def api_cloud_history_global():
    """Global sync history across all projects."""
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_sync_history(limit=limit))


@cloud_bp.route("/api/cloud/overview")
def api_cloud_overview():
    """Global cloud overview: all projects' sync status."""
    projects = db.get_projects()
    projects = [p for p in projects if p.get("path")]

    github_token = db.get_setting("github_token", "")
    r2_account = db.get_setting("r2_account_id", "")

    overview = []
    for p in projects:
        last_push = db.get_last_sync(p["id"], "github", "push")
        last_backup = db.get_last_sync(p["id"], "r2", "backup")
        r2_stats = db.get_r2_storage_stats(p["id"])
        rules_count = len(db.get_asset_rules(p["id"]))

        # Quick git status
        git_clean = None
        pending = 0
        if p.get("path") and (Path(p["path"]) / ".git").exists():
            status = github_ops.git_status_detailed(p["path"])
            if status:
                git_clean = status["is_clean"]
                pending = status["modified"] + status["untracked"] + status["staged"]

        overview.append({
            "id": p["id"],
            "name": p["name"],
            "color": p.get("color", "#8b5cf6"),
            "status": p.get("status", "active"),
            "git_remote": p.get("git_remote", ""),
            "git_clean": git_clean,
            "pending_changes": pending,
            "last_push": last_push["started_at"] if last_push else None,
            "last_backup": last_backup["started_at"] if last_backup else None,
            "r2_objects": r2_stats.get("count", 0) if isinstance(r2_stats, dict) else 0,
            "r2_size": r2_stats.get("total_size", 0) if isinstance(r2_stats, dict) else 0,
            "r2_size_h": _fmt(r2_stats.get("total_size", 0) if isinstance(r2_stats, dict) else 0),
            "rules_count": rules_count,
        })

    return jsonify({
        "projects": overview,
        "github_connected": bool(github_token),
        "r2_configured": bool(r2_account),
    })


# ═══════════════════════════════════════════════════════════════════════════
# ASSET RULES ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@cloud_bp.route("/api/projects/<int:pid>/assets/rules")
def api_asset_rules(pid):
    return jsonify(db.get_asset_rules(pid))


@cloud_bp.route("/api/projects/<int:pid>/assets/rules", methods=["POST"])
def api_add_asset_rule(pid):
    data = request.json or {}
    required = ["asset_type", "storage_backend", "glob_pattern"]
    if not all(data.get(k) for k in required):
        return jsonify({"success": False, "message": "asset_type, storage_backend et glob_pattern requis"})
    try:
        rid = db.add_asset_rule(
            project_id=pid,
            asset_type=data["asset_type"],
            storage_backend=data["storage_backend"],
            glob_pattern=data["glob_pattern"],
            r2_bucket=data.get("r2_bucket", ""),
            r2_prefix=data.get("r2_prefix", ""),
            is_public=data.get("is_public", False),
            auto_sync=data.get("auto_sync", False),
        )
        return jsonify({"success": True, "id": rid})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@cloud_bp.route("/api/projects/<int:pid>/assets/rules/<int:rid>", methods=["PUT"])
def api_update_asset_rule(pid, rid):
    data = request.json or {}
    db.update_asset_rule(rid, **data)
    return jsonify({"success": True})


@cloud_bp.route("/api/projects/<int:pid>/assets/rules/<int:rid>", methods=["DELETE"])
def api_delete_asset_rule(pid, rid):
    db.delete_asset_rule(rid)
    return jsonify({"success": True})


@cloud_bp.route("/api/projects/<int:pid>/assets/scan")
def api_scan_assets(pid):
    return jsonify(cloud_sync.scan_project_assets(pid))


@cloud_bp.route("/api/projects/<int:pid>/assets/apply-defaults", methods=["POST"])
def api_apply_default_rules(pid):
    r2_bucket = (request.json or {}).get("r2_bucket", db.get_setting("r2_default_bucket", ""))
    # Filter rules by active components if any are configured
    active = [c["component"] for c in db.get_project_components(pid) if c["enabled"]]
    applied = cloud_sync.apply_default_rules(pid, r2_bucket, components=active if active else None)
    return jsonify({"success": True, "applied": applied, "message": f"{applied} regles appliquees"})


# ═══════════════════════════════════════════════════════════════════════════
# CONNECTIONS — unified view of all linked services
# ═══════════════════════════════════════════════════════════════════════════

CONNECTION_TYPES = {
    "github":     {"label": "GitHub",         "icon": "branch",   "color": "#8b5cf6"},
    "source":     {"label": "Code source",    "icon": "folder",   "color": "#62627a"},
    "storage":    {"label": "Cloudflare R2",  "icon": "cloud",    "color": "#38bdf8"},
    "database":   {"label": "Base de données","icon": "database",  "color": "#22d3a7"},
    "deploy":     {"label": "Déploiement",    "icon": "rocket",   "color": "#f59e0b"},
    "domain":     {"label": "Domaine",        "icon": "globe",    "color": "#60a5fa"},
    "server":     {"label": "Serveur",        "icon": "server",   "color": "#fbbf24"},
    "backup":     {"label": "Backup",         "icon": "shield",   "color": "#22d3a7"},
    "auth":       {"label": "Auth",           "icon": "lock",     "color": "#a78bfa"},
    "monitoring": {"label": "Monitoring",     "icon": "activity",  "color": "#f43f5e"},
    "docker":     {"label": "Docker",         "icon": "container", "color": "#38bdf8"},
    "ci_cd":      {"label": "CI/CD",          "icon": "refresh",   "color": "#f59e0b"},
}

DB_LABELS = {"postgres": "PostgreSQL", "mysql": "MySQL", "mongodb": "MongoDB",
             "sqlite": "SQLite", "supabase": "Supabase", "prisma": "Prisma", "neon": "Neon", "unknown": "Database"}

CONNECTION_FIELDS = {
    "github": [
        {"key": "url", "label": "URL du repo", "placeholder": "https://github.com/user/repo", "required": True},
    ],
    "deploy": [
        {"key": "platform", "label": "Plateforme", "placeholder": "vercel, netlify, railway, cloudflare"},
        {"key": "site_url", "label": "URL du site", "placeholder": "https://mon-site.vercel.app"},
        {"key": "project_url", "label": "URL dashboard", "placeholder": "https://vercel.com/user/projet"},
        {"key": "deploy_hook", "label": "Deploy hook (webhook)", "placeholder": "https://api.vercel.com/v1/...", "sensitive": True},
        {"key": "domain", "label": "Domaine", "placeholder": "example.com"},
        {"key": "dns", "label": "Provider DNS", "placeholder": "cloudflare, namecheap, ovh"},
        {"key": "ssl", "label": "SSL", "placeholder": "letsencrypt, cloudflare"},
    ],
    "database": [
        {"key": "provider", "label": "Provider", "placeholder": "supabase, neon, planetscale, local"},
        {"key": "host", "label": "Host", "placeholder": "db.xyz.supabase.co"},
        {"key": "port", "label": "Port", "placeholder": "5432"},
        {"key": "name", "label": "Nom de la BDD", "placeholder": "postgres"},
        {"key": "user", "label": "Utilisateur", "placeholder": "postgres"},
        {"key": "connection_string", "label": "Connection string", "placeholder": "postgresql://user:pass@host:5432/db", "sensitive": True},
        {"key": "dashboard_url", "label": "URL dashboard", "placeholder": "https://supabase.com/dashboard/project/xyz"},
    ],
    "storage": [
        {"key": "bucket", "label": "Nom du bucket", "placeholder": "mon-projet-uploads"},
        {"key": "dashboard_url", "label": "URL dashboard", "placeholder": "https://dash.cloudflare.com/..."},
    ],
    "domain": [
        {"key": "domain", "label": "Domaine", "placeholder": "example.com", "required": True},
        {"key": "registrar", "label": "Registrar", "placeholder": "cloudflare, namecheap, ovh"},
        {"key": "dashboard_url", "label": "URL dashboard", "placeholder": "https://dash.cloudflare.com/..."},
    ],
    "server": [
        {"key": "ip", "label": "Adresse IP", "placeholder": "12.34.56.78"},
        {"key": "provider", "label": "Provider", "placeholder": "ovh, digitalocean, hetzner"},
        {"key": "ssh_user", "label": "Utilisateur SSH", "placeholder": "root"},
        {"key": "dashboard_url", "label": "URL dashboard", "placeholder": "https://cloud.digitalocean.com/..."},
    ],
    "backup": [
        {"key": "destination", "label": "Destination", "placeholder": "r2, s3, local"},
        {"key": "bucket", "label": "Bucket", "placeholder": "mon-projet-backups"},
        {"key": "frequency", "label": "Frequence", "placeholder": "daily, weekly, manual"},
    ],
}

SENSITIVE_KEYS = {"connection_string", "deploy_hook", "access_key_id", "secret_access_key", "account_id"}


def _mask_config(cfg):
    """Return config with sensitive values masked for API response."""
    masked = {}
    for k, v in cfg.items():
        if k in SENSITIVE_KEYS and v and v != "****":
            masked[k] = "****" + str(v)[-4:] if len(str(v)) > 8 else "****"
        else:
            masked[k] = v
    return masked


def _parse_cfg(comp):
    """Extract config dict from a component row."""
    if not comp:
        return {}
    cfg = comp.get("config", {})
    if isinstance(cfg, str):
        try: return json.loads(cfg)
        except: return {}
    return cfg


@cloud_bp.route("/api/projects/<int:pid>/connections")
def api_get_connections(pid):
    """Get all connections for a project — verified real status + action hints."""
    project = db.get_project(pid)
    if not project:
        return jsonify({"connections": []})

    comps = {c["component"]: c for c in db.get_project_components(pid)}
    connections = []
    ppath = project.get("path", "")

    # Load wizard data for planned (not yet connected) services
    specs = db.get_project_specs(pid)
    wd = specs.get("wizard_data", {}) if specs else {}
    if isinstance(wd, str):
        try: wd = json.loads(wd)
        except: wd = {}

    r2_account = db.get_setting("r2_account_id", "")

    # ──── CODE SOURCE + GITHUB (unified) ────
    import subprocess as _sp
    import re as _re

    git_remote = project.get("git_remote", "")
    git_cfg = _parse_cfg(comps.get("git"))
    has_git_dir = ppath and os.path.isdir(os.path.join(ppath, ".git"))
    dir_has_files = False
    if ppath and os.path.isdir(ppath):
        contents = [f for f in os.listdir(ppath) if f not in (".devpilot", "CLAUDE.md", ".claude", ".git")]
        dir_has_files = len(contents) > 0

    # Check real git state
    real_remote = ""
    branch = ""
    if has_git_dir:
        try:
            real_remote = _sp.run(["git", "-C", ppath, "remote", "get-url", "origin"],
                                  capture_output=True, text=True, timeout=3).stdout.strip()
            branch = _sp.run(["git", "-C", ppath, "branch", "--show-current"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            pass

    # Clean URL (strip token)
    clean_remote = _re.sub(r'https://[^@]+@', 'https://', git_remote or real_remote)
    github_url = clean_remote if clean_remote.startswith("http") else (f"https://github.com/{clean_remote}" if clean_remote else "")

    # Determine state and actions
    if real_remote and has_git_dir:
        # STATE: Fully connected — git + remote
        last_push = db.get_last_sync(pid, "github", "push")
        connections.append({
            "type": "github", "label": "GitHub", "status": "connected", "url": github_url,
            "details": {"branche": branch, "dernier_push": last_push.get("started_at", "") if last_push else "",
                        "dossier": ppath},
            "action": None,
        })

    elif has_git_dir and not real_remote:
        # STATE: Git init but no remote — needs to link to GitHub
        connections.append({
            "type": "github", "label": "Code source (git local)", "status": "a_configurer",
            "url": "", "details": {"branche": branch, "dossier": ppath},
            "actions": [
                {"label": "Lier a un repo existant", "action_type": "link_repo"},
                {"label": "Creer un nouveau repo", "url": "https://github.com/new", "action_type": "create_repo"},
            ],
        })

    elif not has_git_dir and dir_has_files:
        # STATE: Files exist but no git — needs init + push OR link to existing
        connections.append({
            "type": "github", "label": "Code source (pas de git)", "status": "non_connecte",
            "url": "", "details": {"dossier": ppath, "fichiers": "oui"},
            "actions": [
                {"label": "Creer un repo et pusher", "url": "https://github.com/new", "action_type": "create_and_push"},
                {"label": "Lier a un repo existant", "action_type": "link_repo"},
            ],
        })

    elif not has_git_dir and not dir_has_files:
        # STATE: Empty folder — can clone or create new
        connections.append({
            "type": "github", "label": "Code source (vide)", "status": "non_connecte",
            "url": "", "details": {"dossier": ppath},
            "actions": [
                {"label": "Cloner un repo existant", "action_type": "clone_repo"},
                {"label": "Creer un nouveau repo", "url": "https://github.com/new", "action_type": "create_repo"},
            ],
        })

    # ──── HOSTING / DEPLOIEMENT ────
    deploy_cfg = _parse_cfg(comps.get("deploy"))
    deploy_url = deploy_cfg.get("site_url", "") or deploy_cfg.get("url", "")
    platform = deploy_cfg.get("platform", "") or wd.get("frontend_hosting", "")
    account_name = deploy_cfg.get("account_name", "")
    account_id = deploy_cfg.get("account_id", "")
    project_url = deploy_cfg.get("project_url", "")
    host_urls = {"vercel": "https://vercel.com/dashboard", "netlify": "https://app.netlify.com/",
                 "railway": "https://railway.app/dashboard", "render": "https://dashboard.render.com/",
                 "fly": "https://fly.io/dashboard", "cloudflare": "https://dash.cloudflare.com/"}
    token_urls = {"vercel": "https://vercel.com/account/tokens", "netlify": "https://app.netlify.com/user/applications#personal-access-tokens",
                  "railway": "https://railway.app/account/tokens", "render": "https://dashboard.render.com/settings#api-keys",
                  "fly": "https://fly.io/user/personal_access_tokens", "cloudflare": "https://dash.cloudflare.com/profile/api-tokens"}

    # Check if account has a valid token
    account_has_token = False
    account_email = ""
    if account_id:
        accts = _get_hosting_accounts()
        acct = next((a for a in accts if a["id"] == account_id), None)
        if acct:
            account_has_token = bool(acct.get("token"))
            account_email = acct.get("email", "")

    token_url = token_urls.get(platform, "")

    if deploy_url and account_id:
        # Fully connected: account + site URL + token
        connections.append({
            "type": "deploy",
            "label": f"{platform.title()} — {account_name}" if account_name else f"Deploiement ({platform})",
            "status": "connected", "url": deploy_url,
            "details": {"compte": account_name, "email": account_email, "plateforme": platform,
                        "site": deploy_url, "dashboard": project_url,
                        "token": "actif" if account_has_token else "manquant"},
            "actions": [
                {"label": "Regenerer le token", "url": token_url, "action_type": "regen_token"},
            ] if token_url else [],
        })
    elif account_id:
        # Account linked but no site URL yet
        connections.append({
            "type": "deploy",
            "label": f"{platform.title()} — {account_name}" if account_name else f"Deploiement ({platform})",
            "status": "a_configurer", "url": project_url or host_urls.get(platform, ""),
            "details": {"compte": account_name, "email": account_email, "plateforme": platform,
                        "token": "actif" if account_has_token else "manquant"},
            "actions": [
                {"label": "Configurer le token", "url": token_url, "action_type": "regen_token"} if not account_has_token else
                {"label": f"Ouvrir {platform.title()}", "url": host_urls.get(platform, ""), "action_type": "open_hosting"},
                {"label": "Entrer l'URL du site", "action_type": "enter_site_url"},
            ],
        })
    elif platform:
        # Platform chosen but no account linked
        connections.append({
            "type": "deploy",
            "label": f"Deploiement ({platform})",
            "status": "non_connecte", "url": "",
            "details": {"plateforme": platform},
            "actions": [
                {"label": "Choisir un compte", "action_type": "pick_hosting_account"},
            ],
        })

    # ──── DOMAINE ────
    domain = deploy_cfg.get("domain", "") or wd.get("domain_name", "")
    dns = deploy_cfg.get("dns", "") or wd.get("dns_provider", "")
    ssl = deploy_cfg.get("ssl", "") or wd.get("ssl", "")
    dns_urls = {"cloudflare": "https://dash.cloudflare.com/", "namecheap": "https://www.namecheap.com/",
                "ovh": "https://www.ovh.com/manager/"}

    if domain:
        connections.append({
            "type": "domain", "label": domain, "status": "connected",
            "url": f"https://{domain}",
            "details": {"DNS": dns, "SSL": ssl}, "action": None,
        })

    # ──── BASE DE DONNEES ────
    db_cfg = _parse_cfg(comps.get("database"))
    db_type = db_cfg.get("type", "") or wd.get("db_type", "")
    db_host = db_cfg.get("host", "")
    db_urls = {"supabase": "https://supabase.com/dashboard", "neon": "https://console.neon.tech/",
               "planetscale": "https://app.planetscale.com/"}

    if db_host:
        connections.append({
            "type": "database", "label": DB_LABELS.get(db_type, db_type.title() if db_type else "Database"),
            "status": "connected", "url": db_cfg.get("url", db_urls.get(db_type, "")),
            "details": {"type": db_type, "host": db_host, "port": db_cfg.get("port", ""), "nom": db_cfg.get("name", "")},
            "action": None,
        })
    elif db_type:
        connections.append({
            "type": "database", "label": DB_LABELS.get(db_type, db_type.title()),
            "status": "a_configurer", "url": "",
            "details": {"type": db_type},
            "action": {"label": f"Creer {db_type}", "url": db_urls.get(db_type, ""), "action_type": "create_db"},
        })

    # ──── STOCKAGE R2 ────
    storage_cfg = _parse_cfg(comps.get("storage"))
    bucket = storage_cfg.get("bucket", "") or db.get_setting("r2_default_bucket", "")

    if r2_account and bucket:
        r2_stats = db.get_r2_storage_stats(pid)
        cf_url = f"https://dash.cloudflare.com/{r2_account}/r2/default/buckets/{bucket}"
        connections.append({
            "type": "storage", "label": f"R2 — {bucket}", "status": "connected", "url": cf_url,
            "details": {
                "bucket": bucket,
                "objets": r2_stats.get("count", 0) if isinstance(r2_stats, dict) else 0,
                "taille": r2_stats.get("total_size", 0) if isinstance(r2_stats, dict) else 0,
            }, "action": None,
        })
    elif comps.get("storage") or wd.get("file_upload") == "yes":
        connections.append({
            "type": "storage", "label": "Stockage cloud", "status": "non_connecte", "url": "",
            "details": {},
            "action": {"label": "Configurer R2", "url": "https://dash.cloudflare.com/", "action_type": "setup_r2"},
        })

    # ──── BACKUP ────
    backup_cfg = _parse_cfg(comps.get("backup"))
    backup_bucket = backup_cfg.get("bucket", "") or wd.get("backup_bucket", "")
    backup_freq = backup_cfg.get("frequency", "") or wd.get("backup_freq", "")
    last_backup = db.get_last_sync(pid, "r2", "push")

    if comps.get("backup") or wd.get("needs_backup") == "yes":
        if last_backup:
            connections.append({
                "type": "backup", "label": "Backup", "status": "connected",
                "url": f"https://dash.cloudflare.com/{r2_account}/r2/default/buckets/{backup_bucket}" if (r2_account and backup_bucket) else "",
                "details": {"frequence": backup_freq, "bucket": backup_bucket,
                            "dernier": last_backup.get("started_at", "")},
                "action": None,
            })
        else:
            connections.append({
                "type": "backup", "label": "Backup", "status": "a_configurer", "url": "",
                "details": {"frequence": backup_freq or "non configure"},
                "action": {"label": "Configurer backup", "url": "", "action_type": "setup_backup"},
            })

    # ──── SERVEUR ────
    backend_host = deploy_cfg.get("backend_hosting", "") or wd.get("backend_hosting", "")
    server_host = deploy_cfg.get("server_host", "")
    if backend_host == "vps" or server_host:
        connections.append({
            "type": "server", "label": "Serveur VPS",
            "status": "connected" if server_host else "a_configurer",
            "url": "", "details": {"adresse": server_host or "non configure", "provider": deploy_cfg.get("provider", "")},
            "action": None if server_host else {"label": "Ajouter le serveur", "url": "", "action_type": "add_server"},
        })

    # ──── DOCKER ────
    docker_cfg = _parse_cfg(comps.get("docker"))
    if comps.get("docker"):
        compose = docker_cfg.get("compose_file", "")
        services = docker_cfg.get("services", [])
        connections.append({
            "type": "docker", "label": "Docker",
            "status": "connected" if compose else "detecte",
            "url": "", "details": {"fichier": compose, "services": ", ".join(services) if services else ""},
            "action": None,
        })

    # ──── CI/CD ────
    ci_cfg = _parse_cfg(comps.get("ci_cd"))
    if comps.get("ci_cd"):
        provider = ci_cfg.get("provider", "")
        ci_url = ""
        if provider == "github" and real_remote:
            ci_url = (real_remote if real_remote.startswith("http") else f"https://github.com/{real_remote}") + "/actions"
        connections.append({
            "type": "ci_cd", "label": f"CI/CD ({provider})" if provider else "CI/CD",
            "status": "connected" if ci_url else "detecte",
            "url": ci_url,
            "details": {"workflows": ", ".join(ci_cfg.get("workflows", []))},
            "action": None if ci_url else {"label": "Configurer CI/CD", "url": "https://github.com/new", "action_type": "setup_ci"},
        })

    return jsonify({"connections": connections})


@cloud_bp.route("/api/projects/<int:pid>/connections", methods=["POST"])
def api_update_connection(pid):
    """Add or update a connection for a project."""
    data = request.json or {}
    conn_type = data.get("type", "")
    config = data.get("config", {})

    if not conn_type:
        return jsonify({"success": False, "message": "Type requis"}), 400

    project = db.get_project(pid)
    if not project:
        return jsonify({"success": False, "message": "Projet introuvable"}), 404

    # Strip masked values — don't overwrite real secrets with "****"
    config = {k: v for k, v in config.items() if v and not str(v).startswith("****")}

    # Map connection type to component key
    comp_key = conn_type
    git_result = None
    if conn_type == "github":
        comp_key = "git"
        repo_url = config.get("url", "")
        if repo_url:
            db.update_project(pid, git_remote=repo_url)

            # Auto git setup in project directory
            ppath = project.get("path", "")
            if ppath and os.path.isdir(ppath):
                import subprocess
                git_dir = os.path.join(ppath, ".git")
                # Check if folder is empty (only .devpilot and CLAUDE.md)
                contents = [f for f in os.listdir(ppath) if f not in (".devpilot", "CLAUDE.md", ".claude")]
                is_empty = len(contents) == 0

                if is_empty and not os.path.isdir(git_dir):
                    # Empty folder → clone into it
                    # Clone to temp, then move contents
                    import tempfile, shutil
                    tmp = tempfile.mkdtemp()
                    try:
                        # Use token if available for private repos
                        clone_url = repo_url
                        token = db.get_setting("github_token", "")
                        if token and "github.com" in repo_url and repo_url.startswith("https://"):
                            clone_url = repo_url.replace("https://", f"https://{token}@")
                        r = subprocess.run(["git", "clone", clone_url, tmp + "/repo"],
                                           capture_output=True, text=True, timeout=120)
                        if r.returncode == 0 and os.path.isdir(tmp + "/repo"):
                            # Reset remote to clean URL (without token)
                            subprocess.run(["git", "-C", tmp + "/repo", "remote", "set-url", "origin", repo_url],
                                           capture_output=True, timeout=5)
                            # Move all cloned files into project path
                            for item in os.listdir(tmp + "/repo"):
                                src = os.path.join(tmp + "/repo", item)
                                dst = os.path.join(ppath, item)
                                if os.path.exists(dst):
                                    if os.path.isdir(dst):
                                        shutil.rmtree(dst)
                                    else:
                                        os.remove(dst)
                                shutil.move(src, dst)
                            git_result = {"action": "cloned", "message": f"Repo clone dans {ppath}"}
                        else:
                            git_result = {"action": "clone_failed", "message": r.stderr.strip()[:200]}
                    except Exception as e:
                        git_result = {"action": "clone_failed", "message": str(e)[:200]}
                    finally:
                        shutil.rmtree(tmp, ignore_errors=True)

                elif not os.path.isdir(git_dir):
                    # Folder has files but no git → init + remote + pull
                    subprocess.run(["git", "-C", ppath, "init"], capture_output=True, timeout=10)
                    subprocess.run(["git", "-C", ppath, "remote", "add", "origin", repo_url],
                                   capture_output=True, timeout=10)
                    r = subprocess.run(["git", "-C", ppath, "pull", "origin", "main", "--allow-unrelated-histories"],
                                       capture_output=True, text=True, timeout=60)
                    if r.returncode != 0:
                        # Try master branch
                        subprocess.run(["git", "-C", ppath, "pull", "origin", "master", "--allow-unrelated-histories"],
                                       capture_output=True, timeout=60)
                    git_result = {"action": "linked", "message": f"Git init + remote add + pull dans {ppath}"}

                else:
                    # Already a git repo → just update remote
                    existing_remote = subprocess.run(["git", "-C", ppath, "remote", "get-url", "origin"],
                                                     capture_output=True, text=True, timeout=5).stdout.strip()
                    if existing_remote:
                        subprocess.run(["git", "-C", ppath, "remote", "set-url", "origin", repo_url],
                                       capture_output=True, timeout=5)
                    else:
                        subprocess.run(["git", "-C", ppath, "remote", "add", "origin", repo_url],
                                       capture_output=True, timeout=5)
                    git_result = {"action": "updated", "message": "Remote origin mis a jour"}

    # Merge into existing component config
    existing = db.get_project_component(pid, comp_key)
    if existing:
        merged = existing.get("config", {})
        if isinstance(merged, str):
            try: merged = json.loads(merged)
            except: merged = {}
        merged.update(config)
        db.update_project_component(pid, comp_key, config=merged, enabled=True)
    else:
        db.add_project_component(pid, comp_key, enabled=True, config=config)

    # Regenerate CLAUDE.md so Claude sees the new connection
    ppath = project.get("path", "")
    if ppath and os.path.isdir(ppath):
        from dashboard import _write_claude_md
        project = db.get_project(pid)  # re-read updated data
        specs = db.get_project_specs(pid)
        _write_claude_md(ppath, project, specs)

    result = {"success": True}
    if git_result:
        result["git"] = git_result
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════
# HOSTING ACCOUNTS — manage multiple Vercel/Netlify/etc. accounts
# ═══════════════════════════════════════════════════════════════════════════

def _get_hosting_accounts():
    raw = db.get_setting("hosting_accounts", "[]")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _save_hosting_accounts(accounts):
    db.set_setting("hosting_accounts", json.dumps(accounts))


@cloud_bp.route("/api/hosting/accounts")
def api_hosting_accounts():
    """List all hosting accounts (tokens masked)."""
    accounts = _get_hosting_accounts()
    # Mask tokens for API response
    result = []
    for a in accounts:
        masked = {k: v for k, v in a.items() if k != "token"}
        masked["token_set"] = bool(a.get("token"))
        masked["token_preview"] = "****" + a["token"][-4:] if a.get("token") else ""
        result.append(masked)
    return jsonify(result)


def _verify_hosting_token(platform, token):
    """Verify a hosting token and return real account info from the platform API."""
    import requests
    try:
        if platform == "vercel":
            r = requests.get("https://api.vercel.com/v2/user", headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if r.status_code == 200:
                u = r.json().get("user", {})
                return {"valid": True, "email": u.get("email", ""), "username": u.get("username", ""), "name": u.get("name", "")}
            return {"valid": False, "error": f"Token invalide ({r.status_code})"}

        elif platform == "netlify":
            r = requests.get("https://api.netlify.com/api/v1/user", headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if r.status_code == 200:
                u = r.json()
                return {"valid": True, "email": u.get("email", ""), "username": u.get("slug", ""), "name": u.get("full_name", "")}
            return {"valid": False, "error": f"Token invalide ({r.status_code})"}

        elif platform == "railway":
            r = requests.post("https://backboard.railway.app/graphql/v2",
                              json={"query": "query { me { email name } }"},
                              headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if r.status_code == 200:
                me = r.json().get("data", {}).get("me", {})
                return {"valid": True, "email": me.get("email", ""), "username": "", "name": me.get("name", "")}
            return {"valid": False, "error": f"Token invalide ({r.status_code})"}

        elif platform == "render":
            r = requests.get("https://api.render.com/v1/owners", headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if r.status_code == 200:
                owners = r.json()
                if owners:
                    o = owners[0].get("owner", {})
                    return {"valid": True, "email": o.get("email", ""), "username": o.get("id", ""), "name": o.get("name", "")}
            return {"valid": False, "error": f"Token invalide ({r.status_code})"}

        # Default: accept without verification
        return {"valid": True, "email": "", "username": "", "name": ""}
    except Exception as e:
        return {"valid": False, "error": str(e)[:100]}


@cloud_bp.route("/api/hosting/accounts", methods=["POST"])
def api_add_hosting_account():
    """Add a new hosting account — verifies token with the platform API."""
    data = request.json or {}
    platform = data.get("platform", "vercel").strip().lower()
    token = data.get("token", "").strip()

    if not token:
        return jsonify({"success": False, "message": "Token requis — genere-le sur la plateforme"}), 400

    # Verify token and get real account info
    info = _verify_hosting_token(platform, token)
    if not info.get("valid"):
        return jsonify({"success": False, "message": f"Token invalide: {info.get('error', 'erreur inconnue')}"}), 400

    # Use the real info from the platform
    name = data.get("name", "").strip() or info.get("name") or info.get("username") or info.get("email", "").split("@")[0]
    email = info.get("email", "")
    username = info.get("username", "")

    accounts = _get_hosting_accounts()

    # Check if account with same email already exists
    existing = next((a for a in accounts if a.get("email") == email and a.get("platform") == platform and email), None)
    if existing:
        # Update token on existing account
        existing["token"] = token
        existing["username"] = username
        existing["name"] = name or existing["name"]
        _save_hosting_accounts(accounts)
        return jsonify({"success": True, "id": existing["id"], "name": existing["name"], "email": email,
                        "message": f"Token mis a jour pour {email}"})

    # Generate unique ID
    import hashlib, time
    aid = hashlib.md5(f"{email}{time.time()}".encode()).hexdigest()[:8]

    accounts.append({
        "id": aid,
        "name": name,
        "platform": platform,
        "email": email,
        "username": username,
        "token": token,
        "projects": [],
    })
    _save_hosting_accounts(accounts)
    return jsonify({"success": True, "id": aid, "name": name, "email": email, "username": username})


@cloud_bp.route("/api/hosting/accounts/<aid>", methods=["PUT"])
def api_update_hosting_account(aid):
    """Update a hosting account — re-verifies token if changed."""
    data = request.json or {}
    accounts = _get_hosting_accounts()
    for a in accounts:
        if a["id"] == aid:
            new_token = data.get("token", "")
            if new_token and not new_token.startswith("****"):
                # Verify the new token
                info = _verify_hosting_token(a["platform"], new_token)
                if not info.get("valid"):
                    return jsonify({"success": False, "message": f"Token invalide: {info.get('error', '')}"}), 400
                a["token"] = new_token
                if info.get("email"): a["email"] = info["email"]
                if info.get("username"): a["username"] = info["username"]
                if info.get("name"): a["name"] = info["name"]
            if "name" in data and data["name"]: a["name"] = data["name"]
            _save_hosting_accounts(accounts)
            return jsonify({"success": True, "email": a.get("email", ""), "name": a.get("name", "")})
    return jsonify({"success": False, "message": "Compte introuvable"}), 404


@cloud_bp.route("/api/hosting/accounts/<aid>", methods=["DELETE"])
def api_delete_hosting_account(aid):
    """Delete a hosting account."""
    accounts = _get_hosting_accounts()
    accounts = [a for a in accounts if a["id"] != aid]
    _save_hosting_accounts(accounts)
    return jsonify({"success": True})


@cloud_bp.route("/api/hosting/accounts/<aid>/token")
def api_get_hosting_token(aid):
    """Get the actual token for a hosting account (for Claude to use)."""
    accounts = _get_hosting_accounts()
    for a in accounts:
        if a["id"] == aid:
            return jsonify({"token": a.get("token", ""), "platform": a["platform"], "name": a["name"]})
    return jsonify({"error": "Compte introuvable"}), 404


@cloud_bp.route("/api/projects/<int:pid>/hosting", methods=["POST"])
def api_link_hosting(pid):
    """Link a project to a hosting account."""
    data = request.json or {}
    account_id = data.get("account_id", "")
    site_url = data.get("site_url", "")
    project_url = data.get("project_url", "")

    project = db.get_project(pid)
    if not project:
        return jsonify({"success": False, "message": "Projet introuvable"}), 404

    # Find the account
    accounts = _get_hosting_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        return jsonify({"success": False, "message": "Compte introuvable"}), 404

    # Track project in account
    if pid not in account.get("projects", []):
        account.setdefault("projects", []).append(pid)
        _save_hosting_accounts(accounts)

    # Update deploy component config
    deploy_config = {
        "platform": account["platform"],
        "account_id": account_id,
        "account_name": account["name"],
    }
    if site_url:
        deploy_config["site_url"] = site_url
    if project_url:
        deploy_config["project_url"] = project_url

    existing = db.get_project_component(pid, "deploy")
    if existing:
        merged = existing.get("config", {})
        if isinstance(merged, str):
            try: merged = json.loads(merged)
            except: merged = {}
        merged.update(deploy_config)
        db.update_project_component(pid, "deploy", config=merged, enabled=True)
    else:
        db.add_project_component(pid, "deploy", enabled=True, config=deploy_config)

    # Regenerate CLAUDE.md
    ppath = project.get("path", "")
    if ppath and os.path.isdir(ppath):
        from dashboard import _write_claude_md
        project = db.get_project(pid)
        specs = db.get_project_specs(pid)
        _write_claude_md(ppath, project, specs)

    return jsonify({"success": True, "account": account["name"], "platform": account["platform"]})
