"""Project lifecycle scenarios (numbers = the scenario list of the plan)."""
import subprocess
import time

import pytest

from conftest import sh


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


# ── Names and locations ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", " ", "mon projet", "projét", "a/b", "it's", "-rf", ".hidden", "x" * 65])
def test_invalid_names(P, bad):
    err(P, P.validate_name, bad)


@pytest.mark.parametrize("good", ["marocdefender", "md_console", "site-v2", "a.b", "X1"])
def test_valid_names(P, good):
    assert P.validate_name(good) == good


def test_forbidden_locations(P, home):
    for bad in [home, home / "devpilot", home / "devpilot" / "projects", home / "devpilot" / ".devpilot" / "app"]:
        err(P, P.create, "x", path=str(bad), custom_location=True)
    err(P, P.create, "x", path="/tmp/outside-home-project", custom_location=True)


# ── 1. New empty project ────────────────────────────────────────────────────

def test_create_default_location(P, home):
    p = P.create("site")
    assert p["path"] == str(home / "devpilot" / "projects" / "site")
    assert (home / "devpilot" / "projects" / "site").is_dir()


def test_create_builds_project_space(P, home):
    import json
    P.create("site")
    dp = home / "devpilot" / "projects" / "site" / ".devpilot"
    assert (dp / "sessions").is_dir()
    assert json.loads((dp / "project.json").read_text())["name"] == "site"


def test_create_is_all_or_nothing(P, home, monkeypatch):
    def boom(*a, **k):
        raise P.ProjectError("db down")
    monkeypatch.setattr(P, "register", boom)
    err(P, P.create, "site")
    assert not (home / "devpilot" / "projects" / "site").exists()
    empty = home / "devpilot" / "projects" / "empty"
    empty.mkdir(parents=True)
    err(P, P.create, "empty")
    assert empty.is_dir() and not any(empty.iterdir())


def test_create_in_existing_empty_folder(P, home):
    d = home / "devpilot" / "projects" / "site"
    d.mkdir(parents=True)
    assert P.create("site")["path"] == str(d)


def test_create_with_git_init(P, home):
    p = P.create("site", git_init=True)
    assert (home / "devpilot" / "projects" / "site" / ".git").is_dir()
    assert p["repos"][0]["dir"] == "."


def test_create_outside_projects_needs_explicit_choice(P, home):
    e = err(P, P.create, "site", path=str(home / "work" / "site"))
    assert "hors de ~/devpilot/projects" in e.message
    assert not (home / "work" / "site").exists()
    p = P.create("site", path=str(home / "work" / "site"), custom_location=True)
    assert p["path"] == str(home / "work" / "site")


def test_create_duplicate_name_leaves_nothing(P, home):
    P.create("site")
    err(P, P.create, "SITE", path=str(home / "other"), custom_location=True)
    assert not (home / "other").exists()


def test_create_into_non_empty_folder_refused(P, home):
    d = home / "devpilot" / "projects" / "site"
    d.mkdir(parents=True)
    (d / "file.txt").write_text("keep me")
    err(P, P.create, "site")
    assert (d / "file.txt").read_text() == "keep me"


# ── 3/4. Clone ──────────────────────────────────────────────────────────────

def test_clone(P, home, remote):
    p = P.clone(remote)
    path = home / "devpilot" / "projects" / "hello"
    assert p["name"] == "hello" and p["path"] == str(path)
    assert (path / "package.json").exists()
    assert p["git_remote"] == remote
    assert p["color"] == "#fbbf24"


def test_clone_branch_and_name(P, home, remote):
    p = P.clone(remote, name="hello-dev", branch="dev")
    assert sh("git", "branch", "--show-current", cwd=p["path"]) == "dev"


def test_clone_bad_url_leaves_nothing(P, home, tmp_path):
    e = err(P, P.clone, f"file://{tmp_path}/nope.git", name="nope")
    assert "Clone echoue" in e.message
    assert not (home / "devpilot" / "projects" / "nope").exists()
    assert P.find_by_name("nope") is None


