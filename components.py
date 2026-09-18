"""DevPilot — Project profiles and component management.

Provides a hybrid system where projects have a profile (vitrine, SaaS, API...)
that activates only the relevant components (git, database, storage, backup...).
Components can be auto-detected from project files or manually toggled.
"""

import os
import re
import fnmatch
import db


# ═══════════════════════════════════════════════════════════════════════════
# PROFILES — predefined sets of components per project type
# ═══════════════════════════════════════════════════════════════════════════

PROFILE_COMPONENTS = {
    "vitrine":      ["git", "deploy"],
    "blog_cms":     ["git", "database", "storage", "deploy"],
    "webapp_saas":  ["git", "database", "storage", "backup", "docker", "deploy", "auth", "monitoring", "ci_cd", "secrets"],
    "api_backend":  ["git", "database", "docker", "monitoring", "ci_cd", "secrets"],
    "mobile":       ["git", "database", "storage", "ci_cd", "secrets"],
    "custom":       [],
}

PROFILE_LABELS = {
    "vitrine":      "Site vitrine",
    "blog_cms":     "Blog / CMS",
    "webapp_saas":  "Web App / SaaS",
    "api_backend":  "API / Backend",
    "mobile":       "Mobile",
    "custom":       "Personnalisé",
}

PROFILE_DESCRIPTIONS = {
    "vitrine":      "Site statique, landing page, portfolio",
    "blog_cms":     "Blog, site avec contenu dynamique, CMS",
    "webapp_saas":  "Application web complète avec auth, BDD, cloud",
    "api_backend":  "API REST/GraphQL, microservices",
    "mobile":       "Application mobile (Flutter, React Native...)",
    "custom":       "Choisir les composants manuellement",
}

PROFILE_ICONS = {
    "vitrine":      "globe",
    "blog_cms":     "file-text",
    "webapp_saas":  "layers",
    "api_backend":  "server",
    "mobile":       "smartphone",
    "custom":       "settings",
}


# ═══════════════════════════════════════════════════════════════════════════
# COMPONENTS — all available component types
# ═══════════════════════════════════════════════════════════════════════════

ALL_COMPONENTS = [
    {"key": "git",        "label": "Git / Source Control",  "icon": "branch",    "description": "Gestion du code source avec Git et GitHub"},
    {"key": "database",   "label": "Base de données",       "icon": "database",  "description": "PostgreSQL, SQLite, MongoDB, MySQL"},
    {"key": "storage",    "label": "Stockage cloud",        "icon": "cloud",     "description": "Cloudflare R2, S3 pour médias et fichiers"},
    {"key": "backup",     "label": "Backups",               "icon": "shield",    "description": "Sauvegardes automatiques BDD et fichiers"},
    {"key": "docker",     "label": "Docker",                "icon": "container", "description": "Conteneurs et orchestration Docker"},
    {"key": "deploy",     "label": "Déploiement",           "icon": "rocket",    "description": "Déploiement statique, VPS, Cloudflare Pages"},
    {"key": "auth",       "label": "Authentification",      "icon": "lock",      "description": "Système d'auth (JWT, OAuth, sessions)"},
    {"key": "monitoring", "label": "Monitoring",            "icon": "activity",  "description": "Health checks, logs, métriques"},
    {"key": "ci_cd",      "label": "CI/CD",                 "icon": "refresh",   "description": "Pipelines d'intégration et déploiement continu"},
    {"key": "secrets",    "label": "Secrets / .env",        "icon": "key",       "description": "Gestion des variables d'environnement"},
]

COMPONENT_KEYS = [c["key"] for c in ALL_COMPONENTS]


# ═══════════════════════════════════════════════════════════════════════════
# DETECTION RULES — file/dir patterns that indicate a component is present
# ═══════════════════════════════════════════════════════════════════════════

