"""devpilot effacer : vider ce PC de tout ce qui touche à DevPilot et aux projets — sans jamais
perdre en silence le travail qui n'est QUE sur ce PC, ni sortir du dossier personnel.
HOME isolé ; faux gpg ; un projet « propre » (tout sur GitHub) et un projet avec du travail local."""
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
ENVG = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

HOOK = r'''#!/usr/bin/env bash
case "$1" in
  liste)
    echo '{"id":"avant","titre":"Action avant","action":true,"avant":true,"defaut":true}'
    echo '{"id":"secret","titre":"Secret du projet","chemins":["~/.secret-projet"],"secret":true,"defaut":true}'
    echo '{"id":"evasion","titre":"Hors du dossier perso","chemins":["/tmp/ne-jamais-effacer-%s"],"defaut":true}' ;;
  faire) [ -d "$HOME/devpilot/projects/propre" ] && echo projet-encore-la > "$HOME/.marque-avant" ;;
  verifier) echo "garde ta passphrase papier" ;;
esac
'''


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, env=dict(os.environ, **ENVG), check=True, capture_output=True)


@pytest.fixture
def monde(tmp_path):
    home = tmp_path / "home"; projets = home / "devpilot" / "projects"; projets.mkdir(parents=True)
    data = home / "devpilot" / ".devpilot" / "data"; data.mkdir(parents=True)
    (home / "devpilot" / ".devpilot" / "app").mkdir()
    nu = tmp_path / "gh.git"; src = tmp_path / "src"; src.mkdir()
    git(src, "init", "-q", "-b", "main"); (src / "a.txt").write_text("a"); git(src, "add", "-A"); git(src, "commit", "-qm", "init")
    git(tmp_path, "clone", "-q", "--bare", str(src), str(nu))
    for nom in ("propre", "travail"):
        git(tmp_path, "clone", "-q", str(nu), str(projets / nom))
    git(projets / "travail", "switch", "-q", "-c", "feat/local")
    (projets / "travail" / "b.txt").write_text("b"); git(projets / "travail", "add", "-A"); git(projets / "travail", "commit", "-qm", "local")
    temoin = tmp_path / f"ne-jamais-effacer-{tmp_path.name}"; temoin.write_text("x")
    (projets / "propre" / "effacer-poste.sh").write_text(HOOK % tmp_path.name)
    git(projets / "propre", "add", "-A"); git(projets / "propre", "commit", "-qm", "crochet")
    git(projets / "propre", "push", "-q", "origin", "main")
    con = sqlite3.connect(data / "devpilot.db")
    con.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, path TEXT)")
    con.executemany("INSERT INTO projects(name, path) VALUES(?,?)", [("propre", str(projets / "propre")), ("travail", str(projets / "travail"))])
    con.commit(); con.close()
    (home / ".local" / "bin").mkdir(parents=True); (home / ".local" / "bin" / "devpilot").write_text("#!/bin/sh\n")
    (home / ".config" / "devpilot").mkdir(parents=True); (home / ".config" / "devpilot" / "interdits.txt").write_text("x")
    (home / "propre-lien").symlink_to(projets / "propre")
    (home / ".secret-projet").write_text("s3cret")
    (home / ".ssh").mkdir(); (home / ".ssh" / "id_ed25519").write_text("cle perso")
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "gpg").write_text('#!/bin/bash\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\ncat > "$out"\n')
    (bin_ / "gpg").chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=f"{bin_}:{os.environ['PATH']}", DEVPILOT_PORT="59999",
               DEVPILOT_TMUX_SOCKET=f"dptest-effacer-{tmp_path.name}", **ENVG)
    env.pop("TMUX", None)

    def lancer(entree, *args):
        r = subprocess.run([sys.executable, str(APP / "effacer.py"), *args], input=entree, env=env,
                           capture_output=True, text=True, timeout=300, cwd=str(tmp_path))
        return r.returncode, r.stdout + r.stderr

    def nb_questions(mode):
        _, out = lancer(f"{mode}\n" + "\n" * 60, "--simulation")
        return out.count("[o/N]") + out.count("[O/n]")

    return {"home": home, "projets": projets, "lancer": lancer, "temoin": temoin, "nb": nb_questions,
            "phrase": f"EFFACER {socket.gethostname()}"}


def test_simulation_n_efface_rien(monde):
    rc, out = monde["lancer"]("1\n" + "\n" * 20, "--simulation")
    assert rc == 0 and "SIMULATION" in out and "effacerait" in out
    assert (monde["projets"] / "propre").exists() and (monde["home"] / ".local/bin/devpilot").exists()