def test_clone_failure_into_existing_empty_dir_keeps_it_empty(P, home, tmp_path):
    d = home / "devpilot" / "projects" / "nope"
    d.mkdir(parents=True)
    err(P, P.clone, f"file://{tmp_path}/nope.git", name="nope")
    assert d.is_dir() and not any(d.iterdir())


def test_clone_name_taken_checked_before_cloning(P, home, remote):
    P.create("hello")
    err(P, P.clone, remote)
    assert [x.name for x in (home / "devpilot" / "projects" / "hello").iterdir()] == [".devpilot"]


def test_clone_bad_branch(P, home, remote):
    e = err(P, P.clone, remote, branch="nope")
    assert "branche introuvable" in e.message
    assert not (home / "devpilot" / "projects" / "hello").exists()


def test_clone_rejects_option_injection(P):
    err(P, P.clone, "--upload-pack=touch /tmp/pwned")


def test_clone_token_not_stored(P, home, remote):
    p = P.clone(remote, token="SECRET123")
    cfg = (home / "devpilot" / "projects" / "hello" / ".git" / "config").read_text()
    assert "SECRET123" not in cfg


# ── 5/10. Multi-repo folder (import terminal result) ────────────────────────

def test_register_multi_repo_folder(P, home, remote):
    root = home / "devpilot" / "projects" / "md"
    root.mkdir(parents=True)
    sh("git", "clone", "-q", remote, "md_console", cwd=root)
    sh("git", "clone", "-q", remote, "md_infra", cwd=root)
    p = P.register("md", root)
    assert [r["dir"] for r in p["repos"]] == ["md_console", "md_infra"]
    assert "md_console" in p["description"]


# ── 6. Existing folder ──────────────────────────────────────────────────────

def _folder(home, rel, content="x"):
    d = home / rel
    d.mkdir(parents=True)
    (d / "f.txt").write_text(content)
    return d


def test_adopt_default_moves_into_projects(P, home):
    src = _folder(home, "Desktop/app")
    p = P.adopt(src)
    assert not src.exists()
    assert (home / "devpilot" / "projects" / "app" / "f.txt").exists()
    assert P.is_managed(p["path"])


def test_adopt_keep_in_place(P, home):
    src = _folder(home, "Desktop/app")
    p = P.adopt(src, mode="link")
    assert src.exists() and p["path"] == str(src)


def test_adopt_copy(P, home):
    src = _folder(home, "Desktop/app")
    p = P.adopt(src, mode="copy", name="app2")
    assert src.exists() and (home / "devpilot" / "projects" / "app2" / "f.txt").exists()


def test_adopt_blocked_by_running_process(P, home):
    src = _folder(home, "Desktop/app")
    proc = subprocess.Popen(["sleep", "30"], cwd=src)
    try:
        time.sleep(0.2)
        e = err(P, P.adopt, src)
        assert e.details["blockers"] and src.exists()
    finally:
        proc.kill()


def test_same_folder_via_symlink_is_detected(P, home):
    src = _folder(home, "devpilot/projects/app")
    P.register("app", src)
    (home / "alias").symlink_to(src)
    e = err(P, P.adopt, home / "alias", mode="link", name="other")
    assert 'deja le projet "app"' in e.message


def test_nested_projects_refused(P, home):
    outer = _folder(home, "devpilot/projects/outer")
    inner = _folder(home, "devpilot/projects/outer/inner")
    P.register("outer", outer)
    err(P, P.register, "inner", inner)


# ── 7/8. Scan / folder added by hand ────────────────────────────────────────

def test_unregistered_folders(P, home):
    _folder(home, "devpilot/projects/manual")
    assert [f["name"] for f in P.unregistered_folders()] == ["manual"]
    P.adopt(home / "devpilot" / "projects" / "manual")      # already inside: registered in place
    assert P.unregistered_folders() == []


