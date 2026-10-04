"""« Supprimer tout » with a real (tiny) docker compose stack using the project folder."""
import os
import shutil
import subprocess
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker needed")


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


@pytest.fixture
def U(home):
    import importlib, purge
    importlib.reload(purge)
    return purge


def dk(*a):
    return subprocess.run(["docker", *a], capture_output=True, text=True)


@pytest.fixture
def stack(P, home):
    """Project 'shop' running a compose stack: built image, named volume, network, bind mount of the folder."""
    p = P.create("shop")
    d = home / "devpilot" / "projects" / "shop"
    proj = "dptest" + uuid.uuid4().hex[:6]
    (d / "Dockerfile").write_text("FROM alpine:latest\nCMD [\"sleep\", \"3600\"]\n")
    (d / "docker-compose.yml").write_text(
        "services:\n  app:\n    build: .\n    ports: ['127.0.0.1:0:80']\n"
        "    volumes: [data:/data, ./:/src]\nvolumes:\n  data: {}\n")
    r = subprocess.run(["docker", "compose", "-p", proj, "up", "-d", "--build", "-q" if False else "--quiet-pull"],
                       cwd=d, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    yield p["id"], d, proj
    subprocess.run(["docker", "compose", "-p", proj, "down", "-v", "--rmi", "local"], cwd=d if d.exists() else home,
                   capture_output=True)
    dk("rmi", f"{proj}-app")


def test_plan_finds_everything(P, U, home, stack):
    pid, d, proj = stack
    (home / "shoplink").symlink_to(d)
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    (home / ".local" / "bin" / "shop-start").write_text(f"#!/bin/sh\ncd {d} && docker compose up\n")
    (home / ".config").mkdir(exist_ok=True)
    (home / ".config" / "shop.env").write_text(f"SHOP_DIR={d}\nTOKEN=secret\n")
    (home / ".bash_history").write_text(f"cd {d}\n")
    proc = subprocess.Popen(["sleep", "60"], cwd=d)
    try:
        time.sleep(0.3)
        pl = U.plan(pid)
        g = pl["docker"][0]
        assert g["compose"] == proj and len(g["containers"]) == 1 and g["containers"][0]["ports"]
        assert g["volumes"] == [f"{proj}_data"] and f"{proj}_default" in g["networks"]
        assert [i["ref"] for i in g["images"]] == [f"{proj}-app:latest"]                  # not alpine
        assert any(pr["pid"] == proc.pid for pr in pl["processes"])
        out = {o["path"]: o for o in pl["outside"]}
        assert out[str(home / "shoplink")]["default"] and out[str(home / ".local" / "bin" / "shop-start")]["default"]
        assert out[str(home / ".config" / "shop.env")]["default"] is False                  # config: never by default
        assert str(home / ".bash_history") not in out
        assert not pl["folder"]["blockers"]
    finally:
        proc.kill()


def test_execute_removes_everything(P, U, home, stack):
    pid, d, proj = stack
    (home / "shoplink").symlink_to(d)
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    launcher = home / ".local" / "bin" / "shop-start"
    launcher.write_text(f"#!/bin/sh\ncd {d}\n")
    (home / ".config").mkdir(exist_ok=True)
    cfg = home / ".config" / "shop.env"
    cfg.write_text(f"SHOP_DIR={d}\n")
    proc = subprocess.Popen(["sleep", "60"], cwd=d)
    time.sleep(0.3)
    err(P, U.execute, pid, "Shop")                                        # name must match exactly
    r = U.execute(pid, "shop")
    proc.wait(timeout=10)
    assert not d.exists() and (home / ".local" / "share" / "Trash" / "files" / "shop").is_dir()
    assert dk("ps", "-aq", "--filter", f"label=com.docker.compose.project={proj}").stdout.strip() == ""
    assert dk("volume", "inspect", f"{proj}_data").returncode != 0
    assert dk("network", "inspect", f"{proj}_default").returncode != 0
    assert dk("image", "inspect", f"{proj}-app").returncode != 0
    assert dk("image", "inspect", "alpine:latest").returncode == 0                   # shared base image kept
    assert not (home / "shoplink").exists() and not launcher.exists() and cfg.exists()
    assert P.find_by_name("shop") is None
    assert any("ports" in l for l in r["log"])


def test_execute_can_keep_volumes_and_images(P, U, home, stack):
    pid, d, proj = stack
    U.execute(pid, "shop", {"volumes": False, "images": False})
    assert dk("volume", "inspect", f"{proj}_data").returncode == 0
    assert dk("image", "inspect", f"{proj}-app").returncode == 0
    dk("volume", "rm", f"{proj}_data")


def test_other_stacks_are_not_touched(P, U, home, stack, tmp_path):
    pid, d, proj = stack
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "docker-compose.yml").write_text("services:\n  x:\n    image: alpine:latest\n    command: sleep 3600\n")
    oproj = proj + "other"
    subprocess.run(["docker", "compose", "-p", oproj, "up", "-d"], cwd=other, capture_output=True, check=True)
    try:
        assert [g["compose"] for g in U.plan(pid)["docker"]] == [proj]
        U.execute(pid, "shop")
        assert dk("ps", "-q", "--filter", f"label=com.docker.compose.project={oproj}").stdout.strip()
    finally:
        subprocess.run(["docker", "compose", "-p", oproj, "down"], cwd=other, capture_output=True)


def test_nested_project_blocks(P, U, home):
    outer = P.create("outer")
    inner = home / "devpilot" / "projects" / "outer" / "inner"
    inner.mkdir()
    import db
    db.create_project(name="inner", path=str(inner))
    e = err(P, U.execute, outer["id"], "outer")
    assert "autres projets" in e.message and (home / "devpilot" / "projects" / "outer").exists()


def test_outside_scan_matches_the_exact_path(P, U, home):
    P.create("shop")
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    (home / ".local" / "bin" / "other").write_text(f"cd {home}/devpilot/projects/shop2\n")
    (home / ".local" / "bin" / "mine").write_text(f"cd {home}/devpilot/projects/shop/src\n")
    paths = [o["path"] for o in U.outside_plan((home / "devpilot" / "projects" / "shop").resolve())]
    assert str(home / ".local" / "bin" / "mine") in paths and str(home / ".local" / "bin" / "other") not in paths
