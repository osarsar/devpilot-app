"""DevPilot — « where do I see this repo running? » (local preview).

For each repo of a project:
    running  the local addresses that serve ITS code right now:
             - a docker container that mounts the repo's code (dev stack, hot reload)
             - a process started from the repo's folder (vite, uvicorn, ./dev.sh…)
    start    when nothing runs: the command that starts it, and where to type it
             (its own dev.sh / npm run dev, or the docker stack of the repo that mounts it)

Detected, never configured: what you see is what really answers on this PC.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import dev
import ports

_cache = {"t": 0.0, "v": []}


def _real(p):
    try:
        return Path(p).resolve()
    except (OSError, RuntimeError):
        return None


def containers():
    """Running containers with published ports: [{name, service, ports:[host], mounts:[real paths]}] (cached 5 s)."""
    if time.time() - _cache["t"] < 5:
        return _cache["v"]
    out = []
    try:
        ids = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=10).stdout.split()
        if ids:
            r = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True, timeout=20)
            for c in json.loads(r.stdout or "[]"):
                host = sorted({int(b["HostPort"]) for bs in ((c.get("NetworkSettings") or {}).get("Ports") or {}).values()
                               for b in (bs or []) if b.get("HostPort")})
                # the code first: a read-only mount is data read by the service, not its code
                binds = sorted((x for x in c.get("Mounts") or [] if x.get("Type") == "bind"), key=lambda x: not x.get("RW", True))
                mounts = [m for m in (_real(x.get("Source")) for x in binds) if m]
                labels = (c.get("Config") or {}).get("Labels") or {}
                out.append({"name": c.get("Name", "").lstrip("/"), "service": labels.get("com.docker.compose.service", ""),
                            "ports": host, "mounts": mounts})
    except (OSError, ValueError, subprocess.TimeoutExpired):
        out = []
    _cache.update(t=time.time(), v=out)
    return out


def _get(url, timeout=1.5):
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else ""
    except Exception:
        return 0, ""


def probe(port):
    """What answers on this port: page (a web page to open) | api (JSON: used BY the site, with
    its /docs when it has one) | down (the port is open but nothing answers: starting, or crashed)."""
    code, ctype = _get(f"http://127.0.0.1:{port}/")
    if code and code < 400 and "text/html" in ctype:
        return {"kind": "page"}
    if not code:
        return {"kind": "down"}
    dcode, dtype = _get(f"http://127.0.0.1:{port}/docs")
    return {"kind": "api", "docs": f"http://localhost:{port}/docs" if dcode == 200 and "text/html" in dtype else None}


def _owner(path, repo_paths):
    """The deepest repo that contains this path (md-backend, not md_infra that holds it)."""
    best = None
    for rp in repo_paths:
        if path == rp or path.is_relative_to(rp):
            if best is None or len(rp.parts) > len(best.parts):
                best = rp
    return best


# Gestionnaire de paquets choisi par le fichier de verrouillage du dépôt :
# (fichier, installation fidèle au verrou, lancement du script dev)
_GESTIONNAIRES = (
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile", "pnpm run dev"),
    ("yarn.lock", "yarn install --frozen-lockfile", "yarn dev"),
    ("bun.lockb", "bun install --frozen-lockfile", "bun run dev"),
    ("package-lock.json", "npm ci", "npm run dev"),
)


def _cmd_node(repo):
    """« npm run dev » — précédé de l'installation si node_modules manque : un
    dépôt fraîchement cloné échouait sur « next: not found » (ou vite, etc.)."""
    for verrou, installer, lancer in _GESTIONNAIRES:
        if (repo / verrou).is_file():
            break
    else:
        installer, lancer = "npm install", "npm run dev"
    return lancer if (repo / "node_modules").is_dir() else f"{installer} && {lancer}"


def _start_cmd(repo, root, repo_paths):
    """How to start this repo when nothing runs: {cmd, cwd (relative to the project), why}."""
    rel = lambda p: str(p.relative_to(root)) if p != root else "."
    dsh = repo / "dev.sh"
    if dsh.is_file():
        txt = dsh.read_text(errors="ignore")[:20000]
        up = re.search(r"dev\.sh up\b|^\s*up\)", txt, re.M)
        return {"cmd": "./dev.sh up" if up else "./dev.sh", "cwd": rel(repo), "why": "son script dev.sh"}
    # monté par la pile docker d'un autre dépôt (md_infra/docker-compose.dev.yml → ./marocdefender/md-backend)
    for rp in sorted(repo_paths, key=lambda p: len(p.parts)):
        if rp == repo or not repo.is_relative_to(rp):
            continue
        sub = str(repo.relative_to(rp))
        for f in sorted(rp.glob("docker-compose*.yml")) + sorted(rp.glob("compose*.yml")):
            try:
                if re.search(r"[\s\"']\./" + re.escape(sub) + r"(/|:)", f.read_text(errors="ignore")):
                    st = _start_cmd(rp, root, [])
                    if st:
                        return {**st, "why": f"la pile docker de {rp.name} ({f.name})"}
            except OSError:
                continue
    pkg = repo / "package.json"
    if pkg.is_file():
        try:
            if "dev" in (json.loads(pkg.read_text()).get("scripts") or {}):
                return {"cmd": _cmd_node(repo), "cwd": rel(repo),
                        "why": "package.json" if (repo / "node_modules").is_dir()
                        else "package.json — dépendances installées d'abord (node_modules absent)"}
        except ValueError:
            pass
    for name in ("run.sh", "start.sh"):
        if (repo / name).is_file():
            return {"cmd": f"./{name}", "cwd": rel(repo), "why": name}
    return None


def previews(pid):
    """{dir: {running: [{port, url, via, name}], start}} for every repo of the project."""
    _, root = dev.project_root(pid)
    found = dev._find(root)
    repo_paths = [p.resolve() for p in found]
    res = {}
    for p in repo_paths:
        res[p] = {"running": [], "start": None}
    seen = set()
    for c in containers():
        for m in c["mounts"]:
            o = _owner(m, repo_paths)
            if o is None:
                continue
            for port in c["ports"]:
                if (o, port) not in seen:
                    seen.add((o, port))
                    res[o]["running"].append({"port": port, "url": f"http://localhost:{port}", "via": "docker",
                                              "name": c["service"] or c["name"],
                                              "logs": f"docker logs --tail 50 {c['name']}"})
            break
    own = os.getpid()
    for l in ports.listening():
        if l["pid"] == own or l["addr"] not in ("127.0.0.1", "0.0.0.0", "::", "::1", "localhost"):
            continue
        cwd = _real(l["cwd"]) if l["cwd"] else None
        o = _owner(cwd, repo_paths) if cwd else None
        if o is None or (o, l["port"]) in seen:
            continue
        if any(l["port"] in c["ports"] for c in containers()):          # docker-proxy : déjà compté
            continue
        seen.add((o, l["port"]))
        res[o]["running"].append({"port": l["port"], "url": f"http://localhost:{l['port']}", "via": "process",
                                  "name": l["name"]})
    from concurrent.futures import ThreadPoolExecutor
    tous = [x for r in res.values() for x in r["running"]]
    with ThreadPoolExecutor(max_workers=12) as ex:
        for x, pr in zip(tous, ex.map(lambda x: probe(x["port"]), tous)):
            x.update(pr)
    rang = {"page": 0, "down": 1, "api": 2}
    out = {}
    for p in repo_paths:
        r = res[p]
        r["running"].sort(key=lambda x: (rang[x["kind"]], x["port"]))      # la page à ouvrir d'abord
        if not r["running"]:
            r["start"] = _start_cmd(p, root, repo_paths)
        out[str(p.relative_to(root)) if p != root else "."] = r
    return out
