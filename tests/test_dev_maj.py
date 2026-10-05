"""Mettre à jour les dépôts d'un projet : la branche de chacun est montrée et CHOISIE (★ main si rien
n'est en cours, sinon rester) — plus de « git pull » à l'aveugle sur une vieille branche."""
import importlib

import pytest

from conftest import sh


@pytest.fixture
def DV(home):
    import dev
    importlib.reload(dev)
    return dev


def commit(repo, f, txt, msg):
    (repo / f).write_text(txt); sh("git", "add", "-A", cwd=repo); sh("git", "commit", "-qm", msg, cwd=repo)


@pytest.fixture
def proj(P, DV, home, remote, tmp_path):
    p = P.create("plateforme")
    root = home / "devpilot" / "projects" / "plateforme"
    for n in ("finie", "travail", "sale", "a-jour"):
        sh("git", "clone", "-q", remote, str(root / n))
    autre = tmp_path / "autre"; sh("git", "clone", "-q", remote, str(autre))
    # finie : poussée puis fusionnée dans main ; travail : un commit jamais poussé ; sale : fichier modifié
    sh("git", "switch", "-q", "-c", "feat/finie", cwd=root / "finie"); commit(root / "finie", "x.txt", "x", "x")
    sh("git", "push", "-q", "origin", "feat/finie", cwd=root / "finie")
    sh("git", "fetch", "-q", cwd=autre); sh("git", "merge", "-q", "--no-ff", "-m", "fusion", "origin/feat/finie", cwd=autre)
    commit(autre, "nouveau.txt", "n", "nouveauté"); sh("git", "push", "-q", "origin", "main", cwd=autre)
    sh("git", "switch", "-q", "-c", "feat/gh", cwd=autre); commit(autre, "gh.txt", "g", "gh"); sh("git", "push", "-q", "origin", "feat/gh", cwd=autre)
    sh("git", "switch", "-q", "-c", "feat/travail", cwd=root / "travail"); commit(root / "travail", "t.txt", "t", "t")
    sh("git", "switch", "-q", "-c", "feat/sale", cwd=root / "sale"); (root / "sale" / "package.json").write_text('{"m":1}')
    return p["id"], root


def test_plan_recommande(DV, proj):
    pid, root = proj
    r = {x["dir"]: x for x in DV.plan_maj(pid)["repos"]}
    assert r["finie"]["cible"] == "main" and "finie" in r["finie"]["pourquoi"] and r["finie"]["nouveautes"] >= 1
    assert r["travail"]["cible"] == "feat/travail" and "jamais poussé" in r["travail"]["pourquoi"]
    assert r["sale"]["cible"] == "feat/sale" and "non commité" in r["sale"]["pourquoi"]
    assert r["a-jour"]["cible"] == "main" and r["a-jour"]["nouveautes"] >= 1
    assert any(c["name"] == "feat/gh" and not c["local"] and c["github"] for c in r["a-jour"]["choix"])


def test_appliquer(DV, proj):
    pid, root = proj
    out = {x["dir"]: x for x in DV.appliquer_maj(pid, {"finie": "main", "travail": "feat/travail", "sale": "main",
                                                        "a-jour": "feat/gh"})["resultats"]}
    assert out["finie"]["ok"] and sh("git", "branch", "--show-current", cwd=root / "finie") == "main"
    assert (root / "finie" / "nouveau.txt").exists()                                        # main avancé
    assert out["travail"]["ok"] and (root / "travail" / "t.txt").exists()                   # resté, rien perdu
    assert not out["sale"]["ok"] and "non commitées" in out["sale"]["message"]
    assert sh("git", "branch", "--show-current", cwd=root / "sale") == "feat/sale"          # rien écrasé
    assert out["a-jour"]["ok"] and (root / "a-jour" / "gh.txt").exists()                    # branche GitHub suivie
