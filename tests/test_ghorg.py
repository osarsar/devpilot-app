"""Multi-repo project linked to a GitHub organisation (MarocDefender layout)."""
import pytest

from conftest import sh

ORG = "acme"
NAMES = ["md_console", "md_infra", "md_backend", "md_release"]


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


@pytest.fixture
def org(home, tmp_path, monkeypatch):
    """Bare repos standing for github.com/acme/*, reachable at their real GitHub
    SSH address through git's insteadOf (no network)."""
    remotes = tmp_path / "gh" / ORG
    for n in NAMES:
        src = tmp_path / "src" / n
        src.mkdir(parents=True)
        sh("git", "init", "-q", "-b", "main", cwd=src)
        (src / "README.md").write_text(n)
        sh("git", "add", "-A", cwd=src)
        sh("git", "commit", "-qm", "init", cwd=src)
        sh("git", "clone", "-q", "--bare", str(src), str(remotes / f"{n}.git"))
    sh("git", "config", "--global", f"url.file://{remotes}/.insteadOf", f"git@github.com:{ORG}/")
    import importlib, ghorg
    importlib.reload(ghorg)
    monkeypatch.setattr(ghorg, "list_repos", lambda o: [
        {"name": n, "description": "", "default_branch": "main", "private": True, "archived": False,
         "ssh": f"git@github.com:{ORG}/{n}.git", "web": f"https://github.com/{ORG}/{n}"} for n in NAMES])
    return ghorg, remotes


@pytest.fixture
def md(P, home, org):
    """Project laid out like MarocDefender: console, infra, a service nested inside infra."""
    root = home / "devpilot" / "projects" / "md"
    root.mkdir(parents=True)
    sh("git", "clone", "-q", f"git@github.com:{ORG}/md_console.git", "md_console", cwd=root)
    sh("git", "clone", "-q", f"git@github.com:{ORG}/md_infra.git", "md_infra", cwd=root)
    (root / "md_infra" / "marocdefender").mkdir()
    # like the real md_infra/.gitignore: the service repos nested inside are not part of it
    (root / "md_infra" / ".git" / "info" / "exclude").write_text("marocdefender/\n")
    sh("git", "clone", "-q", f"git@github.com:{ORG}/md_backend.git", "marocdefender/md-backend", cwd=root / "md_infra")
    p = P.register("md", root)
    return p["id"], root


@pytest.mark.parametrize("text,expected", [
    ("marocdefender", "marocdefender"), ("github.com/marocdefender", "marocdefender"),
    ("https://github.com/marocdefender", "marocdefender"),
    ("https://github.com/orgs/marocdefender/repositories", "marocdefender"),
    ("https://github.com/marocdefender/", "marocdefender"), ("a b", None), ("-x", None)])
def test_parse_org(org, text, expected):
    G, _ = org
    assert G.parse_org(text) == expected


def test_link_org_finds_nested_clones(P, org, md):
    G, _ = org
    pid, root = md
    r = G.link_org(pid, "https://github.com/orgs/acme/repositories")
    assert (r["repos"], r["cloned"]) == (4, 3)
    places = P.read_manifest(root)["github"]["places"]
    assert places["md_backend"] == ["md_infra/marocdefender/md-backend"]
    assert places["md_release"] == []
    assert P.get(pid)["git_remote"] == "https://github.com/acme"


def test_overview_states(org, md):
    G, _ = org
    pid, root = md
    G.link_org(pid, ORG)
    (root / "md_console" / "new.txt").write_text("x")
    ov = {r["name"]: r for r in G.overview(pid, fetch=False)["repos"]}
    assert ov["md_console"]["clones"][0]["untracked"] == 1
    assert ov["md_backend"]["clones"][0]["branch"] == "main"
    assert ov["md_release"]["clones"] == []


def test_roles_are_kept_in_project_space(P, org, md):
    G, _ = org
    pid, root = md
    G.link_org(pid, ORG)
    G.set_role(pid, "md_backend", "Service backend de l'app")
    assert P.read_manifest(root)["github"]["roles"]["md_backend"] == "Service backend de l'app"
    assert {r["name"]: r["role"] for r in G.overview(pid, fetch=False)["repos"]}["md_backend"] == "Service backend de l'app"
    G.link_org(pid, ORG)                                          # relink keeps the roles
    assert P.read_manifest(root)["github"]["roles"]["md_backend"]


