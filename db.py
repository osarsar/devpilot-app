"""DevPilot — Database layer for project lifecycle management."""

import sqlite3
import os
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager

DEVPILOT_ROOT = Path.home() / "devpilot"
DB_PATH = DEVPILOT_ROOT / ".devpilot" / "data" / "devpilot.db"

# In-memory cache of project paths for fast lookup
_project_paths_cache = None
_project_paths_cache_time = None


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                color TEXT DEFAULT '#8b5cf6',
                icon TEXT DEFAULT '',
                description TEXT DEFAULT '',
                path TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                git_remote TEXT DEFAULT '',
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                path TEXT,
                name TEXT NOT NULL,
                size INTEGER DEFAULT 0,
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                is_temporary INTEGER DEFAULT 0,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                resource_type TEXT,
                resource_name TEXT,
                resource_path TEXT,
                size INTEGER DEFAULT 0,
                project_id INTEGER,
                details TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notified INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                resource_type TEXT DEFAULT 'any',
                auto_temporary INTEGER DEFAULT 0,
                expire_days INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS port_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                port INTEGER NOT NULL,
                pid INTEGER,
                process_name TEXT DEFAULT '',
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS project_specs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                wizard_data TEXT DEFAULT '{}',
                checklist TEXT DEFAULT '[]',
                prompt TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- Defaults
            INSERT OR IGNORE INTO settings (key, value) VALUES ('watch_downloads', '1');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('watch_desktop', '1');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('watch_docker', '1');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('temp_expire_days', '7');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_enabled', '1');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('scan_dirs', '~/Desktop');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('large_file_threshold_mb', '100');

            CREATE INDEX IF NOT EXISTS idx_resources_project ON resources(project_id);
            CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(status);
            CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
            CREATE INDEX IF NOT EXISTS idx_resources_path ON resources(path);
            CREATE INDEX IF NOT EXISTS idx_port_project ON port_tracking(project_id);
            CREATE INDEX IF NOT EXISTS idx_port_status ON port_tracking(status);

            -- ═══════════════════════════════════════════════════════════
            -- CLOUD INTEGRATION TABLES
            -- ═══════════════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS asset_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                asset_type TEXT NOT NULL,
                storage_backend TEXT NOT NULL,
                glob_pattern TEXT NOT NULL,
                r2_bucket TEXT DEFAULT '',
                r2_prefix TEXT DEFAULT '',
                is_public INTEGER DEFAULT 0,
                auto_sync INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, glob_pattern)
            );

            CREATE TABLE IF NOT EXISTS sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                direction TEXT NOT NULL,
                backend TEXT NOT NULL,
                asset_type TEXT DEFAULT '',
                files_count INTEGER DEFAULT 0,
                total_bytes INTEGER DEFAULT 0,
                status TEXT DEFAULT 'success',
                error_message TEXT DEFAULT '',
                details TEXT DEFAULT '',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS r2_objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                bucket TEXT NOT NULL,
                key TEXT NOT NULL,
                size INTEGER DEFAULT 0,
                content_type TEXT DEFAULT '',
                etag TEXT DEFAULT '',
                last_modified TIMESTAMP,
                asset_type TEXT DEFAULT '',
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, bucket, key)
            );

            CREATE INDEX IF NOT EXISTS idx_asset_rules_project ON asset_rules(project_id);
            CREATE INDEX IF NOT EXISTS idx_sync_history_project ON sync_history(project_id);
            CREATE INDEX IF NOT EXISTS idx_sync_history_created ON sync_history(started_at);
            CREATE INDEX IF NOT EXISTS idx_r2_objects_project ON r2_objects(project_id);

            -- ═══════════════════════════════════════════════════════════
            -- PROJECT COMPONENTS TABLE
            -- ═══════════════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS project_components (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                component TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                config TEXT DEFAULT '{}',
                detected INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, component)
            );

            CREATE INDEX IF NOT EXISTS idx_project_components_project ON project_components(project_id);

            -- Cloud settings defaults
            INSERT OR IGNORE INTO settings (key, value) VALUES ('r2_account_id', '');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('r2_access_key_id', '');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('r2_secret_access_key', '');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('r2_default_bucket', '');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_backup_enabled', '0');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_backup_interval_hours', '24');
        """)

        # Migrations for existing DB
        for col, default in [("status", "'active'"), ("git_remote", "''"), ("last_activity", "''"), ("profile", "''")]:
            try:
                db.execute(f"SELECT {col} FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                db.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT DEFAULT {default}")

        try:
            db.execute("SELECT path FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            db.execute("ALTER TABLE projects ADD COLUMN path TEXT DEFAULT ''")


# ═══════════════════════════════════════════════════════════════════════════
# PROJECT PATH MATCHING — the core function replacing hardcoded regex
# ═══════════════════════════════════════════════════════════════════════════

def _refresh_project_paths_cache():
    """Rebuild the in-memory cache of project paths."""
    global _project_paths_cache, _project_paths_cache_time
    with get_db() as db:
        rows = db.execute("SELECT id, name, path FROM projects WHERE path != '' AND status != 'archived'").fetchall()
        _project_paths_cache = [(r["id"], r["name"], r["path"]) for r in rows]
        _project_paths_cache_time = datetime.now()


def invalidate_project_cache():
    """Call this when projects are created/deleted/modified."""
    global _project_paths_cache
    _project_paths_cache = None


def find_project_by_path(file_path):
    """Given any file path, find the project it belongs to by walking up directories.
    Returns (project_id, project_name) or (None, None)."""
    global _project_paths_cache, _project_paths_cache_time

    # Refresh cache if stale (>30s) or empty
    if _project_paths_cache is None or (datetime.now() - _project_paths_cache_time).seconds > 30:
        _refresh_project_paths_cache()

    if not _project_paths_cache:
        return None, None

    file_path = os.path.abspath(file_path)

    # Check if file_path is inside any project's directory
    for pid, pname, ppath in _project_paths_cache:
        ppath_abs = os.path.abspath(os.path.expanduser(ppath))
        if file_path.startswith(ppath_abs + "/") or file_path == ppath_abs:
            return pid, pname

    # Check if file is a sibling of a project (same parent dir)
    file_parent = os.path.dirname(file_path)
    for pid, pname, ppath in _project_paths_cache:
        ppath_abs = os.path.abspath(os.path.expanduser(ppath))
        if os.path.dirname(ppath_abs) == file_parent:
            # File name contains project name?
            fname = os.path.basename(file_path).lower()
            if pname.lower() in fname:
                return pid, pname

    return None, None


def find_project_by_name_hint(name):
    """Match a Docker resource name to a project using project names/dir names.
    Returns (project_id, project_name) or (None, None)."""
    global _project_paths_cache, _project_paths_cache_time

    if _project_paths_cache is None or (datetime.now() - _project_paths_cache_time).seconds > 30:
        _refresh_project_paths_cache()

    if not _project_paths_cache:
        return None, None

    name_lower = name.lower().replace("-", "").replace("_", "")

    for pid, pname, ppath in _project_paths_cache:
        # Match against project name
        pname_norm = pname.lower().replace("-", "").replace("_", "")
        if pname_norm in name_lower or name_lower in pname_norm:
            return pid, pname
        # Match against directory basename
        if ppath:
            dirname = os.path.basename(ppath).lower().replace("-", "").replace("_", "")
            if dirname and (dirname in name_lower or name_lower in dirname):
                return pid, pname

    return None, None


# ═══════════════════════════════════════════════════════════════════════════
# PROJECTS
# ═══════════════════════════════════════════════════════════════════════════

def create_project(name, color="#8b5cf6", icon="", description="", path="", git_remote="", status="active"):
    invalidate_project_cache()
    with get_db() as db:
        db.execute(
            "INSERT INTO projects (name, color, icon, description, path, git_remote, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, color, icon, description, path, git_remote, status),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_projects(status_filter=None):
    with get_db() as db:
        if status_filter:
            projects = db.execute("SELECT * FROM projects WHERE status = ? ORDER BY last_activity DESC", (status_filter,)).fetchall()
        else:
            projects = db.execute("SELECT * FROM projects ORDER BY last_activity DESC").fetchall()
        result = []
        for p in projects:
            stats = db.execute("""
                SELECT COUNT(*) as count, COALESCE(SUM(size), 0) as total_size
                FROM resources WHERE project_id = ? AND status = 'active'
            """, (p["id"],)).fetchone()
            port_count = db.execute(
                "SELECT COUNT(*) as c FROM port_tracking WHERE project_id = ? AND status = 'active'",
                (p["id"],)
            ).fetchone()["c"]
            result.append({
                **dict(p),
                "resource_count": stats["count"],
                "total_size": stats["total_size"],
                "port_count": port_count,
            })
        return result


def get_project(project_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None


def update_project(project_id, **kwargs):
    allowed = {"name", "color", "icon", "description", "path", "status", "git_remote", "profile"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    invalidate_project_cache()
    with get_db() as db:
        db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", [*fields.values(), project_id])


def update_project_activity(project_id):
    """Update last_activity timestamp for a project."""
    with get_db() as db:
        db.execute("UPDATE projects SET last_activity = ? WHERE id = ?",
                   (datetime.now().isoformat(), project_id))


def delete_project(project_id):
    invalidate_project_cache()
    with get_db() as db:
        db.execute("UPDATE resources SET project_id = NULL WHERE project_id = ?", (project_id,))
        db.execute("DELETE FROM rules WHERE project_id = ?", (project_id,))
        db.execute("DELETE FROM port_tracking WHERE project_id = ?", (project_id,))
        db.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def set_project_status(project_id, status):
    """Change project lifecycle status: active, paused, done."""
    with get_db() as db:
        db.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))


# ═══════════════════════════════════════════════════════════════════════════
# RESOURCES
# ═══════════════════════════════════════════════════════════════════════════

def add_resource(type, name, path=None, size=0, project_id=None, is_temporary=False, expire_days=0):
    expires_at = None
    if is_temporary and expire_days > 0:
        expires_at = (datetime.now() + timedelta(days=expire_days)).isoformat()
    with get_db() as db:
        if path:
            existing = db.execute("SELECT id FROM resources WHERE path = ? AND status = 'active'", (path,)).fetchone()
            if existing:
                return existing["id"]
        db.execute(
            """INSERT INTO resources (type, name, path, size, project_id, is_temporary, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (type, name, path, size, project_id, int(is_temporary), expires_at),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_resources(project_id=None, type=None, status="active"):
    with get_db() as db:
        query = "SELECT r.*, p.name as project_name, p.color as project_color FROM resources r LEFT JOIN projects p ON r.project_id = p.id WHERE r.status = ?"
        params = [status]
        if project_id:
            query += " AND r.project_id = ?"
            params.append(project_id)
        if type:
            query += " AND r.type = ?"
            params.append(type)
        query += " ORDER BY r.created_at DESC"
        return [dict(row) for row in db.execute(query, params).fetchall()]


def get_resource_by_path(path):
    with get_db() as db:
        row = db.execute("SELECT * FROM resources WHERE path = ? AND status = 'active'", (path,)).fetchone()
        return dict(row) if row else None


def update_resource(resource_id, **kwargs):
    allowed = {"project_id", "is_temporary", "expires_at", "status", "size", "name"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_db() as db:
        db.execute(f"UPDATE resources SET {set_clause} WHERE id = ?", [*fields.values(), resource_id])


def link_resource_to_project(resource_id, project_id):
    with get_db() as db:
        db.execute("UPDATE resources SET project_id = ? WHERE id = ?", (project_id, resource_id))


def set_resource_temporary(resource_id, expire_days=7):
    expires_at = (datetime.now() + timedelta(days=expire_days)).isoformat()
    with get_db() as db:
        db.execute("UPDATE resources SET is_temporary = 1, expires_at = ? WHERE id = ?", (expires_at, resource_id))


def set_resource_permanent(resource_id):
    with get_db() as db:
        db.execute("UPDATE resources SET is_temporary = 0, expires_at = NULL WHERE id = ?", (resource_id,))


def get_expired_resources():
    now = datetime.now().isoformat()
    with get_db() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM resources WHERE is_temporary = 1 AND expires_at <= ? AND status = 'active'",
            (now,),
        ).fetchall()]