# ── 10. Rename ──────────────────────────────────────────────────────────────

def test_rename_is_display_only(P, home):
    p = P.create("site")
    r = P.rename(p["id"], "site-client")
    assert r["name"] == "site-client" and r["path"] == p["path"]
    import db
    with db.get_db() as c:
        assert c.execute("SELECT pattern FROM rules WHERE project_id=?", (p["id"],)).fetchone()[0] == "site\\-client"
    P.create("other")
    err(P, P.rename, p["id"], "OTHER")


# ── 11. Move ────────────────────────────────────────────────────────────────

def test_move_with_symlink(P, home):
    p = P.create("site")
    (home / "devpilot" / "projects" / "site" / "f.txt").write_text("x")
    dest = home / "work" / "site"
    err(P, P.move, p["id"], dest)                       # outside projects: must be explicit
    r = P.move(p["id"], dest, custom_location=True, leave_symlink=True)
    assert r["path"] == str(dest) and (dest / "f.txt").exists()
    assert (home / "devpilot" / "projects" / "site").is_symlink()


def test_move_refusals(P, home):
    p = P.create("site")
    src = home / "devpilot" / "projects" / "site"
    err(P, P.move, p["id"], src / "sub")                                    # into itself
    _folder(home, "devpilot/projects/busy")
    err(P, P.move, p["id"], home / "devpilot" / "projects" / "busy")        # not empty
    proc = subprocess.Popen(["sleep", "30"], cwd=src)
    try:
        time.sleep(0.2)
        e = err(P, P.move, p["id"], home / "devpilot" / "projects" / "site2")
        assert e.details["blockers"]
    finally:
        proc.kill()
    assert src.is_dir()


def test_ignored_pids_do_not_block(P, home):
    p = P.create("site")
    src = home / "devpilot" / "projects" / "site"
    proc = subprocess.Popen(["sleep", "30"], cwd=src)
    P.ignored_pids.append(lambda: [proc.pid])
    try:
        time.sleep(0.2)
        P.move(p["id"], home / "devpilot" / "projects" / "site2")
    finally:
        P.ignored_pids.clear()
        proc.kill()


def test_move_reports_venvs(P, home):
    p = P.create("site")
    venv = home / "devpilot" / "projects" / "site" / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin")
    r = P.move(p["id"], home / "devpilot" / "projects" / "site2")
    assert r["venvs_to_rebuild"] == [str(home / "devpilot" / "projects" / "site2" / ".venv")]


# ── 12. Duplicate / 13. Missing folder ──────────────────────────────────────

def test_duplicate(P, home):
    p = P.create("site")
    d = P.duplicate(p["id"], "site-copy")
    assert d["path"] == str(home / "devpilot" / "projects" / "site-copy")


def test_missing_folder_then_relocate(P, home):
    p = P.create("site")
    src = home / "devpilot" / "projects" / "site"
    src.rename(home / "devpilot" / "projects" / "moved")
    assert P.state(P.get(p["id"])) == "missing"
    err(P, P.move, p["id"], home / "x")
    r = P.relocate(p["id"], home / "devpilot" / "projects" / "moved")
    assert P.state(r) == "ok"


# ── 16-19. Remove ───────────────────────────────────────────────────────────

def test_remove_keeps_files_by_default(P, home):
    p = P.create("site")
    P.remove(p["id"])
    assert (home / "devpilot" / "projects" / "site").is_dir()
    assert P.find_by_name("site") is None


def test_delete_files_needs_typed_name(P, home):
    p = P.create("site")
    err(P, P.remove, p["id"], delete_files=True)
    err(P, P.remove, p["id"], delete_files=True, confirm_name="Site")
    assert (home / "devpilot" / "projects" / "site").is_dir()
    assert P.find_by_name("site")