def test_clone_missing_repo(P, org, md):
    G, _ = org
    pid, root = md
    G.link_org(pid, ORG)
    r = G.clone_repo(pid, "md_release")
    assert (root / "md_release" / "README.md").exists() and r["dir"] == "md_release"
    assert P.read_manifest(root)["github"]["places"]["md_release"] == ["md_release"]
    err(P, G.clone_repo, pid, "md_release")                       # already there, not empty
    err(P, G.clone_repo, pid, "md_model")                         # not in the organisation
    err(P, G.clone_repo, pid, "md_release", dest="../../outside")  # outside the project


def test_pull_all_only_touches_clean_repos(org, md, tmp_path):
    G, remotes = org
    pid, root = md
    G.link_org(pid, ORG)
    for n in ("md_console", "md_backend"):                        # GitHub moves on for two repos
        other = tmp_path / f"other-{n}"
        sh("git", "clone", "-q", str(remotes / f"{n}.git"), str(other))
        (other / "up.txt").write_text("up")
        sh("git", "add", "-A", cwd=other)
        sh("git", "commit", "-qm", "up", cwd=other)
        sh("git", "push", "-q", cwd=other)
    (root / "md_console" / "wip.txt").write_text("mine")           # md_console has local work
    rep = {x["repo"].split(" ")[0]: x for x in G.pull_all(pid)["report"]}
    assert rep["md_backend"]["result"] == "recupere"
    assert (root / "md_infra" / "marocdefender" / "md-backend" / "up.txt").exists()
    assert rep["md_console"]["result"] == "ignore" and not (root / "md_console" / "up.txt").exists()
    assert rep["md_infra"]["result"] == "a jour"


def test_sync_works_on_a_nested_repo(org, md):
    import importlib, gitsync
    importlib.reload(gitsync)
    G, _ = org
    pid, root = md
    G.link_org(pid, ORG)
    s = gitsync.status(pid, "md_infra/marocdefender/md-backend", fetch=False)
    assert s["branch"] == "main" and s["upstream"] == "origin/main"


# ── Link from the terminal: `git clone` in the project folder, then DevPilot records ──

import subprocess
from pathlib import Path

RC = Path(__file__).resolve().parent.parent / "import_rc.sh"


def term(cwd, cmd):
    """Run cmd the way the DevPilot terminal does (same rcfile)."""
    return subprocess.run(["bash", "-c", f"source {RC} >/dev/null; {cmd}"], cwd=cwd,
                          capture_output=True, text=True)


def test_clone_dot_into_new_project_folder(P, home, org):
    p = P.create("site")
    d = home / "devpilot" / "projects" / "site"
    (d / ".devpilot" / "prompt.md").write_text("keep")
    (d / "CLAUDE.md").write_text(P.DEVPILOT_MARK + "\nctx")
    r = term(d, f"git clone -q git@github.com:{ORG}/md_console.git .")
    assert r.returncode == 0, r.stderr
    assert (d / "README.md").exists() and (d / ".git").is_dir()
    assert (d / ".devpilot" / "prompt.md").read_text() == "keep" and P.is_devpilot_file(d / "CLAUDE.md")
    res = P.detect_link(p["id"], record=True)
    assert res["kind"] == "single" and P.get(p["id"])["git_remote"] == f"git@github.com:{ORG}/md_console.git"
    assert sh("git", "status", "--porcelain", cwd=d) == ""          # .devpilot/ and CLAUDE.md excluded


def test_clone_dot_repo_own_claude_md_wins(P, home, tmp_path):
    src = tmp_path / "withmd"
    src.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=src)
    (src / "CLAUDE.md").write_text("repo rules")
    sh("git", "add", "-A", cwd=src)
    sh("git", "commit", "-qm", "i", cwd=src)
    P.create("site")
    d = home / "devpilot" / "projects" / "site"
    (d / "CLAUDE.md").write_text(P.DEVPILOT_MARK + "\nctx")
    assert term(d, f"git clone -q {src} .").returncode == 0
    assert (d / "CLAUDE.md").read_text() == "repo rules"
    assert P.is_devpilot_file(d / ".devpilot" / "CLAUDE.devpilot.md")


