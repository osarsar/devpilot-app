"""DevPilot — Cloud sync orchestration + asset classification engine."""

import os
import json
from pathlib import Path
from fnmatch import fnmatch
from datetime import datetime

import db
import github_ops
import cloudflare_r2


# ═══════════════════════════════════════════════════════════════════════════
# DEFAULT ASSET RULES TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_ASSET_RULES = [
    # Source code -> GitHub
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.py",     "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.js",     "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.ts",     "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.tsx",    "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.jsx",    "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.dart",   "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.html",   "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.css",    "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.json",   "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.yaml",   "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.yml",    "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.md",     "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.sh",     "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.go",     "is_public": 0},
    {"asset_type": "source",   "storage_backend": "github",    "glob_pattern": "*.rs",     "is_public": 0},

    # Database -> R2 (private)
    {"asset_type": "database", "storage_backend": "r2",        "glob_pattern": "*.sql",     "is_public": 0},
    {"asset_type": "database", "storage_backend": "r2",        "glob_pattern": "*.db",      "is_public": 0},
    {"asset_type": "database", "storage_backend": "r2",        "glob_pattern": "*.sqlite",  "is_public": 0},
    {"asset_type": "database", "storage_backend": "r2",        "glob_pattern": "*.sqlite3", "is_public": 0},
    {"asset_type": "database", "storage_backend": "r2",        "glob_pattern": "*.dump",    "is_public": 0},
    {"asset_type": "database", "storage_backend": "r2",        "glob_pattern": "backups/*",  "is_public": 0},

    # Media -> R2
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.png",     "is_public": 1},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.jpg",     "is_public": 1},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.jpeg",    "is_public": 1},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.gif",     "is_public": 1},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.svg",     "is_public": 1},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.webp",    "is_public": 1},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.mp4",     "is_public": 0},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.pdf",     "is_public": 0},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.zip",     "is_public": 0},
    {"asset_type": "media",    "storage_backend": "r2",        "glob_pattern": "*.tar.gz",  "is_public": 0},

    # Config/Secrets -> Local ONLY
    {"asset_type": "config",   "storage_backend": "local",     "glob_pattern": ".env",         "is_public": 0},
    {"asset_type": "config",   "storage_backend": "local",     "glob_pattern": ".env.*",       "is_public": 0},
    {"asset_type": "config",   "storage_backend": "local",     "glob_pattern": "*.pem",        "is_public": 0},
    {"asset_type": "config",   "storage_backend": "local",     "glob_pattern": "*.key",        "is_public": 0},
    {"asset_type": "config",   "storage_backend": "local",     "glob_pattern": "*.cert",       "is_public": 0},
    {"asset_type": "config",   "storage_backend": "local",     "glob_pattern": "config.json",  "is_public": 0},
    {"asset_type": "config",   "storage_backend": "local",     "glob_pattern": "credentials*", "is_public": 0},
    {"asset_type": "config",   "storage_backend": "local",     "glob_pattern": "storage_state*", "is_public": 0},

    # Build artifacts -> Ephemeral
    {"asset_type": "build",    "storage_backend": "ephemeral", "glob_pattern": "node_modules/*", "is_public": 0},
    {"asset_type": "build",    "storage_backend": "ephemeral", "glob_pattern": ".next/*",       "is_public": 0},
    {"asset_type": "build",    "storage_backend": "ephemeral", "glob_pattern": "dist/*",        "is_public": 0},
    {"asset_type": "build",    "storage_backend": "ephemeral", "glob_pattern": "__pycache__/*", "is_public": 0},
    {"asset_type": "build",    "storage_backend": "ephemeral", "glob_pattern": ".dart_tool/*",  "is_public": 0},
    {"asset_type": "build",    "storage_backend": "ephemeral", "glob_pattern": "build/*",       "is_public": 0},
    {"asset_type": "build",    "storage_backend": "ephemeral", "glob_pattern": "target/*",      "is_public": 0},
    {"asset_type": "build",    "storage_backend": "ephemeral", "glob_pattern": ".gradle/*",     "is_public": 0},

    # Sessions -> Local only
    {"asset_type": "session",  "storage_backend": "local",     "glob_pattern": ".devpilot/*",     "is_public": 0},
]

# Directories to skip when scanning
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".next", ".nuxt", "dist", "build",
    "target", ".gradle", ".dart_tool", "venv", ".venv", "env", ".tox",
    ".eggs", "vendor", "site-packages", ".expo", ".parcel-cache",
}


# ═══════════════════════════════════════════════════════════════════════════
# ASSET CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