def test_delete_files_goes_to_trash(P, home):
    p = P.create("site")
    (home / "devpilot" / "projects" / "site" / "f.txt").write_text("x")
    r = P.remove(p["id"], delete_files=True, confirm_name="site")
    trash = home / ".local" / "share" / "Trash"
    assert not (home / "devpilot" / "projects" / "site").exists()
    assert (trash / "files" / "site" / "f.txt").exists()
    info = (trash / "info" / "site.trashinfo").read_text()
    assert "Path=" + str(home / "devpilot" / "projects" / "site") in info
    # a second project with the same name does not overwrite the first one in the trash
    p2 = P.create("site")
    P.remove(p2["id"], delete_files=True, confirm_name="site")
    assert (trash / "files" / "site.2").is_dir()


def test_removal_report_git_risks(P, home, remote):
    p = P.clone(remote)
    path = p["path"]
    (home / "devpilot" / "projects" / "hello" / "new.txt").write_text("x")
    (home / "devpilot" / "projects" / "hello" / "package.json").write_text('{"a":1}')
    sh("git", "commit", "-qam", "local", cwd=path)
    (home / "devpilot" / "projects" / "hello" / "package.json").write_text('{"a":2}')
    sh("git", "stash", "-q", cwd=path)
    rep = P.removal_report(p["id"])
    s = rep["repos"][0]["state"]
    assert (s["unpushed"], s["untracked"], s["stash"]) == (1, 1, 1)
    assert any("non pousse" in r for r in rep["risks"])


def test_delete_blocked_by_process_and_nested(P, home):
    p = P.create("site")
    src = home / "devpilot" / "projects" / "site"
    proc = subprocess.Popen(["sleep", "30"], cwd=src)
    try:
        time.sleep(0.2)
        e = err(P, P.remove, p["id"], delete_files=True, confirm_name="site")
        assert e.details["blockers"]
    finally:
        proc.kill()
    assert src.is_dir() and P.find_by_name("site")


def test_on_removed_hook(P, home):
    seen = []
    P.on_removed.append(lambda proj: seen.append(proj["name"]))
    try:
        P.remove(P.create("site")["id"])
    finally:
        P.on_removed.clear()
    assert seen == ["site"]


def test_safe_rmtree(P, home):
    root = _folder(home, "devpilot/projects/site")
    cache = _folder(home, "devpilot/projects/site/node_modules")
    err(P, P.safe_rmtree, root, root)
    err(P, P.safe_rmtree, home, root)
    err(P, P.safe_rmtree, root / ".." / "..", root)
    P.safe_rmtree(cache, root)
    assert not cache.exists() and root.exists()


# ── Clone progress / 14. Link git / DevPilot files ─────────────────────────

def test_clone_streams_progress(P, home, remote):
    lines = []
    P.clone(remote, on_output=lines.append)
    assert any("Cloning" in l or "Clonage" in l for l in lines)


def test_exclude_is_idempotent(P, home):
    p = P.create("site", git_init=True)
    P.exclude_from_git(p["path"])
    P.exclude_from_git(p["path"])
    txt = (home / "devpilot" / "projects" / "site" / ".git" / "info" / "exclude").read_text()
    assert txt.count(".devpilot/") == 1


# ── Everything of a project lives in its folder (.devpilot/) ─────────────────

def test_space_follows_every_change(P, home):
    import json, db
    p = P.create("site", description="vitrine")
    dp = home / "devpilot" / "projects" / "site" / ".devpilot"
    m = json.loads((dp / "project.json").read_text())
    assert (m["name"], m["description"]) == ("site", "vitrine") and m["created_at"]
    assert "path" not in m                                  # the folder can move
    db.save_project_specs(p["id"], {"client_name": "X"}, [{"text": "logo", "done": True}], "Fais un site")
    P.write_space(p["id"])
    assert (dp / "prompt.md").read_text() == "Fais un site"
    assert json.loads((dp / "checklist.json").read_text())[0]["text"] == "logo"
    assert "- [x] logo" in (dp / "checklist.md").read_text()
    P.rename(p["id"], "site-client")
    assert json.loads((dp / "project.json").read_text())["name"] == "site-client"


