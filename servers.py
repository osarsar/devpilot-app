"""DevPilot — the servers of a project (VPS / machines reached over SSH).

A project has a list of servers, each with a role (prod, staging, gpu...):
  {id, name, role, provider, host, user, port, key_path, remote_path, urls,
   dashboard_url, test: {ok, at, hostname, os, disk, docker, path_exists}, history: [...]}
The list lives in the project's folder (.devpilot/project.json "servers") and
is mirrored into the "server" component for the rest of DevPilot. Only the
PATH of a key is stored, never a key. Host keys are checked (accept-new: the
first connection records the server's identity, a change is refused).
"""

import json
import os
import re
import shlex
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import db
import projects as P
from projects import ProjectError

SSH_DIR = Path.home() / ".ssh"
KEYS_DIR = SSH_DIR / "devpilot"

PROVIDERS = {
    "ovh":          {"label": "OVH",          "user": "ubuntu", "dashboard": "https://www.ovh.com/manager/"},
    "hetzner":      {"label": "Hetzner",      "user": "root",   "dashboard": "https://console.hetzner.cloud/"},
    "digitalocean": {"label": "DigitalOcean", "user": "root",   "dashboard": "https://cloud.digitalocean.com/droplets"},
    "scaleway":     {"label": "Scaleway",     "user": "root",   "dashboard": "https://console.scaleway.com/"},
    "contabo":      {"label": "Contabo",      "user": "root",   "dashboard": "https://my.contabo.com/"},
    "aws":          {"label": "AWS EC2",      "user": "ubuntu", "dashboard": "https://console.aws.amazon.com/ec2/"},
    "gcp":          {"label": "Google Cloud", "user": "",       "dashboard": "https://console.cloud.google.com/compute"},
    "azure":        {"label": "Azure",        "user": "azureuser", "dashboard": "https://portal.azure.com/"},
    "local":        {"label": "Machine locale / VM", "user": "", "dashboard": ""},
    "other":        {"label": "Autre",        "user": "",       "dashboard": ""},
}
ROLES = ["prod", "staging", "dev", "gpu", "backup", "autre"]

_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
                      r"|^\d{1,3}(?:\.\d{1,3}){3}$|^\[?[0-9A-Fa-f:]+\]?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


# ── Validation ──────────────────────────────────────────────────────────────

def _clean(cfg):
    """Validated server config (raises ProjectError with a clear message)."""
    c = {k: (cfg.get(k) or "") if isinstance(cfg.get(k), str) or cfg.get(k) is None else cfg.get(k)
         for k in ("name", "role", "provider", "host", "user", "port", "key_path", "remote_path",
                   "urls", "dashboard_url")}
    c["name"] = str(c["name"]).strip()[:60]
    c["role"] = str(c["role"]).strip().lower()[:20] or "prod"
    c["provider"] = str(c["provider"]).strip().lower() or "other"
    if c["provider"] not in PROVIDERS:
        c["provider"] = "other"
    c["host"] = str(c["host"]).strip()
    c["user"] = str(c["user"]).strip()
    c["remote_path"] = str(c["remote_path"]).strip()
    if not c["name"]:
        raise ProjectError("Donne un nom au serveur (ex. VPS prod)")
    if not c["host"] or c["host"].startswith("-") or not _HOST_RE.match(c["host"]):
        raise ProjectError(f"Adresse invalide : « {c['host']} » (IP ou nom de domaine)")
    if not _USER_RE.match(c["user"]):
        raise ProjectError(f"Utilisateur SSH invalide : « {c['user']} »")
    try:
        c["port"] = int(c["port"] or 22)
        assert 1 <= c["port"] <= 65535
    except (ValueError, AssertionError):
        raise ProjectError("Port SSH invalide")
    key = str(c["key_path"]).strip()
    if key:
        kp = Path(os.path.expanduser(key)).resolve()
        if not kp.is_relative_to(SSH_DIR.resolve()) or not kp.is_file():
            raise ProjectError(f"Cle introuvable dans ~/.ssh : {key}")
        c["key_path"] = str(kp)
    if any(ch in c["remote_path"] for ch in "\n\r\0"):
        raise ProjectError("Dossier sur le serveur invalide")
    urls = c["urls"]
    if isinstance(urls, str):
        urls = [u.strip() for u in re.split(r"[\s,]+", urls) if u.strip()]
    c["urls"] = [u for u in (urls or []) if re.match(r"^https?://", u)][:10]
    c["dashboard_url"] = str(c["dashboard_url"]).strip() or PROVIDERS[c["provider"]]["dashboard"]
    return c


# ── Storage: .devpilot/project.json "servers" (+ mirror in the DB) ─────────

def _root(pid):
    project = P.get(pid)
    if not project.get("path") or not os.path.isdir(project["path"]):
        raise ProjectError("Le dossier du projet est introuvable")
    return project