def test_inventaire_signale_le_travail_local(monde):
    rc, out = monde["lancer"]("2\n" + "n\n" * 20, "--simulation")
    assert "travail qui n'est QUE sur ce PC" in out and "feat/local" in out and "garde ta passphrase papier" in out


def test_tout_effacer_sauf_le_travail_non_sauve(monde):
    h = monde["home"]
    rc, out = monde["lancer"]("1\n" + "\n" * monde["nb"](1) + monde["phrase"] + "\nphrase-archive-1\nphrase-archive-1\n")
    assert rc == 0, out
    assert not (monde["projets"] / "propre").exists()                         # projet propre : effacé
    assert (monde["projets"] / "travail").exists()                            # travail local : GARDÉ (défaut non)
    assert not (h / "devpilot" / ".devpilot").exists() and not (h / ".local/bin/devpilot").exists()
    assert not (h / ".config/devpilot").exists() and not (h / "propre-lien").exists()
    assert not (h / ".secret-projet").exists()                                # secret déclaré par le projet
    assert (h / ".marque-avant").read_text().strip() == "projet-encore-la"    # action « avant » AVANT d'effacer
    assert monde["temoin"].exists() and "hors de ton dossier personnel" in out   # jamais hors du HOME
    assert (h / ".ssh" / "id_ed25519").exists() and (h / ".ssh").is_dir()     # clés perso intactes
    assert list(h.glob("devpilot-donnees-*.tar.gz.gpg"))                      # données sauvegardées avant


def test_le_travail_local_s_efface_seulement_si_on_le_dit(monde):
    rc, out = monde["lancer"]("2\n" + "o\n" * monde["nb"](2) + monde["phrase"] + "\nphrase-archive-1\nphrase-archive-1\n")
    assert rc == 0 and not (monde["projets"] / "travail").exists() and not (monde["home"] / "devpilot").exists()


def test_mauvaise_confirmation_rien_n_est_efface(monde):
    rc, out = monde["lancer"]("1\n" + "\n" * monde["nb"](1) + "effacer\n")
    assert rc == 1 and "RIEN n'a été effacé" in out
    assert (monde["projets"] / "propre").exists() and (monde["home"] / ".local/bin/devpilot").exists()


def test_garde_fous_des_chemins(monde, monkeypatch):
    monkeypatch.setenv("HOME", str(monde["home"]))
    import importlib
    sys.path.insert(0, str(APP))
    import effacer
    importlib.reload(effacer)
    h = effacer.HOME
    assert effacer.autorise(h) and effacer.autorise(h / ".ssh") and effacer.autorise(h / ".config")
    assert "hors" in effacer.autorise(Path("/etc/passwd")) and "hors" in effacer.autorise(Path(str(h) + "-autre/x"))
    (h / "fuite").symlink_to("/tmp")
    assert "sort" in effacer.autorise(h / "fuite" / "x")                     # un lien ne fait pas sortir du HOME
    assert effacer.autorise(h / ".ssh" / "md") == ""                        # DEDANS : permis


def test_refuse_depuis_un_terminal_devpilot(monde):
    env = dict(os.environ, HOME=str(monde["home"]), TMUX="/tmp/tmux-1000/devpilot,123,0")
    r = subprocess.run([sys.executable, str(APP / "effacer.py")], input="", env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "terminal NORMAL" in r.stdout



def test_archive_reelle_chiffree_et_relisible(monde, tmp_path):
    """Vrai gpg (pas le faux) : la phrase passe par le programme, l'archive se relit avec."""
    import shutil
    if not shutil.which("gpg"):
        pytest.skip("gpg absent")
    h = monde["home"]
    gnupg = tmp_path / "gnupg"; gnupg.mkdir(mode=0o700)
    env = dict(os.environ, HOME=str(h), GNUPGHOME=str(gnupg))
    script = f"""
import sys; sys.path.insert(0, {str(APP)!r})
import effacer
journal = []
effacer.action_export(effacer.ESPACE / "data")(journal)
print("\\n".join(journal))
"""
    r = subprocess.run([sys.executable, "-c", script], input="trop\nphrase-archive-1\nphrase-archive-1\n", env=env,
                       capture_output=True, text=True, timeout=120)
    assert "8 caractères minimum" in r.stdout and "sauvegardées" in r.stdout, r.stdout + r.stderr
    arch = next(h.glob("devpilot-donnees-*.tar.gz.gpg"))
    d = subprocess.run(f"gpg --batch --pinentry-mode loopback --passphrase phrase-archive-1 -d {arch} | tar -tz",
                       shell=True, env=env, capture_output=True, text=True)
    assert "data/devpilot.db" in d.stdout, d.stderr