def apply_default_rules(project_id, r2_bucket="", components=None):
    """Apply default asset classification rules to a project.

    If components is provided (list of active component keys), only apply
    rules relevant to those components. None = apply all (legacy behavior).
    """
    # Map asset types to required components
    ASSET_COMPONENT_MAP = {
        "source":   "git",
        "database": "database",
        "media":    "storage",
        "config":   "secrets",
        "build":    None,       # always apply
        "session":  None,       # always apply
    }

    applied = 0
    for rule in DEFAULT_ASSET_RULES:
        # Filter by active components if specified
        if components is not None:
            required = ASSET_COMPONENT_MAP.get(rule["asset_type"])
            if required and required not in components:
                continue

        try:
            db.add_asset_rule(
                project_id=project_id,
                asset_type=rule["asset_type"],
                storage_backend=rule["storage_backend"],
                glob_pattern=rule["glob_pattern"],
                r2_bucket=r2_bucket,
                is_public=rule.get("is_public", False),
            )
            applied += 1
        except Exception:
            pass
    return applied


def classify_file(file_path, project_id):
    """Classify a file based on the project's asset rules. Returns asset_type string."""
    rules = db.get_asset_rules(project_id)
    if not rules:
        return "unclassified"

    fname = os.path.basename(file_path)

    # Get project path for relative matching
    project = db.get_project(project_id)
    rel_path = file_path
    if project and project.get("path"):
        try:
            rel_path = os.path.relpath(file_path, project["path"])
        except ValueError:
            pass

    # Check config/secrets first (highest priority — never sync these)
    for rule in rules:
        if rule["storage_backend"] == "local" and rule["asset_type"] == "config":
            if fnmatch(fname, rule["glob_pattern"]) or fnmatch(rel_path, rule["glob_pattern"]):
                return "config"

    # Then check all other rules
    for rule in rules:
        if rule["asset_type"] == "config":
            continue
        if fnmatch(fname, rule["glob_pattern"]) or fnmatch(rel_path, rule["glob_pattern"]):
            return rule["asset_type"]

    return "unclassified"


def scan_project_assets(project_id):
    """Scan a project directory and classify all files. Returns breakdown by type."""
    project = db.get_project(project_id)
    if not project or not project.get("path"):
        return {"error": "Project has no path"}

    ppath = project["path"]
    if not os.path.isdir(ppath):
        return {"error": f"Directory not found: {ppath}"}

    breakdown = {}
    total_files = 0

    for root, dirs, files in os.walk(ppath):
        # Skip build/cache dirs
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in files:
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, ppath)

            try:
                size = os.path.getsize(fpath)
            except (OSError, PermissionError):
                continue

            asset_type = classify_file(fpath, project_id)

            if asset_type not in breakdown:
                breakdown[asset_type] = {"count": 0, "size": 0, "files": []}

            breakdown[asset_type]["count"] += 1
            breakdown[asset_type]["size"] += size
            # Keep first 10 files per type for preview
            if len(breakdown[asset_type]["files"]) < 10:
                breakdown[asset_type]["files"].append(rel_path)
            total_files += 1

    # Add backend info from rules
    rules = db.get_asset_rules(project_id)
    type_to_backend = {}
    for rule in rules:
        if rule["asset_type"] not in type_to_backend:
            type_to_backend[rule["asset_type"]] = rule["storage_backend"]

    for atype in breakdown:
        breakdown[atype]["backend"] = type_to_backend.get(atype, "unclassified")

    return {"breakdown": breakdown, "total_files": total_files, "project_path": ppath}


# ═══════════════════════════════════════════════════════════════════════════
# SYNC OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

def _get_r2_client():
    """Get R2 client from stored settings."""
    account_id = db.get_setting("r2_account_id", "")
    access_key = db.get_setting("r2_access_key_id", "")
    secret_key = db.get_setting("r2_secret_access_key", "")
    if not account_id or not access_key or not secret_key:
        return None
    return cloudflare_r2.r2_get_client(account_id, access_key, secret_key)