def test_reimported_folder_brings_its_project_back(P, home):
    import db
    p = P.create("site", description="vitrine")
    db.update_project(p["id"], color="#123456", status="paused")
    db.save_project_specs(p["id"], {}, [{"text": "logo", "done": False}], "Fais un site")
    P.write_space(p["id"])
    P.remove(p["id"])                                        # DB forgets, folder stays
    q = P.adopt(home / "devpilot" / "projects" / "site")
    assert (q["description"], q["color"], q["status"]) == ("vitrine", "#123456", "paused")
    specs = db.get_project_specs(q["id"])
    assert specs["prompt"] == "Fais un site" and specs["checklist"][0]["text"] == "logo"


def test_moved_folder_keeps_its_space(P, home):
    import json
    p = P.create("site", description="vitrine")
    P.move(p["id"], home / "devpilot" / "projects" / "site2")
    m = json.loads((home / "devpilot" / "projects" / "site2" / ".devpilot" / "project.json").read_text())
    assert m["description"] == "vitrine"


# ── Step 2: link a project to GitHub, pull/push ready ──────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/osarsar/site", ("osarsar", "site")),
    ("https://github.com/osarsar/site.git", ("osarsar", "site")),
    ("git@github.com:osarsar/site.git", ("osarsar", "site")),
    ("ssh://git@github.com/osarsar/site.git", ("osarsar", "site")),
    ("osarsar/site", ("osarsar", "site")),
    ("github.com/osarsar/site/", ("osarsar", "site")),
    ("https://github.com/osarsar/site/tree/main/src", ("osarsar", "site")),
    ("https://gitlab.com/a/b", None),
    ("/tmp/x.git", None),
])
def test_parse_github(P, url, expected):
    assert P.parse_github(url) == expected


@pytest.fixture
def empty_remote(tmp_path):
    bare = tmp_path / "remotes" / "empty.git"
    bare.parent.mkdir(exist_ok=True)
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    return f"file://{bare}"


def _commit_all(path, msg="work"):
    sh("git", "add", "-A", cwd=path)
    sh("git", "commit", "-qm", msg, cwd=path)


def test_new_project_linked_to_empty_repo_then_push(P, home, empty_remote, tmp_path):
    p = P.create("site")
    path = home / "devpilot" / "projects" / "site"
    r = P.link_git(p["id"], empty_remote)
    assert r["action"] == "linked_empty" and r["branch"] == "main"
    (path / "index.html").write_text("<h1>hi</h1>")
    _commit_all(path)
    sh("git", "push", "-q", cwd=path)                          # plain push works: upstream is set
    assert sh("git", "ls-remote", empty_remote).endswith("refs/heads/main")
    assert ".devpilot" not in sh("git", "ls-tree", "-r", "--name-only", "HEAD", cwd=path)
    other = tmp_path / "other"                                  # someone else pushes...
    sh("git", "clone", "-q", empty_remote, str(other))
    (other / "b.txt").write_text("b")
    _commit_all(other)
    sh("git", "push", "-q", cwd=other)
    sh("git", "pull", "-q", cwd=path)                           # ...plain pull gets it
    assert (path / "b.txt").exists()


def test_new_project_linked_to_repo_with_code(P, home, remote):
    p = P.create("site")
    path = home / "devpilot" / "projects" / "site"
    (path / ".devpilot" / "prompt.md").write_text("keep")
    r = P.link_git(p["id"], remote)
    assert r["action"] == "cloned"
    assert (path / "package.json").exists() and (path / ".devpilot" / "prompt.md").read_text() == "keep"
    assert sh("git", "status", "--porcelain", cwd=path) in ("", "?? CLAUDE.md")
    (path / "x.txt").write_text("x")
    _commit_all(path)
    sh("git", "push", "-q", cwd=path)
    sh("git", "pull", "-q", cwd=path)
    comp = __import__("db").get_project_component(p["id"], "git")
    assert comp and comp["enabled"]