def list_servers(pid):
    project = _root(pid)
    return P.read_manifest(project["path"]).get("servers") or []


def _save(pid, servers):
    project = _root(pid)
    P.write_space(pid)
    m = P.read_manifest(project["path"])
    m["servers"] = servers
    P._write_json(P.space_dir(project["path"]) / "project.json", m)
    public = [{k: s.get(k) for k in ("id", "name", "role", "provider", "host", "user", "port",
                                      "remote_path", "urls", "dashboard_url")} for s in servers]
    comp = db.get_project_component(pid, "server")
    if servers:
        if comp:
            db.update_project_component(pid, "server", config={"servers": public}, enabled=True)
        else:
            db.add_project_component(pid, "server", enabled=True, config={"servers": public})
    elif comp:
        db.update_project_component(pid, "server", config={"servers": []}, enabled=False)
    return servers


def _find(servers, sid):
    for s in servers:
        if s["id"] == sid:
            return s
    raise ProjectError("Serveur introuvable")


def add_server(pid, cfg, test=True):
    servers = list_servers(pid)
    c = _clean(cfg)
    if any(s["name"].lower() == c["name"].lower() for s in servers):
        raise ProjectError(f"Un serveur « {c['name']} » existe deja dans ce projet")
    c["id"] = uuid.uuid4().hex[:8]
    c["history"] = []
    c["test"] = test_connection(c) if test else None
    servers.append(c)
    _save(pid, servers)
    return c


def update_server(pid, sid, cfg, test=True):
    """Change a server (other IP, provider, user...). The previous config goes to its history."""
    servers = list_servers(pid)
    s = _find(servers, sid)
    c = _clean({**s, **cfg})
    if any(o["id"] != sid and o["name"].lower() == c["name"].lower() for o in servers):
        raise ProjectError(f"Un serveur « {c['name']} » existe deja dans ce projet")
    before = {k: s.get(k) for k in c}
    if any(before.get(k) != c[k] for k in c):
        s.setdefault("history", []).insert(0, {"at": _now(), "config": before})
        del s["history"][10:]
    s.update(c)
    if test:
        s["test"] = test_connection(s)
    _save(pid, servers)
    return s


def restore_server(pid, sid, index):
    servers = list_servers(pid)
    s = _find(servers, sid)
    try:
        old = s.get("history", [])[int(index)]["config"]
    except (IndexError, ValueError, KeyError):
        raise ProjectError("Version introuvable dans l'historique")
    return update_server(pid, sid, old)


def remove_server(pid, sid):
    """Unlink the server from the project. Nothing is touched on the server."""
    servers = list_servers(pid)
    s = _find(servers, sid)
    _save(pid, [o for o in servers if o["id"] != sid])
    return {"removed": s["name"]}


def retest(pid, sid):
    servers = list_servers(pid)
    s = _find(servers, sid)
    s["test"] = test_connection(s)
    _save(pid, servers)
    return s


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ── SSH ─────────────────────────────────────────────────────────────────────

def ssh_base(s, tty=False):
    """ssh argv for a server — built only from the stored, validated config."""
    # known_hosts explicite : le même que sshaccess (paramiko) — ssh lit sinon celui du
    # compte (pas $HOME), ce que les tests et les deux vérifications ne partageaient pas
    kh = os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={kh}", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30", "-p", str(s.get("port") or 22)]
    if s.get("key_path"):
        args += ["-i", s["key_path"], "-o", "IdentitiesOnly=yes"]
    args += ["-t"] if tty else ["-o", "BatchMode=yes"]
    return args + ["--", f"{s['user']}@{s['host']}"] if s.get("user") else args + ["--", s["host"]]


