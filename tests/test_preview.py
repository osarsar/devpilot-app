"""Aperçu local : quel port montre le code de quel dépôt, et comment le lancer sinon."""
import importlib
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import sh


@pytest.fixture
def PV(home, monkeypatch):
    import dev, preview
    importlib.reload(dev); importlib.reload(preview)
    return preview


@pytest.fixture
def proj(P, home, remote):
    p = P.create("plateforme")
    root = home / "devpilot" / "projects" / "plateforme"
    sh("git", "clone", "-q", remote, str(root / "infra"))
    sh("git", "clone", "-q", remote, str(root / "infra" / "services" / "backend"))
    sh("git", "clone", "-q", remote, str(root / "landing"))
    (root / "infra" / "dev.sh").write_text('#!/bin/bash\ncase "$1" in\n  up) docker compose up -d;;\nesac\n')
    (root / "infra" / "docker-compose.dev.yml").write_text("services:\n  backend:\n    volumes:\n      - ./services/backend:/app\n")
    (root / "landing" / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    return p["id"], root


def test_rien_ne_tourne_commande_de_lancement(PV, proj, monkeypatch):
    pid, root = proj
    monkeypatch.setattr(PV, "containers", lambda: [])
    v = PV.previews(pid)
    assert v["infra"]["start"] == {"cmd": "./dev.sh up", "cwd": "infra", "why": "son script dev.sh"}
    b = v["infra/services/backend"]["start"]                       # monté par la pile docker d'infra
    assert b["cmd"] == "./dev.sh up" and b["cwd"] == "infra" and "docker" in b["why"]
    assert v["landing"]["start"]["cmd"] == "npm run dev"


def test_conteneur_rattache_au_depot_dont_il_monte_le_code(PV, proj, monkeypatch):
    pid, root = proj
    be = (root / "infra" / "services" / "backend").resolve()
    monkeypatch.setattr(PV, "containers", lambda: [
        {"name": "x-backend-1", "service": "backend", "ports": [18002], "mounts": [be]},
        # monte d'abord le code (RW), puis les données d'un autre dépôt en lecture seule
        {"name": "x-collector-1", "service": "collector", "ports": [18003], "mounts": [(root / "landing").resolve(), be / "data"]}])
    v = PV.previews(pid)
    assert [x["port"] for x in v["infra/services/backend"]["running"]] == [18002]     # le plus profond, pas « infra »
    assert v["infra/services/backend"]["start"] is None
    assert [x["port"] for x in v["landing"]["running"]] == [18003]
    assert v["infra"]["running"] == []


def test_processus_lance_depuis_le_dossier(PV, proj, monkeypatch):
    pid, root = proj
    monkeypatch.setattr(PV, "containers", lambda: [])
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    p = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=root / "landing",
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if any(x["port"] == port for x in PV.previews(pid)["landing"]["running"]):
                break
            time.sleep(0.1)
        v = PV.previews(pid)["landing"]
        assert v["running"][0]["url"] == f"http://localhost:{port}" and v["running"][0]["via"] == "process"
        assert v["start"] is None
    finally:
        p.kill()


def test_lancer_tape_la_commande_detectee(P, PV, proj, monkeypatch):
    pid, root = proj
    import sizes, project_routes, cloud_routes, dashboard
    for m in (sizes, project_routes, cloud_routes, dashboard):
        importlib.reload(m)
    dashboard.app.config.update(TESTING=True, SERVER_NAME="127.0.0.1:5555")
    monkeypatch.setattr(PV, "containers", lambda: [])
    c = dashboard.app.test_client()
    r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "infra/services/backend", "start": True}).get_json()
    try:
        assert r["success"] and r["run"] == "./dev.sh up" and Path(r["cwd"]) == root / "infra"
        assert r["label"] == "▶ backend"
    finally:
        dashboard._term_sessions[r["sid"]].kill()
    # rien à lancer : refusé, et jamais de commande venue de la requête
    (root / "landing" / "package.json").unlink()
    r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "landing", "start": True, "run": "rm -rf ~"}).get_json()
    assert not r["success"]


def _port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_page_api_ou_en_panne(PV, tmp_path):
    import json, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Api(BaseHTTPRequestHandler):
        def do_GET(self):
            code, body, ct = (200, b"<html>docs</html>", "text/html") if self.path == "/docs" else (404, b'{"detail":"Not Found"}', "application/json")
            self.send_response(code); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(b"<html></html>")
        def log_message(self, *a): pass

    serveurs = [HTTPServer(("127.0.0.1", 0), h) for h in (Page, Api)]
    for srv in serveurs:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    mort = socket.socket(); mort.bind(("127.0.0.1", 0)); mort.listen(5)        # port ouvert, personne ne répond
    try:
        page, api = (x.server_address[1] for x in serveurs)
        assert PV.probe(page) == {"kind": "page"}
        assert PV.probe(api) == {"kind": "api", "docs": f"http://localhost:{api}/docs"}
        assert PV.probe(mort.getsockname()[1]) == {"kind": "down"}
        assert PV.probe(_port()) == {"kind": "down"}                             # fermé
    finally:
        for srv in serveurs:
            srv.shutdown()
        mort.close()


def test_journaux_du_service_en_panne(P, PV, proj, monkeypatch):
    pid, root = proj
    import sizes, project_routes, cloud_routes, dashboard
    for m in (sizes, project_routes, cloud_routes, dashboard):
        importlib.reload(m)
    dashboard.app.config.update(TESTING=True, SERVER_NAME="127.0.0.1:5555")
    be = (root / "infra" / "services" / "backend").resolve()
    monkeypatch.setattr(PV, "containers", lambda: [{"name": "x-backend-1", "service": "backend", "ports": [_port()], "mounts": [be]}])
    v = PV.previews(pid)["infra/services/backend"]["running"][0]
    assert v["kind"] == "down" and v["logs"] == "docker logs --tail 50 x-backend-1"
    c = dashboard.app.test_client()
    r = c.post("/api/terminal/sessions", json={"pid": pid, "repo": "infra/services/backend", "logs": True}).get_json()
    try:
        assert r["success"] and r["run"] == "docker logs --tail 50 x-backend-1" and r["label"] == "journaux backend"
    finally:
        dashboard._term_sessions[r["sid"]].kill()
    assert not c.post("/api/terminal/sessions", json={"pid": pid, "repo": "landing", "logs": True}).get_json()["success"]