DETECTION_RULES = {
    "git": {
        "dirs": [".git"],
        "files": [],
        "content_patterns": {},
    },
    "database": {
        "dirs": ["migrations", "alembic", "prisma"],
        "files": ["*.db", "*.sqlite", "*.sqlite3", "schema.prisma",
                  "knexfile.js", "knexfile.ts", "ormconfig.js", "ormconfig.ts",
                  "database.yml", "models.py"],
        "content_patterns": {
            "docker-compose.yml": r"(postgres|mysql|mongo|mariadb|redis)",
            "docker-compose.yaml": r"(postgres|mysql|mongo|mariadb|redis)",
            "requirements.txt": r"(psycopg2|pymongo|sqlalchemy|django|asyncpg|aiosqlite)",
            "package.json": r"(\"pg\"|mysql2|mongoose|prisma|sequelize|typeorm|knex|drizzle)",
            "pubspec.yaml": r"(sqflite|drift|floor|hive)",
        },
    },
    "storage": {
        "dirs": ["uploads", "media", "public/uploads"],
        "files": [],
        "content_patterns": {
            ".env": r"(R2_|S3_|AWS_S3|STORAGE_|BUCKET_)",
            ".env.example": r"(R2_|S3_|AWS_S3|STORAGE_|BUCKET_)",
            "requirements.txt": r"(boto3|django-storages)",
            "package.json": r"(@aws-sdk/client-s3|aws-sdk)",
        },
    },
    "backup": {
        "dirs": ["backups", "backup"],
        "files": ["backup.sh", "*.dump", "backup.py", "md-backup.sh"],
        "content_patterns": {},
    },
    "docker": {
        "dirs": [],
        "files": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml",
                  "docker-compose.dev.yml", "docker-compose.prod.yml",
                  ".dockerignore"],
        "content_patterns": {},
    },
    "deploy": {
        "dirs": ["deploy"],
        "files": ["vercel.json", "netlify.toml", "fly.toml", "render.yaml",
                  "Procfile", "railway.json", "app.yaml", "Caddyfile",
                  "nginx.conf", "wrangler.toml", "cloudflare.json"],
        "content_patterns": {},
    },
    "auth": {
        "dirs": [],
        "files": [],
        "content_patterns": {
            ".env": r"(JWT_SECRET|AUTH0|CLERK|NEXTAUTH|SESSION_SECRET|OAUTH)",
            ".env.example": r"(JWT_SECRET|AUTH0|CLERK|NEXTAUTH|SESSION_SECRET|OAUTH)",
            "requirements.txt": r"(django-allauth|flask-login|python-jose|passlib|PyJWT|authlib)",
            "package.json": r"(next-auth|passport|@auth0|@clerk|lucia|jsonwebtoken|bcrypt)",
        },
    },
    "monitoring": {
        "dirs": [],
        "files": ["sentry.config.js", "sentry.config.ts", "newrelic.js", "datadog.yaml"],
        "content_patterns": {
            ".env": r"(SENTRY_DSN|DATADOG|NEW_RELIC|LOGFLARE)",
            "requirements.txt": r"(sentry-sdk|prometheus-client|structlog)",
            "package.json": r"(@sentry/node|@sentry/react|winston|pino)",
        },
    },
    "ci_cd": {
        "dirs": [".github/workflows", ".gitlab-ci", ".circleci"],
        "files": [".gitlab-ci.yml", "Jenkinsfile", "bitbucket-pipelines.yml",
                  ".travis.yml"],
        "content_patterns": {},
    },
    "secrets": {
        "dirs": [],
        "files": [".env", ".env.local", ".env.production", ".env.development",
                  ".env.example", ".env.template"],
        "content_patterns": {},
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# DETECTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _check_file_pattern(project_path, pattern):
    """Check if a glob pattern matches any file in the project root (1 level deep)."""
    for entry in os.listdir(project_path):
        if fnmatch.fnmatch(entry, pattern):
            return entry
    return None


def _check_content_pattern(project_path, filename, regex):
    """Check if a file exists and its content matches a regex pattern."""
    filepath = os.path.join(project_path, filename)
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, "r", errors="ignore") as f:
            content = f.read(8192)  # read first 8KB
        match = re.search(regex, content)
        if match:
            return match.group(0)
    except (OSError, IOError):
        pass
    return None