def test_clone_dot_with_other_files_is_plain_git(P, home, org):
    P.create("site")
    d = home / "devpilot" / "projects" / "site"
    (d / "mine.txt").write_text("x")
    r = term(d, f"git clone -q git@github.com:{ORG}/md_console.git .")
    assert r.returncode != 0 and "not an empty directory" in r.stderr
    assert (d / ".devpilot").is_dir() and (d / "mine.txt").exists()


def test_reconstruct_like_multi_repo_then_org_detected(P, home, org):
    G, _ = org
    p = P.create("md")
    d = home / "devpilot" / "projects" / "md"
    assert term(d, f"git clone -q git@github.com:{ORG}/md_console.git && git clone -q git@github.com:{ORG}/md_infra.git").returncode == 0
    live = P.detect_link(p["id"])
    assert live["kind"] == "org" and live["org"] == ORG and {r["dir"] for r in live["repos"]} == {"md_console", "md_infra"}
    res = P.detect_link(p["id"], record=True)
    assert "2 deja sur ton PC" in res["message"]
    assert P.read_manifest(d)["github"]["org"] == ORG


def test_detect_nothing_cloned(P, home):
    p = P.create("site")
    assert P.detect_link(p["id"])["kind"] is None
    err(P, P.detect_link, p["id"], record=True)


def test_detect_mixed_owners(P, home, org, tmp_path):
    p = P.create("mix")
    d = home / "devpilot" / "projects" / "mix"
    sh("git", "clone", "-q", f"git@github.com:{ORG}/md_console.git", "a", cwd=d)
    sh("git", "init", "-q", "b", cwd=d)
    res = P.detect_link(p["id"], record=True)
    assert res["kind"] == "multi" and "2 depot(s)" in res["message"]


def test_clone_repo_named_like_project_goes_into_the_folder(P, home, org):
    p = P.create("md_console")                                     # project named like the repo
    d = home / "devpilot" / "projects" / "md_console"
    (d / ".devpilot" / "prompt.md").write_text("keep")
    r = term(d, f"git clone -q git@github.com:{ORG}/md_console.git")
    assert r.returncode == 0, r.stderr
    assert (d / ".git").is_dir() and (d / "README.md").exists() and not (d / "md_console").exists()
    assert (d / ".devpilot" / "prompt.md").read_text() == "keep"
    assert P.detect_link(p["id"], record=True)["kind"] == "single"


@pytest.mark.parametrize("cmd", [
    "git clone -q -b main git@github.com:acme/md_console.git",
    "git clone --branch=main -q git@github.com:acme/md_console.git/",
])
def test_clone_same_name_with_options(P, home, org, cmd):
    P.create("md_console")
    d = home / "devpilot" / "projects" / "md_console"
    assert term(d, cmd).returncode == 0
    assert (d / "README.md").exists() and not (d / "md_console").exists()


def test_clone_other_name_still_goes_to_a_subfolder(P, home, org):
    P.create("md")
    d = home / "devpilot" / "projects" / "md"
    assert term(d, f"git clone -q git@github.com:{ORG}/md_console.git").returncode == 0
    assert (d / "md_console" / "README.md").exists() and not (d / ".git").exists()


def test_clone_explicit_destination_is_respected(P, home, org):
    P.create("md_console")
    d = home / "devpilot" / "projects" / "md_console"
    assert term(d, f"git clone -q git@github.com:{ORG}/md_console.git src").returncode == 0
    assert (d / "src" / "README.md").exists() and not (d / ".git").exists()


def test_reimport_keeps_the_organisation_link(P, org, md):
    G, _ = org
    pid, root = md
    G.link_org(pid, ORG)
    P.remove(pid)
    q = P.adopt(root, mode="link")
    assert q["git_remote"] == f"https://github.com/{ORG}"