def get_project_resources_breakdown(project_id):
    with get_db() as db:
        rows = db.execute("""
            SELECT type, COUNT(*) as count, COALESCE(SUM(size), 0) as total_size
            FROM resources WHERE project_id = ? AND status = 'active'
            GROUP BY type
        """, (project_id,)).fetchall()
        return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# PROJECT SPECS (wizard data, checklist, prompt)
# ═══════════════════════════════════════════════════════════════════════════

def save_project_specs(project_id, wizard_data, checklist, prompt):
    import json as _json
    with get_db() as db:
        existing = db.execute("SELECT id FROM project_specs WHERE project_id = ?", (project_id,)).fetchone()
        wd = _json.dumps(wizard_data) if isinstance(wizard_data, dict) else wizard_data
        cl = _json.dumps(checklist) if isinstance(checklist, list) else checklist
        if existing:
            db.execute("UPDATE project_specs SET wizard_data = ?, checklist = ?, prompt = ? WHERE project_id = ?",
                       (wd, cl, prompt, project_id))
        else:
            db.execute("INSERT INTO project_specs (project_id, wizard_data, checklist, prompt) VALUES (?, ?, ?, ?)",
                       (project_id, wd, cl, prompt))


def get_project_specs(project_id):
    import json as _json
    with get_db() as db:
        row = db.execute("SELECT * FROM project_specs WHERE project_id = ?", (project_id,)).fetchone()
        if not row:
            return None
        r = dict(row)
        try:
            r["wizard_data"] = _json.loads(r["wizard_data"])
        except (ValueError, TypeError):
            r["wizard_data"] = {}
        try:
            r["checklist"] = _json.loads(r["checklist"])
        except (ValueError, TypeError):
            r["checklist"] = []
        return r