def sync_project_to_github(project_id, message="", add_all=True):
    """Push a project's code to GitHub."""
    project = db.get_project(project_id)
    if not project or not project.get("path"):
        return {"success": False, "message": "Projet sans dossier"}

    ppath = project["path"]
    if not (Path(ppath) / ".git").exists():
        return {"success": False, "message": "Pas un depot Git"}

    if not message:
        message = f"DevPilot sync {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    # Get status before
    status = github_ops.git_status_detailed(ppath)
    if not status:
        return {"success": False, "message": "Impossible de lire le status Git"}

    if status["is_clean"]:
        # Nothing to commit, just try to push
        ok, output = github_ops.git_push(ppath)
        if ok:
            db.log_sync(project_id, "push", "github", "source", status="success", details=output)
            db.log_event("synced", "github", project["name"], ppath, 0, project_id, details="Push (already clean)")
            return {"success": True, "message": "Deja a jour, push OK", "output": output}
        return {"success": False, "message": f"Push echoue: {output}"}

    # Stage and commit
    if add_all:
        ok, output = github_ops.git_commit(ppath, message, add_all=True)
    else:
        ok, output = github_ops.git_commit(ppath, message)

    if not ok:
        return {"success": False, "message": f"Commit echoue: {output}"}

    # Push
    ok_push, push_output = github_ops.git_push(ppath)

    files_count = status["modified"] + status["untracked"] + status["staged"]
    if ok_push:
        db.log_sync(project_id, "push", "github", "source", files_count=files_count, status="success",
                     details=json.dumps({"commit": output, "push": push_output}))
        db.log_event("synced", "github", project["name"], ppath, 0, project_id,
                     details=f"Commit + Push: {files_count} fichiers")
        db.update_project_activity(project_id)
        return {
            "success": True,
            "message": f"{files_count} fichiers commites et pushes",
            "commit_output": output,
            "push_output": push_output,
        }
    else:
        db.log_sync(project_id, "push", "github", "source", files_count=files_count, status="partial",
                     error_message=push_output,
                     details=json.dumps({"commit": output, "push_error": push_output}))
        return {
            "success": False,
            "message": f"Commit OK mais push echoue: {push_output}",
            "commit_output": output,
        }


def project_bucket(project_id):
    """The bucket chosen for THIS project (backup, else storage component), so backup and restore agree."""
    import json as _json
    for key in ("backup", "storage"):
        c = db.get_project_component(project_id, key)
        if not c:
            continue
        cfg = c.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = _json.loads(cfg)
            except ValueError:
                cfg = {}
        if cfg.get("bucket"):
            return cfg["bucket"]
    return ""


def sync_project_to_r2(project_id, asset_type=None):
    """Upload project files to R2 based on asset rules."""
    project = db.get_project(project_id)
    if not project or not project.get("path"):
        return {"success": False, "message": "Projet sans dossier"}

    client = _get_r2_client()
    if not client:
        return {"success": False, "message": "R2 non configure"}

    ppath = project["path"]
    rules = db.get_asset_rules(project_id)
    r2_rules = [r for r in rules if r["storage_backend"] == "r2"]
    if asset_type:
        r2_rules = [r for r in r2_rules if r["asset_type"] == asset_type]

    if not r2_rules:
        return {"success": False, "message": "Pas de regles R2 pour ce projet"}

    bucket = project_bucket(project_id) or r2_rules[0].get("r2_bucket") or db.get_setting("r2_default_bucket", "")
    if not bucket:
        return {"success": False, "message": "Pas de bucket R2 configure"}

    uploaded = 0
    failed = 0
    total_bytes = 0
    errors = []

    for root, dirs, files in os.walk(ppath):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in files:
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, ppath)

            # Check if file matches any R2 rule
            matched_rule = None
            for rule in r2_rules:
                if fnmatch(fname, rule["glob_pattern"]) or fnmatch(rel_path, rule["glob_pattern"]):
                    matched_rule = rule
                    break

            if not matched_rule:
                continue

            # Build R2 key
            prefix = matched_rule.get("r2_prefix") or project["name"]
            remote_key = f"{prefix}/{matched_rule['asset_type']}/{rel_path}"

            result = cloudflare_r2.r2_upload_file(client, bucket, fpath, remote_key)
            if result.get("success"):
                uploaded += 1
                total_bytes += result.get("size", 0)
                db.upsert_r2_object(
                    project_id, bucket, remote_key,
                    size=result.get("size", 0),
                    content_type=result.get("content_type", ""),
                    asset_type=matched_rule["asset_type"],
                )
            else:
                failed += 1
                errors.append(f"{rel_path}: {result.get('message', '')}")

    status = "success" if failed == 0 else ("partial" if uploaded > 0 else "failed")
    db.log_sync(project_id, "backup", "r2", asset_type or "all",
                files_count=uploaded, total_bytes=total_bytes, status=status,
                error_message="; ".join(errors[:5]) if errors else "",
                details=json.dumps({"uploaded": uploaded, "failed": failed, "bucket": bucket}))
    db.log_event("synced", "r2", project["name"], ppath, total_bytes, project_id,
                 details=f"Upload R2: {uploaded} fichiers ({_fmt(total_bytes)})")
    db.update_project_activity(project_id)

    return {
        "success": status != "failed",
        "message": f"{uploaded} fichiers uploades ({_fmt(total_bytes)})" + (f", {failed} erreurs" if failed else ""),
        "uploaded": uploaded,
        "failed": failed,
        "total_bytes": total_bytes,
        "errors": errors[:10],
    }