def remote_dir_expr(path):
    """A remote path for the shell: ~ expanded by the server, the rest quoted."""
    path = (path or "").strip()
    if not path:
        return '"$HOME"'
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def test_connection(s):
    """Connect and report what DevPilot needs to know. Never changes anything on the server."""
    d = remote_dir_expr(s.get("remote_path"))
    script = ("echo __DEVPILOT__; hostname; "
              "( . /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\" ) || uname -sr; "
              "df -h / 2>/dev/null | awk 'NR==2{print $4\" libres sur \"$2}'; "
              "(command -v docker >/dev/null && docker --version) || echo -; "
              f"if [ -d {d} ]; then echo yes; else echo no; fi")
    res = {"ok": False, "at": _now()}
    try:
        r = subprocess.run(ssh_base(s) + [script], capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        res["error"] = "Le serveur ne repond pas (delai depasse) : adresse, port ou pare-feu ?"
        return res
    out = r.stdout.split("__DEVPILOT__", 1)
    if r.returncode == 0 and len(out) == 2:
        lines = [l.strip() for l in out[1].strip().splitlines()]
        lines += [""] * (5 - len(lines))
        res.update(ok=True, hostname=lines[0], os=lines[1], disk=lines[2],
                   docker=lines[3] if lines[3] != "-" else "", path_exists=lines[4] == "yes")
        return res
    res["error"] = explain_ssh_error(r.stderr, s)
    return res


def explain_ssh_error(err, s):
    e = err or ""
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in e or "Host key verification failed" in e:
        return (f"L'identite du serveur {s['host']} a change depuis la derniere connexion. Si tu as reinstalle "
                f"le serveur c'est normal : retire l'ancienne empreinte (ssh-keygen -R {s['host']}) puis reteste. "
                "Sinon, ne te connecte pas.")
    if "Permission denied" in e:
        return (f"Connexion refusee pour {s['user']}@{s['host']} : la cle n'est pas autorisee sur le serveur "
                "(ajoute la cle publique dans ~/.ssh/authorized_keys du serveur, ou dans le panneau du fournisseur).")
    if "Could not resolve hostname" in e:
        return f"Adresse introuvable : {s['host']}"
    if "Connection refused" in e:
        return f"Connexion refusee sur le port {s.get('port', 22)} (SSH arrete ou autre port ?)"
    if "timed out" in e or "No route to host" in e or "Network is unreachable" in e:
        return "Le serveur ne repond pas : adresse, port ou pare-feu ?"
    return e.strip()[-300:] or "Connexion impossible"


def terminal_argv(pid, sid):
    """argv of the DevPilot terminal on a server: ssh, straight into the project's folder."""
    s = _find(list_servers(pid), sid)
    d = remote_dir_expr(s.get("remote_path"))
    remote = f"cd {d} 2>/dev/null || echo \"(dossier {s.get('remote_path') or '~'} absent : tu es dans ~)\"; exec \"${{SHELL:-bash}}\" -l"
    return ssh_base(s, tty=True) + [remote], s


# ── Keys and ~/.ssh/config ──────────────────────────────────────────────────

def ssh_keys():
    """Private keys of ~/.ssh (with their .pub), including sub-folders like ~/.ssh/md/."""
    out = []
    if not SSH_DIR.is_dir():
        return out
    for pub in sorted(SSH_DIR.rglob("*.pub")):
        key = pub.with_suffix("")
        if not key.is_file():
            continue
        r = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, timeout=10)
        parts = r.stdout.split()
        out.append({"path": str(key), "name": str(key.relative_to(SSH_DIR)),
                    "type": parts[-1].strip("()") if parts else "", "comment": " ".join(parts[2:-1]) if len(parts) > 3 else ""})
    return out


def generate_key(pid, label):
    """A new ed25519 key dedicated to this project: ~/.ssh/devpilot/<project>_<label>."""
    project = P.get(pid)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{project['name']}_{label or 'serveur'}").strip("-")[:60]
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SSH_DIR, 0o700)
    os.chmod(KEYS_DIR, 0o700)
    path = KEYS_DIR / safe
    n = 1
    while path.exists() or path.with_suffix(".pub").exists():
        n += 1
        path = KEYS_DIR / f"{safe}-{n}"
    r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", f"devpilot {safe}", "-f", str(path)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ProjectError("Generation de la cle echouee : " + r.stderr.strip())
    return {"path": str(path), "public": path.with_suffix(".pub").read_text().strip()}


def public_key(path):
    kp = Path(os.path.expanduser(path)).resolve()
    if not kp.is_relative_to(SSH_DIR.resolve()):
        raise ProjectError("Cle hors de ~/.ssh")
    pub = kp.with_suffix(".pub") if kp.suffix != ".pub" else kp
    if not pub.is_file():
        raise ProjectError("Pas de cle publique (.pub) a cote de cette cle")
    return pub.read_text().strip()


def ssh_config_hosts():
    """Hosts already described in ~/.ssh/config (to add a server in one click)."""
    cfg = SSH_DIR / "config"
    if not cfg.is_file():
        return []
    hosts, cur = [], None
    for raw in cfg.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition(" ")
        key, val = key.lower(), val.strip()
        if key == "host":
            names = [n for n in val.split() if not any(ch in n for ch in "*?!")]
            cur = {"alias": names[0], "aliases": names, "host": "", "user": "", "port": 22, "key_path": ""} if names else None
            if cur:
                hosts.append(cur)
        elif cur is not None:
            if key == "hostname":
                cur["host"] = val
            elif key == "user":
                cur["user"] = val
            elif key == "port" and val.isdigit():
                cur["port"] = int(val)
            elif key == "identityfile":
                cur["key_path"] = str(Path(os.path.expanduser(val)))
    out = []
    for h in hosts:
        h["host"] = h["host"] or next((a for a in h["aliases"] if "." in a), h["alias"])
        if h["host"] in ("github.com", "gitlab.com", "bitbucket.org"):
            continue
        out.append(h)
    return out