def update_checklist(project_id, checklist):
    import json as _json
    with get_db() as db:
        db.execute("UPDATE project_specs SET checklist = ? WHERE project_id = ?",
                   (_json.dumps(checklist), project_id))


# ═══════════════════════════════════════════════════════════════════════════
# PORT TRACKING
# ═══════════════════════════════════════════════════════════════════════════

def add_or_update_port(port, pid=None, process_name="", project_id=None):
    """Register or update a port. Returns the tracking ID."""
    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM port_tracking WHERE port = ? AND status = 'active'", (port,)
        ).fetchone()
        now = datetime.now().isoformat()
        if existing:
            db.execute(
                "UPDATE port_tracking SET last_seen = ?, pid = ?, process_name = ?, project_id = COALESCE(?, project_id) WHERE id = ?",
                (now, pid, process_name, project_id, existing["id"])
            )
            return existing["id"]
        else:
            db.execute(
                "INSERT INTO port_tracking (port, pid, process_name, project_id) VALUES (?, ?, ?, ?)",
                (port, pid, process_name, project_id)
            )
            return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_project_ports(project_id):
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM port_tracking WHERE project_id = ? AND status = 'active' ORDER BY port",
            (project_id,)
        ).fetchall()]


def close_stale_ports(active_ports):
    """Mark ports that are no longer listening as closed."""
    with get_db() as db:
        tracked = db.execute("SELECT id, port FROM port_tracking WHERE status = 'active'").fetchall()
        for t in tracked:
            if t["port"] not in active_ports:
                db.execute("UPDATE port_tracking SET status = 'closed' WHERE id = ?", (t["id"],))


