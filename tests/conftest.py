"""Each test gets a throwaway HOME (its own DevPilot DB, projects folder and
trash) and fresh db/projects modules bound to it."""
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    import db
    importlib.reload(db)
    import projects
    importlib.reload(projects)
    return h


@pytest.fixture
def P(home):
    import projects
    return projects


def sh(*args, cwd=None):
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A bare repo with one commit on 'main' and a 'dev' branch: file:// URL."""
    src = tmp_path / "src"
    src.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=src)
    (src / "package.json").write_text("{}")
    sh("git", "add", ".", cwd=src)
    sh("git", "commit", "-qm", "init", cwd=src)
    sh("git", "branch", "dev", cwd=src)
    bare = tmp_path / "remotes" / "hello.git"
    bare.parent.mkdir()
    sh("git", "clone", "-q", "--bare", str(src), str(bare))
    return f"file://{bare}"