def detect_components(project_path):
    """Scan a project directory and detect which components are present.

    Returns dict: {component_key: {"detected": True, "evidence": [...], "config": {...}}}
    """
    if not project_path or not os.path.isdir(project_path):
        return {}

    project_path = os.path.abspath(os.path.expanduser(project_path))
    results = {}

    try:
        entries = set(os.listdir(project_path))
    except OSError:
        return {}

    # Also check immediate subdirectories (monorepo support)
    subdirs = []
    for entry in entries:
        subpath = os.path.join(project_path, entry)
        if os.path.isdir(subpath) and not entry.startswith(".") and entry not in ("node_modules", "__pycache__", "venv", ".venv"):
            subdirs.append((entry, subpath))

    # All paths to scan: root + immediate subdirs
    scan_paths = [("", project_path)] + subdirs

    for component, rules in DETECTION_RULES.items():
        evidence = []
        config = {}

        for prefix, scan_path in scan_paths:
            prefix_label = f"{prefix}/" if prefix else ""

            # Check directories
            for d in rules.get("dirs", []):
                full_path = os.path.join(scan_path, d)
                if os.path.isdir(full_path):
                    evidence.append(f"dossier {prefix_label}{d}/ trouvé")

            # Check files (glob patterns)
            for pattern in rules.get("files", []):
                match = _check_file_pattern(scan_path, pattern)
                if match:
                    evidence.append(f"fichier {prefix_label}{match} trouvé")

            # Check content patterns
            for filename, regex in rules.get("content_patterns", {}).items():
                match = _check_content_pattern(scan_path, filename, regex)
                if match:
                    evidence.append(f"{match} dans {prefix_label}{filename}")

        # Deduplicate evidence
        evidence = list(dict.fromkeys(evidence))

        # Component-specific config detection
        if component == "database" and evidence:
            config = _detect_database_config(project_path, entries, evidence)
        elif component == "docker" and evidence:
            config = _detect_docker_config(project_path, entries)
        elif component == "ci_cd" and evidence:
            config = _detect_ci_config(project_path)
        elif component == "secrets" and evidence:
            config = _detect_secrets_config(project_path, entries)

        if evidence:
            results[component] = {
                "detected": True,
                "evidence": evidence,
                "config": config,
            }

    return results


def _detect_database_config(project_path, entries, evidence):
    """Infer database type from evidence."""
    config = {"type": "unknown"}
    evidence_str = " ".join(evidence).lower()

    if "postgres" in evidence_str or "asyncpg" in evidence_str or "psycopg2" in evidence_str:
        config["type"] = "postgres"
    elif "mongo" in evidence_str or "mongoose" in evidence_str:
        config["type"] = "mongodb"
    elif "mysql" in evidence_str or "mariadb" in evidence_str:
        config["type"] = "mysql"
    elif "sqlite" in evidence_str or "sqflite" in evidence_str or "aiosqlite" in evidence_str:
        config["type"] = "sqlite"
    elif "prisma" in evidence_str:
        config["type"] = "prisma"

    if "migrations" in entries or os.path.isdir(os.path.join(project_path, "alembic")):
        config["has_migrations"] = True

    return config


def _detect_docker_config(project_path, entries):
    """Detect Docker compose file and services."""
    config = {}
    for name in ["docker-compose.yml", "docker-compose.yaml", "docker-compose.dev.yml"]:
        if name in entries:
            config["compose_file"] = name
            # Try to extract service names
            try:
                filepath = os.path.join(project_path, name)
                with open(filepath, "r") as f:
                    content = f.read()
                services = re.findall(r"^\s{2}(\w[\w-]*):", content, re.MULTILINE)
                if services:
                    config["services"] = services
            except (OSError, IOError):
                pass
            break
    return config


def _detect_ci_config(project_path):
    """Detect CI/CD provider."""
    config = {}
    workflows_dir = os.path.join(project_path, ".github", "workflows")
    if os.path.isdir(workflows_dir):
        config["provider"] = "github"
        try:
            config["workflows"] = [f for f in os.listdir(workflows_dir) if f.endswith((".yml", ".yaml"))]
        except OSError:
            pass
    elif os.path.isfile(os.path.join(project_path, ".gitlab-ci.yml")):
        config["provider"] = "gitlab"
    elif os.path.isdir(os.path.join(project_path, ".circleci")):
        config["provider"] = "circleci"
    return config