def get_all_active_ports():
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT pt.*, p.name as project_name, p.color as project_color FROM port_tracking pt LEFT JOIN projects p ON pt.project_id = p.id WHERE pt.status = 'active' ORDER BY pt.port"
        ).fetchall()]


# ═══════════════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════════════

def log_event(type, resource_type="", resource_name="", resource_path="", size=0, project_id=None, details=""):
    with get_db() as db:
        db.execute(
            """INSERT INTO events (type, resource_type, resource_name, resource_path, size, project_id, details)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (type, resource_type, resource_name, resource_path, size, project_id, details),
        )


def get_events(limit=50, since_hours=None):
    with get_db() as db:
        query = "SELECT e.*, p.name as project_name FROM events e LEFT JOIN projects p ON e.project_id = p.id"
        params = []
        if since_hours:
            since = (datetime.now() - timedelta(hours=since_hours)).isoformat()
            query += " WHERE e.created_at >= ?"
            params.append(since)
        query += " ORDER BY e.created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in db.execute(query, params).fetchall()]


def get_unnotified_events():
    with get_db() as db:
        rows = db.execute("SELECT * FROM events WHERE notified = 0 ORDER BY created_at DESC").fetchall()
        result = [dict(r) for r in rows]
        if result:
            db.execute("UPDATE events SET notified = 1 WHERE notified = 0")
        return result


# ═══════════════════════════════════════════════════════════════════════════
# RULES
# ═══════════════════════════════════════════════════════════════════════════

def add_rule(pattern, project_id, resource_type="any", auto_temporary=False, expire_days=0):
    with get_db() as db:
        db.execute(
            "INSERT INTO rules (pattern, project_id, resource_type, auto_temporary, expire_days) VALUES (?, ?, ?, ?, ?)",
            (pattern, project_id, resource_type, int(auto_temporary), expire_days),
        )


def get_rules():
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT r.*, p.name as project_name FROM rules r JOIN projects p ON r.project_id = p.id"
        ).fetchall()]


def delete_rule(rule_id):
    with get_db() as db:
        db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


def match_rules(name, resource_type="any"):
    import re
    with get_db() as db:
        rules = db.execute("SELECT * FROM rules").fetchall()
        for rule in rules:
            if rule["resource_type"] not in ("any", resource_type):
                continue
            try:
                if re.search(rule["pattern"], name, re.IGNORECASE):
                    return dict(rule)
            except re.error:
                pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# SETTINGS
# ═══════════════════════════════════════════════════════════════════════════

def get_setting(key, default=None):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


def get_all_settings():
    with get_db() as db:
        return {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}


# ═══════════════════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════════════════

def get_stats():
    with get_db() as db:
        total_resources = db.execute("SELECT COUNT(*) as c FROM resources WHERE status='active'").fetchone()["c"]
        total_temp = db.execute("SELECT COUNT(*) as c FROM resources WHERE status='active' AND is_temporary=1").fetchone()["c"]
        total_expired = len(get_expired_resources())
        total_projects = db.execute("SELECT COUNT(*) as c FROM projects").fetchone()["c"]
        active_projects = db.execute("SELECT COUNT(*) as c FROM projects WHERE status='active'").fetchone()["c"]
        total_events_24h = db.execute(
            "SELECT COUNT(*) as c FROM events WHERE created_at >= datetime('now', '-1 day')"
        ).fetchone()["c"]
        unlinked = db.execute(
            "SELECT COUNT(*) as c FROM resources WHERE project_id IS NULL AND status='active'"
        ).fetchone()["c"]
        active_ports = db.execute(
            "SELECT COUNT(*) as c FROM port_tracking WHERE status='active'"
        ).fetchone()["c"]

        return {
            "total_resources": total_resources,
            "total_temporary": total_temp,
            "total_expired": total_expired,
            "total_projects": total_projects,
            "active_projects": active_projects,
            "events_24h": total_events_24h,
            "unlinked_resources": unlinked,
            "active_ports": active_ports,
        }


# ═══════════════════════════════════════════════════════════════════════════
# ASSET RULES
# ═══════════════════════════════════════════════════════════════════════════

def add_asset_rule(project_id, asset_type, storage_backend, glob_pattern, r2_bucket="", r2_prefix="", is_public=False, auto_sync=False):
    with get_db() as db:
        db.execute(
            """INSERT OR IGNORE INTO asset_rules
               (project_id, asset_type, storage_backend, glob_pattern, r2_bucket, r2_prefix, is_public, auto_sync)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, asset_type, storage_backend, glob_pattern, r2_bucket, r2_prefix, int(is_public), int(auto_sync)),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_asset_rules(project_id=None):
    with get_db() as db:
        if project_id:
            rows = db.execute(
                "SELECT ar.*, p.name as project_name FROM asset_rules ar JOIN projects p ON ar.project_id = p.id WHERE ar.project_id = ? ORDER BY ar.asset_type",
                (project_id,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT ar.*, p.name as project_name FROM asset_rules ar JOIN projects p ON ar.project_id = p.id ORDER BY ar.project_id, ar.asset_type"
            ).fetchall()
        return [dict(r) for r in rows]


def get_asset_rules_for_type(project_id, asset_type):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM asset_rules WHERE project_id = ? AND asset_type = ?",
            (project_id, asset_type)
        ).fetchall()
        return [dict(r) for r in rows]


