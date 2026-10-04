"""Carte des branches : l'état de la branche de travail de chaque dépôt sur le chemin vers main
(sur GitHub ? à pousser ? à récupérer ? à fusionner ? déjà fusionnée, même en squash ?)."""
import importlib

import pytest

from conftest import sh


@pytest.fixture
def DV(home):
    import dev
    importlib.reload(dev)
    return dev


@pytest.fixture
def proj(P, DV, home, remote, tmp_path):
    p = P.create("plateforme")
    root = home / "devpilot" / "projects" / "plateforme"
    sh("git", "clone", "-q", remote, str(root / "landing"))
    # un 2e PC / GitHub : un clone à part pour pousser « d'ailleurs »
    autre = tmp_path / "autre"
    sh("git", "clone", "-q", remote, str(autre))
    return p["id"], root / "landing", autre


def st(DV, pid):
    return {r["dir"]: r for r in DV.repos(pid)["repos"]}["landing"]


def commit(repo, f, txt, msg):
    (repo / f).write_text(txt); sh("git", "add", "-A", cwd=repo); sh("git", "commit", "-qm", msg, cwd=repo)


def test_sur_main_pas_de_branche(DV, proj):
    pid, repo, _ = proj
    s = st(DV, pid)
    assert s["on_base"] and s["merge"] == "none" and not s["remote_branch"]


def test_cycle_de_vie_d_une_branche(DV, proj):
    pid, repo, autre = proj
    DV.new_branch(pid, "landing", "feat/accueil")
    s = st(DV, pid)
    assert (s["merge"], s["remote_branch"], s["to_push"]) == ("empty", False, 0)       # rien encore
    commit(repo, "a.txt", "1", "accueil")
    s = st(DV, pid)
    assert (s["merge"], s["remote_branch"], s["to_push"]) == ("todo", False, 1)        # jamais poussée
    sh("git", "push", "-q", "origin", "feat/accueil", cwd=repo)                         # comme md_console : sans -u
    s = st(DV, pid)
    assert (s["remote_branch"], s["pushed"], s["to_push"], s["to_pull"]) == (True, True, 0, 0)
    commit(repo, "b.txt", "2", "suite")
    assert st(DV, pid)["to_push"] == 1
    sh("git", "push", "-q", "origin", "feat/accueil", cwd=repo)
    # un autre PC pousse sur la même branche
    sh("git", "fetch", "-q", "origin", cwd=autre); sh("git", "switch", "-q", "feat/accueil", cwd=autre)
    commit(autre, "c.txt", "3", "depuis l'autre PC"); sh("git", "push", "-q", "origin", "feat/accueil", cwd=autre)
    s = DV.repos(pid, fetch=True)["repos"][0]
    assert s["to_pull"] == 1 and s["merge"] == "todo"
    # fusion en SQUASH sur GitHub (ce que fait le bouton « Squash and merge »)
    sh("git", "switch", "-q", "main", cwd=autre); sh("git", "pull", "-q", cwd=autre)
    sh("git", "merge", "-q", "--squash", "origin/feat/accueil", cwd=autre); sh("git", "commit", "-qm", "Accueil (#1)", cwd=autre)
    sh("git", "push", "-q", "origin", "main", cwd=autre)
    sh("git", "pull", "-q", "--ff-only", "origin", "feat/accueil", cwd=repo)
    s = DV.repos(pid, fetch=True)["repos"][0]
    assert s["for_base"] == 3 and s["merge"] == "done"                                   # commits « en avance » mais déjà dans main


def test_fusion_classique_aussi_reconnue(DV, proj):
    pid, repo, autre = proj
    DV.new_branch(pid, "landing", "fix/x")
    commit(repo, "x.txt", "x", "x"); sh("git", "push", "-q", "origin", "fix/x", cwd=repo)
    sh("git", "fetch", "-q", cwd=autre); sh("git", "merge", "-q", "--no-ff", "-m", "merge", "origin/fix/x", cwd=autre)
    sh("git", "push", "-q", "origin", "main", cwd=autre)
    s = DV.repos(pid, fetch=True)["repos"][0]
    assert s["merge"] == "empty"            # main contient déjà tous ses commits


def test_les_autres_branches_sont_listees(DV, proj):
    pid, repo, _ = proj
    sh("git", "branch", "vieille", cwd=repo)
    DV.new_branch(pid, "landing", "feat/a")
    assert st(DV, pid)["others"] == ["vieille"]


def test_creer_en_emportant_les_modifs_faites_sur_main(P, DV, proj):
    pid, repo, _ = proj
    (repo / "package.json").write_text('{"modifié": true}')
    with pytest.raises(P.ProjectError):
        DV.new_branch(pid, "landing", "feat/sans")                                     # par défaut : refusé
    r = DV.new_branch(pid, "landing", "feat/avec", carry=True)
    assert "suivi" in r["note"] and "modifié" in (repo / "package.json").read_text()
    assert sh("git", "branch", "--show-current", cwd=repo) == "feat/avec"
    assert st(DV, pid)["modified"] == 1


def test_gh_repo(DV):
    assert DV._gh_repo("git@github.com:marocdefender/md_factcheck.git") == "marocdefender/md_factcheck"
    assert DV._gh_repo("https://github.com/osarsar/devpilot-app") == "osarsar/devpilot-app"
    assert DV._gh_repo("file:///tmp/x.git") == ""


def test_route_prs_sans_gh(P, DV, proj, monkeypatch):
    pid, _, _ = proj
    monkeypatch.setattr(DV.shutil, "which", lambda x: None)
    assert DV.prs(pid) == {}
