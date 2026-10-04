"""devpilot publish / devpilot update : travailler depuis plusieurs PC sur la même version.
Deux « PC » (clones), un « GitHub » (dépôt nu) et un faux gh qui fait vraiment la PR et la
fusion squash — comme sur GitHub."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
GH_URL = "https://github.com/test/app.git"

FAUX_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, tempfile
nu, etat = os.environ["FAUX_GITHUB"], os.environ["FAUX_GH_ETAT"]
a = sys.argv[1:]
def prs():
    return json.load(open(etat)) if os.path.exists(etat) else {}
def git(*x, cwd=None):
    subprocess.run(["git", *x], cwd=cwd, check=True, capture_output=True)
if a[:2] == ["auth", "status"]:
    sys.exit(0)
if a[:2] == ["pr", "list"]:
    br = a[a.index("--head") + 1]; p = prs().get(br)
    if p and not p.get("merged"): print(p["url"])
    sys.exit(0)
if a[:2] == ["pr", "create"]:
    br, titre = a[a.index("--head") + 1], a[a.index("--title") + 1]
    d = prs(); n = len(d) + 1
    d[br] = {"url": f"https://github.com/test/app/pull/{n}", "titre": titre}
    json.dump(d, open(etat, "w")); print(d[br]["url"]); sys.exit(0)
if a[:2] == ["pr", "merge"]:
    d = prs(); br = next(b for b, p in d.items() if p["url"] == a[2])
    with tempfile.TemporaryDirectory() as t:
        git("clone", "-q", nu, t)
        git("merge", "-q", "--squash", f"origin/{br}", cwd=t)
        git("commit", "-qm", f"{d[br]['titre']} (#{a[2].rsplit('/', 1)[1]})", cwd=t)
        git("push", "-q", "origin", "main", cwd=t)
        git("push", "-q", "origin", "--delete", br, cwd=t)
    d[br]["merged"] = True; json.dump(d, open(etat, "w")); sys.exit(0)
sys.exit("faux gh : commande inconnue " + " ".join(a))
'''


def git(cwd, *a, check=True):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert not check or r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def monde(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "src"; src.mkdir()
    for f in ("publish.sh", "update.sh", "pc.sh"):
        shutil.copy(APP / f, src / f)
    (src / "requirements.txt").write_text("")
    (src / "page.py").write_text("TITRE = 'v1'\n")
    (src / "dashboard.py").write_text("")
    (src / "launch.sh").write_text("#!/bin/bash\nexit 0\n"); (src / "launch.sh").chmod(0o755)
    (src / ".gitignore").write_text(".venv\n")
    git(src, "init", "-q", "-b", "main"); git(src, "add", "-A"); git(src, "commit", "-qm", "init")
    nu = tmp_path / "github.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(nu))
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "gh").write_text(FAUX_GH); (bin_ / "gh").chmod(0o755)
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text(f'[url "{nu}"]\n\tinsteadOf = {GH_URL}\n[user]\n\tname = t\n\temail = t@t\n')
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", FAUX_GITHUB=str(nu), FAUX_GH_ETAT=str(tmp_path / "prs.json"),
               DEVPILOT_PUSH_URL=str(nu), GIT_CONFIG_GLOBAL=str(gcfg), DEVPILOT_INTERDITS=str(tmp_path / "interdits.txt"))

    def pc(nom):
        d = tmp_path / nom
        subprocess.run(["git", "clone", "-q", GH_URL, str(d)], env=env, check=True, capture_output=True)
        (d / ".venv" / "bin").mkdir(parents=True)
        (d / ".venv" / "bin" / "pip").symlink_to(Path(sys.executable).parent / "pip")
        (d / ".venv" / "bin" / "python").symlink_to(sys.executable)
        home = tmp_path / f"home-{nom}"; home.mkdir()
        return {"dir": d, "env": dict(env, HOME=str(home))}

    def run(p, script, *args):
        r = subprocess.run(["bash", str(p["dir"] / script), *args], cwd=p["dir"], env=p["env"],
                           capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    return {"nu": nu, "pc": pc, "run": run, "tmp": tmp_path}


def sur_github(m, f="page.py"):
    return git(m["nu"], "show", f"main:{f}")


def test_publier_puis_mettre_a_jour_un_autre_pc(monde):
    a, b = monde["pc"]("pc-a"), monde["pc"]("pc-b")
    (a["dir"] / "page.py").write_text("TITRE = 'v2'\n")
    (a["dir"] / "nouveau.py").write_text("X = 1\n")                                  # fichier nouveau aussi
    rc, out = monde["run"](a, "publish.sh", "Titre v2", "--sans-tests", "--sans-redemarrer")
    assert rc == 0, out
    assert "v2" in sur_github(monde) and sur_github(monde, "nouveau.py") == "X = 1"
    assert git(monde["nu"], "log", "-1", "--format=%s", "main") == "Titre v2 (#1)"     # un commit propre, via PR
    assert git(monde["nu"], "branch", "--list", "maj/*") == ""                        # branche supprimée
    assert git(a["dir"], "branch", "--show-current") == "main" and git(a["dir"], "status", "--porcelain") == ""
    assert git(a["dir"], "rev-parse", "HEAD") == git(monde["nu"], "rev-parse", "main")
    rc, out = monde["run"](b, "update.sh", "--sans-redemarrer")                    # l'autre PC
    assert rc == 0, out
    assert (b["dir"] / "page.py").read_text() == "TITRE = 'v2'\n" and (b["dir"] / "nouveau.py").exists()
    assert "Titre v2" in out


def test_rien_a_publier(monde):
    a = monde["pc"]("pc-a")
    rc, out = monde["run"](a, "publish.sh", "rien", "--sans-tests", "--sans-redemarrer")
    assert rc == 0 and "rien à publier" in out


@pytest.mark.parametrize("contenu", ["TOKEN = 'ghp_" + "a" * 36 + "'\n", "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n"])
def test_secret_refuse_rien_envoye(monde, contenu):
    a = monde["pc"]("pc-a"); avant = git(monde["nu"], "rev-parse", "main")
    (a["dir"] / "conf.py").write_text(contenu)
    rc, out = monde["run"](a, "publish.sh", "oups", "--sans-tests", "--sans-redemarrer")
    assert rc != 0 and "REFUS" in out
    assert git(monde["nu"], "rev-parse", "main") == avant and (a["dir"] / "conf.py").exists()
    assert git(a["dir"], "diff", "--cached", "--name-only") == ""                      # rien laissé indexé


def test_mot_interdit_prive(monde):
    a = monde["pc"]("pc-a")
    (monde["tmp"] / "interdits.txt").write_text("# mes vraies IP\n198.51.100.77\n")
    (a["dir"] / "page.py").write_text("SERVEUR = '198.51.100.77'\n")
    rc, out = monde["run"](a, "publish.sh", "ip", "--sans-tests", "--sans-redemarrer")
    assert rc != 0 and "mot interdit" in out and "v1" in sur_github(monde)


def test_marques_de_conflit_refusees(monde):
    a = monde["pc"]("pc-a")
    (a["dir"] / "page.py").write_text("<<<<<<< HEAD\nTITRE = 'a'\n=======\nTITRE = 'b'\n>>>>>>> origin/main\n")
    rc, out = monde["run"](a, "publish.sh", "fusion", "--sans-tests", "--sans-redemarrer")
    assert rc != 0 and "marques de conflit" in out and "v1" in sur_github(monde)


def test_deux_pc_meme_ligne_rien_d_ecrase(monde):
    a, b = monde["pc"]("pc-a"), monde["pc"]("pc-b")
    (a["dir"] / "page.py").write_text("TITRE = 'depuis A'\n")
    assert monde["run"](a, "publish.sh", "A", "--sans-tests", "--sans-redemarrer")[0] == 0
    (b["dir"] / "page.py").write_text("TITRE = 'depuis B'\n")                       # B n'a pas fait update
    rc, out = monde["run"](b, "publish.sh", "B", "--sans-tests", "--sans-redemarrer")
    assert rc != 0 and "mêmes lignes" in out
    assert "depuis A" in sur_github(monde)                                            # A n'est pas écrasé
    assert (b["dir"] / "page.py").read_text() == "TITRE = 'depuis B'\n"               # B n'est pas perdu
    assert git(b["dir"], "branch", "--show-current").startswith("maj/")


def test_deux_pc_fichiers_differents_tout_passe(monde):
    a, b = monde["pc"]("pc-a"), monde["pc"]("pc-b")
    (a["dir"] / "a.py").write_text("A = 1\n")
    assert monde["run"](a, "publish.sh", "A", "--sans-tests", "--sans-redemarrer")[0] == 0
    (b["dir"] / "b.py").write_text("B = 1\n")
    rc, out = monde["run"](b, "publish.sh", "B", "--sans-tests", "--sans-redemarrer")
    assert rc == 0, out
    assert sur_github(monde, "a.py") == "A = 1" and sur_github(monde, "b.py") == "B = 1"


def test_tests_en_echec_rien_envoye(monde):
    a = monde["pc"]("pc-a"); avant = git(monde["nu"], "rev-parse", "main")
    (a["dir"] / "tests").mkdir()
    (a["dir"] / "tests" / "test_x.py").write_text("def test_casse():\n    assert False\n")
    rc, out = monde["run"](a, "publish.sh", "cassé", "--sans-redemarrer")
    assert rc != 0 and "tests en échec" in out and git(monde["nu"], "rev-parse", "main") == avant



# ── l'assistant « devpilot pc » ─────────────────────────────────────────────

def assistant(m, p, reponses, app=None):
    env = dict(p["env"], DEVPILOT_APP=str(app or p["dir"]))
    r = subprocess.run(["bash", str(APP / "pc.sh")], input=reponses, env=env, capture_output=True, text=True, timeout=180)
    return r.stdout + r.stderr


def test_assistant_pc_non_installe(monde):
    a = monde["pc"]("pc-a")
    out = assistant(monde, a, "n\n", app=monde["tmp"] / "rien")
    assert "n'est pas installé" in out and "Installer DevPilot" in out


def test_assistant_ancienne_installation_mise_a_niveau(monde):
    a = monde["pc"]("pc-a")                         # aucun lanceur « devpilot » complet sur ce PC
    out = assistant(monde, a, "\n")                 # Entrée = mode recommandé
    assert "ancienne installation" in out and "1) Première mise à niveau (ancienne installation)   ← recommandé" in out
    lanceur = Path(a["env"]["HOME"]) / ".local" / "bin" / "devpilot"
    assert "publish" in lanceur.read_text() and "pc|assistant" in lanceur.read_text()


def test_assistant_recommande_mettre_a_jour_puis_le_fait(monde):
    a, b = monde["pc"]("pc-a"), monde["pc"]("pc-b")
    for p in (a, b):
        monde["run"](p, "update.sh", "--sans-redemarrer")       # installations à jour
    (a["dir"] / "page.py").write_text("TITRE = 'v2'\n")
    assert monde["run"](a, "publish.sh", "v2", "--sans-tests", "--sans-redemarrer")[0] == 0
    out = assistant(monde, b, "\n")
    assert "1 nouveauté(s) sur GitHub" in out and "2) Mettre à jour (récupérer ce qui a été publié)   ← recommandé" in out
    assert (b["dir"] / "page.py").read_text() == "TITRE = 'v2'\n"


def test_assistant_recommande_publier(monde):
    a = monde["pc"]("pc-a")
    monde["run"](a, "update.sh", "--sans-redemarrer")
    (a["dir"] / "page.py").write_text("TITRE = 'local'\n")
    out = assistant(monde, a, "q\n")
    assert "modifié(s) ici, pas encore publiés" in out and "3) Publier mes modifications (pour les autres PC)   ← recommandé" in out