def update_asset_rule(rule_id, **kwargs):
    allowed = {"asset_type", "storage_backend", "glob_pattern", "r2_bucket", "r2_prefix", "is_public", "auto_sync"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_db() as db:
        db.execute(f"UPDATE asset_rules SET {set_clause} WHERE id = ?", [*fields.values(), rule_id])


def delete_asset_rule(rule_id):
    with get_db() as db:
        db.execute("DELETE FROM asset_rules WHERE id = ?", (rule_id,))


# ═══════════════════════════════════════════════════════════════════════════
# SYNC HISTORY
# ═══════════════════════════════════════════════════════════════════════════

def log_sync(project_id, direction, backend, asset_type="", files_count=0, total_bytes=0, status="success", error_message="", details=""):
    with get_db() as db:
        db.execute(
            """INSERT INTO sync_history
               (project_id, direction, backend, asset_type, files_count, total_bytes, status, error_message, details, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, direction, backend, asset_type, files_count, total_bytes, status, error_message, details,
             datetime.now().isoformat() if status != "in_progress" else None),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_sync_history(project_id=None, limit=50):
    with get_db() as db:
        if project_id:
            rows = db.execute(
                "SELECT sh.*, p.name as project_name FROM sync_history sh JOIN projects p ON sh.project_id = p.id WHERE sh.project_id = ? ORDER BY sh.started_at DESC LIMIT ?",
                (project_id, limit)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT sh.*, p.name as project_name FROM sync_history sh JOIN projects p ON sh.project_id = p.id ORDER BY sh.started_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_last_sync(project_id, backend, direction="push"):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM sync_history WHERE project_id = ? AND backend = ? AND direction = ? AND status = 'success' ORDER BY started_at DESC LIMIT 1",
            (project_id, backend, direction)
        ).fetchone()
        return dict(row) if row else None


# ═══════════════════════════════════════════════════════════════════════════
# R2 OBJECTS
# ═══════════════════════════════════════════════════════════════════════════

def upsert_r2_object(project_id, bucket, key, size=0, content_type="", etag="", last_modified=None, asset_type=""):
    with get_db() as db:
        db.execute(
            """INSERT INTO r2_objects (project_id, bucket, key, size, content_type, etag, last_modified, asset_type, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, bucket, key)
               DO UPDATE SET size=excluded.size, content_type=excluded.content_type, etag=excluded.etag,
                             last_modified=excluded.last_modified, asset_type=excluded.asset_type, synced_at=excluded.synced_at""",
            (project_id, bucket, key, size, content_type, etag, last_modified, asset_type, datetime.now().isoformat()),
        )


def get_r2_objects(project_id, bucket=None, asset_type=None):
    with get_db() as db:
        query = "SELECT * FROM r2_objects WHERE project_id = ?"
        params = [project_id]
        if bucket:
            query += " AND bucket = ?"
            params.append(bucket)
        if asset_type:
            query += " AND asset_type = ?"
            params.append(asset_type)
        query += " ORDER BY key"
        return [dict(r) for r in db.execute(query, params).fetchall()]


def delete_r2_object(project_id, bucket, key):
    with get_db() as db:
        db.execute("DELETE FROM r2_objects WHERE project_id = ? AND bucket = ? AND key = ?",
                   (project_id, bucket, key))


def clear_r2_objects(project_id, bucket=None):
    with get_db() as db:
        if bucket:
            db.execute("DELETE FROM r2_objects WHERE project_id = ? AND bucket = ?", (project_id, bucket))
        else:
            db.execute("DELETE FROM r2_objects WHERE project_id = ?", (project_id,))


def get_r2_storage_stats(project_id=None):
    """Get R2 storage totals, optionally per project."""
    with get_db() as db:
        if project_id:
            row = db.execute(
                "SELECT COUNT(*) as count, COALESCE(SUM(size), 0) as total_size FROM r2_objects WHERE project_id = ?",
                (project_id,)
            ).fetchone()
            return dict(row)
        else:
            rows = db.execute(
                """SELECT r2.project_id, p.name as project_name, COUNT(*) as count, COALESCE(SUM(r2.size), 0) as total_size
                   FROM r2_objects r2 JOIN projects p ON r2.project_id = p.id
                   GROUP BY r2.project_id ORDER BY total_size DESC"""
            ).fetchall()
            return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# PROJECT COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════

def add_project_component(project_id, component, enabled=True, config=None, detected=False):
    import json as _json
    with get_db() as db:
        db.execute(
            """INSERT INTO project_components (project_id, component, enabled, config, detected)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(project_id, component)
               DO UPDATE SET enabled=excluded.enabled, config=excluded.config, detected=excluded.detected""",
            (project_id, component, int(enabled), _json.dumps(config or {}), int(detected)),
        )


def get_project_components(project_id):
    import json as _json
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM project_components WHERE project_id = ? ORDER BY component",
            (project_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["config"] = _json.loads(d["config"])
            except (ValueError, TypeError):
                d["config"] = {}
            result.append(d)
        return result


def get_project_component(project_id, component):
    import json as _json
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM project_components WHERE project_id = ? AND component = ?",
            (project_id, component)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["config"] = _json.loads(d["config"])
        except (ValueError, TypeError):
            d["config"] = {}
        return d


def update_project_component(project_id, component, **kwargs):
    import json as _json
    allowed = {"enabled", "config", "detected"}
    fields = {}
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k == "config":
            fields[k] = _json.dumps(v) if isinstance(v, dict) else v
        elif k in ("enabled", "detected"):
            fields[k] = int(v)
        else:
            fields[k] = v
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_db() as db:
        db.execute(
            f"UPDATE project_components SET {set_clause} WHERE project_id = ? AND component = ?",
            [*fields.values(), project_id, component]
        )


def delete_project_component(project_id, component):
    with get_db() as db:
        db.execute(
            "DELETE FROM project_components WHERE project_id = ? AND component = ?",
            (project_id, component)
        )


def set_project_profile(project_id, profile):
    with get_db() as db:
        db.execute("UPDATE projects SET profile = ? WHERE id = ?", (profile, project_id))


# Init on import
init_db()