def restore_from_r2(project_id, asset_type=None):
    """Download project files from R2."""
    project = db.get_project(project_id)
    if not project or not project.get("path"):
        return {"success": False, "message": "Projet sans dossier"}

    client = _get_r2_client()
    if not client:
        return {"success": False, "message": "R2 non configure"}

    ppath = project["path"]
    bucket = project_bucket(project_id) or db.get_setting("r2_default_bucket", "")
    rules = db.get_asset_rules(project_id)
    r2_rules = [r for r in rules if r["storage_backend"] == "r2"]

    if not bucket:
        # Try to get bucket from rules
        for r in r2_rules:
            if r.get("r2_bucket"):
                bucket = r["r2_bucket"]
                break

    if not bucket:
        return {"success": False, "message": "Pas de bucket R2 configure"}

    # Determine prefix
    prefix = project["name"]
    if asset_type:
        prefix = f"{prefix}/{asset_type}"

    result = cloudflare_r2.r2_download_directory(client, bucket, prefix, ppath)

    db.log_sync(project_id, "restore", "r2", asset_type or "all",
                files_count=result.get("downloaded", 0),
                total_bytes=result.get("total_bytes", 0),
                status="success" if result.get("failed", 0) == 0 else "partial")
    db.log_event("restored", "r2", project["name"], ppath, result.get("total_bytes", 0), project_id,
                 details=f"Restore R2: {result.get('downloaded', 0)} fichiers")

    return {
        "success": True,
        "message": f"{result.get('downloaded', 0)} fichiers restaures ({_fmt(result.get('total_bytes', 0))})",
        **result,
    }


# ═══════════════════════════════════════════════════════════════════════════
# STATUS & SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

def get_sync_status(project_id):
    """Get current sync status for a project."""
    project = db.get_project(project_id)
    if not project:
        return {"error": "Project not found"}

    ppath = project.get("path", "")

    # Git status
    git_status = None
    if ppath and (Path(ppath) / ".git").exists():
        git_status = github_ops.git_status_detailed(ppath)

    # Last syncs
    last_github_push = db.get_last_sync(project_id, "github", "push")
    last_r2_backup = db.get_last_sync(project_id, "r2", "backup")
    last_r2_restore = db.get_last_sync(project_id, "r2", "restore")

    # R2 storage
    r2_stats = db.get_r2_storage_stats(project_id)

    # Asset rules count
    rules = db.get_asset_rules(project_id)

    return {
        "project_name": project["name"],
        "git": git_status,
        "last_github_push": {
            "date": last_github_push["started_at"] if last_github_push else None,
            "files": last_github_push["files_count"] if last_github_push else 0,
        } if last_github_push else None,
        "last_r2_backup": {
            "date": last_r2_backup["started_at"] if last_r2_backup else None,
            "files": last_r2_backup["files_count"] if last_r2_backup else 0,
            "bytes": last_r2_backup["total_bytes"] if last_r2_backup else 0,
        } if last_r2_backup else None,
        "last_r2_restore": {
            "date": last_r2_restore["started_at"] if last_r2_restore else None,
        } if last_r2_restore else None,
        "r2_objects": r2_stats if isinstance(r2_stats, dict) else {"count": 0, "total_size": 0},
        "rules_count": len(rules),
        "has_rules": len(rules) > 0,
    }


def get_project_cloud_summary(project_id):
    """Full cloud overview for a project."""
    sync_status = get_sync_status(project_id)
    history = db.get_sync_history(project_id, limit=10)
    rules = db.get_asset_rules(project_id)
    r2_objects = db.get_r2_objects(project_id)

    # Check connections
    github_token = db.get_setting("github_token", "")
    r2_account = db.get_setting("r2_account_id", "")

    return {
        "sync_status": sync_status,
        "history": history,
        "rules": rules,
        "r2_objects": r2_objects,
        "github_connected": bool(github_token),
        "r2_configured": bool(r2_account),
    }


def _fmt(b):
    """Format bytes to human readable."""
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"
