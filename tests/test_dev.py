"""Développer : les dépôts d'un projet (même imbriqués), les branches, les outils."""
import importlib
import os
from pathlib import Path

import pytest

from conftest import sh


@pytest.fixture
def DV(home):
    import dev
    importlib.reload(dev)
    return dev


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value.message


@pytest.fixture
def multi(P, DV, home, tmp_path, remote):
    """Un projet « marocdefender-like » : un dépôt racine et un dépôt imbriqué à 2 niveaux."""
    p = P.create("plateforme")
    root = home / "devpilot" / "projects" / "plateforme"
    sh("git", "clone", "-q", remote, str(root / "md_infra"))
    sh("git", "clone", "-q", remote, str(root / "md_infra" / "services" / "md-backend"))
    return p["id"], root


def repos_by_dir(DV, pid):
    return {r["dir"]: r for r in DV.repos(pid)["repos"]}


def test_les_depots_imbriques_sont_trouves(DV, multi):
    pid, root = multi
    r = repos_by_dir(DV, pid)
    assert set(r) == {"md_infra", "md_infra/services/md-backend"}
    b = r["md_infra/services/md-backend"]
    assert b["branch"] == "main" and b["on_base"] and b["base"] == "main" and b["modified"] == 0


def test_nouvelle_branche_toujours_depuis_main_a_jour(DV, multi, tmp_path):
    pid, root = multi
    repo = root / "md_infra"
    sh("git", "switch", "-q", "-c", "vieille", cwd=repo)
    (repo / "x.txt").write_text("vieux travail"); sh("git", "add", "-A", cwd=repo); sh("git", "commit", "-qm", "vieux", cwd=repo)
    r = DV.new_branch(pid, "md_infra", "fix/message")
    assert r["from"] == "origin/main"
    assert not (repo / "x.txt").exists()                           # partie de main, pas de « vieille »
    assert repos_by_dir(DV, pid)["md_infra"]["branch"] == "fix/message"


def test_noms_de_branche_et_doublons(P, DV, multi):
    pid, _ = multi
    assert "invalide" in err(P, DV.new_branch, pid, "md_infra", "mauvais nom..")
    assert "invalide" in err(P, DV.new_branch, pid, "md_infra", "-x")
    assert "existe déjà" in err(P, DV.new_branch, pid, "md_infra", "dev")     # sur GitHub


def test_jamais_de_changement_de_branche_avec_du_travail_non_commite(P, DV, multi):
    pid, root = multi
    repo = root / "md_infra"
    (repo / "package.json").write_text('{"modifié": true}')
    m = err(P, DV.switch, pid, "md_infra", "dev")
    assert "non commité" in m and "package.json" in m and repos_by_dir(DV, pid)["md_infra"]["branch"] == "main"
    r = DV.switch(pid, "md_infra", "dev", stash=True)              # avec « mettre de côté »
    assert r["branch"] == "dev" and "stash" in r["note"]
    assert "DevPilot : avant" in sh("git", "stash", "list", cwd=repo)


def test_branche_seulement_sur_github(DV, multi):
    pid, root = multi
    b = {x["name"]: x for x in DV.branches(pid, "md_infra")["branches"]}
    assert b["dev"]["local"] is False and b["dev"]["remote"] and b["main"]["current"]
    DV.switch(pid, "md_infra", "dev")
    assert sh("git", "rev-parse", "--abbrev-ref", "dev@{u}", cwd=root / "md_infra") == "origin/dev"


def test_mettre_main_a_jour_sans_quitter_sa_branche(DV, multi, tmp_path, remote):
    pid, root = multi
    repo = root / "md_infra"
    autre = tmp_path / "autre"
    sh("git", "clone", "-q", remote, str(autre))
    (autre / "nouveau.txt").write_text("x"); sh("git", "add", "-A", cwd=autre); sh("git", "commit", "-qm", "sur github", cwd=autre)
    sh("git", "push", "-q", "origin", "main", cwd=autre)
    DV.new_branch(pid, "md_infra", "feat/a")
    sh("git", "fetch", "-q", cwd=repo)
    assert repos_by_dir(DV, pid)["md_infra"]["base_behind"] == 1
    DV.update_main(pid, "md_infra")
    r = repos_by_dir(DV, pid)["md_infra"]
    assert r["base_behind"] == 0 and r["branch"] == "feat/a"        # main avancée, on reste sur sa branche


def test_depot_hors_projet_refuse(P, DV, multi):
    pid, _ = multi
    assert "hors du projet" in err(P, DV.switch, pid, "../..", "main")
    assert "pas un dépôt" in err(P, DV.switch, pid, "md_infra/services", "main")


def test_outils_et_ouverture(P, DV, multi, monkeypatch, tmp_path):
    pid, root = multi
    lances = []
    monkeypatch.setattr(DV, "_detach", lambda argv, cwd: lances.append((argv, str(cwd))))
    bindir = tmp_path / "bin"; bindir.mkdir()
    for t in ("code", "claude", "gnome-terminal"):
        (bindir / t).write_text("#!/bin/sh\n"); (bindir / t).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    assert DV.tools()["editor"]["ok"] and DV.tools()["claude"]["ok"]
    DV.open_editor(pid, "md_infra/services/md-backend")
    assert lances[-1] == (["code", str(root / "md_infra/services/md-backend")], str(root / "md_infra/services/md-backend"))
    DV.open_external(pid, "md_infra", claude=True)
    argv = lances[-1][0]
    assert argv[0] == "gnome-terminal" and str(root / "md_infra") in argv and "claude; exec bash" in argv[-1]
    import db
    db.set_setting("editor_cmd", "editeur-absent")
    assert "introuvable" in err(P, DV.open_editor, pid, "md_infra")