def test_link_folder_with_own_files_never_merges(P, home, remote):
    p = P.create("site")
    path = home / "devpilot" / "projects" / "site"
    (path / "package.json").write_text("MINE")
    r = P.link_git(p["id"], remote)
    assert r["action"] == "needs_choice" and "Rien n" in r["message"]
    assert (path / "package.json").read_text() == "MINE"


    assert sh("git", "rev-parse", "--verify", "origin/main", cwd=path)
    assert sh("git", "config", "branch.main.merge", cwd=path) == "refs/heads/main"


def test_relink_to_another_repo_needs_confirmation(P, home, remote, empty_remote):
    p = P.create("site")
    P.link_git(p["id"], remote)
    e = err(P, P.link_git, p["id"], empty_remote)
    assert e.details["needs_confirm"]
    path = home / "devpilot" / "projects" / "site"
    assert sh("git", "remote", "get-url", "origin", cwd=path) == remote
    P.link_git(p["id"], empty_remote, replace=True)
    assert sh("git", "remote", "get-url", "origin", cwd=path) == empty_remote


def test_link_same_repo_again_is_fine(P, home, remote):
    p = P.create("site")
    P.link_git(p["id"], remote)
    assert P.link_git(p["id"], remote)["action"] == "remote_updated"


def test_multi_repo_project_is_not_wrapped_in_a_new_repo(P, home, remote):
    root = home / "devpilot" / "projects" / "md"
    root.mkdir(parents=True)
    sh("git", "clone", "-q", remote, "md_console", cwd=root)
    p = P.register("md", root)
    e = err(P, P.link_git, p["id"], remote)
    assert "md_console" in e.message and not (root / ".git").exists()


def test_link_unreachable_repo_changes_nothing(P, home, tmp_path):
    p = P.create("site")
    err(P, P.link_git, p["id"], f"file://{tmp_path}/nope.git")
    assert not (home / "devpilot" / "projects" / "site" / ".git").exists()
    assert P.get(p["id"])["git_remote"] == ""


def test_link_writes_remote_in_project_space(P, home, remote):
    import json
    p = P.create("site")
    P.link_git(p["id"], remote)
    m = json.loads((home / "devpilot" / "projects" / "site" / ".devpilot" / "project.json").read_text())
    assert m["git_remote"] == remote


def test_link_keeps_devpilot_claude_md_out_of_the_repo(P, home, empty_remote):
    p = P.create("site")
    path = home / "devpilot" / "projects" / "site"
    (path / "CLAUDE.md").write_text(P.DEVPILOT_MARK + "\nconnexions...")
    P.link_git(p["id"], empty_remote)
    (path / "index.html").write_text("x")
    _commit_all(path)
    assert sh("git", "ls-tree", "-r", "--name-only", "HEAD", cwd=path) == "index.html"


def test_link_keeps_repo_own_claude_md_tracked(P, home, tmp_path):
    src = tmp_path / "withmd"
    src.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=src)
    (src / "CLAUDE.md").write_text("repo rules")
    _commit_all(src)
    bare = tmp_path / "remotes" / "withmd.git"
    bare.parent.mkdir(exist_ok=True)
    sh("git", "clone", "-q", "--bare", str(src), str(bare))
    p = P.create("site")
    P.link_git(p["id"], f"file://{bare}")
    path = home / "devpilot" / "projects" / "site"
    assert (path / "CLAUDE.md").read_text() == "repo rules"
    assert "CLAUDE.md" in sh("git", "ls-files", cwd=path)