def _detect_secrets_config(project_path, entries):
    """List .env files found."""
    config = {"env_files": []}
    for entry in sorted(entries):
        if entry.startswith(".env") and os.path.isfile(os.path.join(project_path, entry)):
            config["env_files"].append(entry)
    return config


# ═══════════════════════════════════════════════════════════════════════════
# PROFILE SUGGESTION
# ═══════════════════════════════════════════════════════════════════════════

def detect_and_suggest_profile(project_path):
    """Detect components and suggest the best matching profile.

    Returns {"detected": {...}, "suggested_profile": str, "match_scores": {...}}
    """
    detected = detect_components(project_path)
    detected_keys = set(detected.keys())

    if not detected_keys:
        return {
            "detected": detected,
            "suggested_profile": "vitrine",
            "match_scores": {},
        }

    # Score each profile by overlap with detected components
    scores = {}
    for profile, profile_comps in PROFILE_COMPONENTS.items():
        if profile == "custom":
            continue
        profile_set = set(profile_comps)
        if not profile_set:
            scores[profile] = 0
            continue
        # Jaccard-like score: intersection / union
        intersection = len(detected_keys & profile_set)
        union = len(detected_keys | profile_set)
        scores[profile] = round(intersection / union, 2) if union > 0 else 0

    best_profile = max(scores, key=scores.get) if scores else "custom"
    # If score is too low, suggest custom
    if scores.get(best_profile, 0) < 0.2:
        best_profile = "custom"

    return {
        "detected": detected,
        "suggested_profile": best_profile,
        "match_scores": scores,
    }


# ═══════════════════════════════════════════════════════════════════════════
# PROFILE APPLICATION
# ═══════════════════════════════════════════════════════════════════════════

def apply_profile(project_id, profile, detected=None):
    """Apply a profile to a project: set the profile and create component rows.

    If detected is provided (from detect_components), merge detected config
    into the components.
    """
    detected = detected or {}

    # Set the profile on the project
    db.set_project_profile(project_id, profile)

    # Get the components list for this profile
    if profile == "custom":
        # For custom, use detected components
        components_to_add = list(detected.keys()) if detected else []
    else:
        components_to_add = PROFILE_COMPONENTS.get(profile, [])

    # Create component rows
    for comp_key in components_to_add:
        is_detected = comp_key in detected
        config = detected[comp_key].get("config", {}) if is_detected else {}
        db.add_project_component(
            project_id=project_id,
            component=comp_key,
            enabled=True,
            config=config,
            detected=is_detected,
        )

    # Also add detected components that aren't in the profile (as disabled)
    for comp_key, comp_data in detected.items():
        if comp_key not in components_to_add:
            db.add_project_component(
                project_id=project_id,
                component=comp_key,
                enabled=False,
                config=comp_data.get("config", {}),
                detected=True,
            )

    return components_to_add


def set_component(project_id, component, enabled=True, config=None):
    """Enable or disable a component for a project."""
    if component not in COMPONENT_KEYS:
        return False
    existing = db.get_project_component(project_id, component)
    if existing:
        kwargs = {"enabled": enabled}
        if config is not None:
            kwargs["config"] = config
        db.update_project_component(project_id, component, **kwargs)
    else:
        db.add_project_component(project_id, component, enabled=enabled, config=config)
    return True


def get_project_components(project_id):
    """Get all components for a project with full metadata."""
    rows = db.get_project_components(project_id)
    # Enrich with component metadata
    comp_map = {c["key"]: c for c in ALL_COMPONENTS}
    result = []
    for row in rows:
        meta = comp_map.get(row["component"], {})
        result.append({
            "component": row["component"],
            "enabled": bool(row["enabled"]),
            "config": row["config"],
            "detected": bool(row["detected"]),
            "label": meta.get("label", row["component"]),
            "icon": meta.get("icon", ""),
            "description": meta.get("description", ""),
        })
    return result


def get_active_component_keys(project_id):
    """Get just the list of active component keys for a project."""
    rows = db.get_project_components(project_id)
    return [r["component"] for r in rows if r["enabled"]]
