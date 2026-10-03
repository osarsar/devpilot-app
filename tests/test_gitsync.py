"""Folder vs GitHub: reconcile at link time, then sync (pull --rebase) while working."""
import pytest

from conftest import sh


@pytest.fixture
def G(home):
    import importlib, gitsync
    importlib.reload(gitsync)
    return gitsync


def err(P, fn, *a, **kw):
    with pytest.raises(P.ProjectError) as e:
        fn(*a, **kw)
    return e.value


def commit_all(path, msg="work"):
    sh("git", "add", "-A", cwd=path)
    sh("git", "commit", "-qm", msg, cwd=path)


@pytest.fixture
def gh(tmp_path):
    """Remote with: same.txt, diff.txt (GitHub version), gh_only.txt, .gitignore(node_modules/)."""
    src = tmp_path / "ghsrc"
    src.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=src)
    (src / "same.txt").write_text("same\n")
    (src / "diff.txt").write_text("github version\n")
    (src / "gh_only.txt").write_text("only on github\n")
    (src / ".gitignore").write_text("node_modules/\n")
    commit_all(src, "initial")
    bare = tmp_path / "remotes" / "gh.git"
    bare.parent.mkdir(exist_ok=True)
    sh("git", "clone", "-q", "--bare", str(src), str(bare))
    return f"file://{bare}", bare


@pytest.fixture
def linked(P, home, gh):
    """A project whose folder has its own files, linked to gh → waiting for a choice."""
    url, bare = gh
    p = P.create("site")
    path = home / "devpilot" / "projects" / "site"
    (path / "same.txt").write_text("same\n")
    (path / "diff.txt").write_text("my version\n")
    (path / "mine_only.txt").write_text("only here\n")
    (path / "node_modules").mkdir()
    (path / "node_modules" / "big.js").write_text("x")
    r = P.link_git(p["id"], url)
    assert r["action"] == "needs_choice"
    return p["id"], path, bare, r


# ── 1. At link time ─────────────────────────────────────────────────────────

def test_compare(linked):
    pid, path, bare, r = linked
    c = r["compare"]
    assert c["identical"] == ["same.txt"]
    assert c["different"] == ["diff.txt"]
    assert c["only_local"] == ["mine_only.txt"]                  # node_modules ignored (repo's .gitignore)
    assert c["only_github"] == [".gitignore", "gh_only.txt"]
    assert (path / "diff.txt").read_text() == "my version\n"     # nothing changed yet


def test_github_wins(G, linked):
    pid, path, bare, _ = linked
    r = G.reconcile(pid, "github")
    assert (path / "diff.txt").read_text() == "github version\n"
    assert not (path / "mine_only.txt").exists() and (path / "gh_only.txt").exists()
    assert (lambda b: (b / "mine_only.txt").exists() and (b / "diff.txt").read_text() == "my version\n")(
        __import__("pathlib").Path(r["backup"]))
    assert sh("git", "status", "--porcelain", "--untracked-files=no", cwd=path) == ""
    sh("git", "pull", "-q", cwd=path)


def test_local_wins_without_rewriting_github(G, linked, tmp_path):
    pid, path, bare, _ = linked
    r = G.reconcile(pid, "local")
    assert r["commit"]
    assert (path / "diff.txt").read_text() == "my version\n" and (path / "gh_only.txt").exists()
    assert sh("git", "rev-parse", "HEAD~1", cwd=path) == sh("git", "rev-parse", "origin/main", cwd=path)
    sh("git", "push", "-q", cwd=path)                              # fast-forward: no force needed
    out = tmp_path / "check"
    sh("git", "clone", "-q", str(bare), str(out))
    assert (out / "mine_only.txt").exists() and (out / "diff.txt").read_text() == "my version\n"
    assert "initial" in sh("git", "log", "--format=%s", cwd=out)   # GitHub's history kept


def test_merge_needs_a_choice_per_different_file(P, G, linked):
    pid, path, *_ = linked
    e = err(P, G.reconcile, pid, "merge", {})
    assert e.details["missing"] == ["diff.txt"]
    assert (path / "diff.txt").read_text() == "my version\n"


def test_merge_pick_github_for_a_file(G, linked):
    pid, path, *_ = linked
    G.reconcile(pid, "merge", {"diff.txt": "github"})
    assert (path / "diff.txt").read_text() == "github version\n"
    assert (path / "mine_only.txt").exists() and (path / "gh_only.txt").exists()
    assert "mine_only.txt" in sh("git", "ls-files", cwd=path)


def test_merge_edit_then_finish(P, G, linked):
    pid, path, *_ = linked
    r = G.reconcile(pid, "merge", {"diff.txt": "edit"})
    assert r["status"] == "edit"
    text = (path / "diff.txt").read_text()
    assert "my version" in text and "github version" in text and "<<<<<<<" in text
    err(P, G.finish_reconcile, pid)                                 # markers still there
    (path / "diff.txt").write_text("both, edited\n")
    r = G.finish_reconcile(pid)
    assert r["commit"] and sh("git", "show", "HEAD:diff.txt", cwd=path) == "both, edited"


def test_status_reports_pending_link(G, linked):
    pid, *_ = linked
    assert G.status(pid)["pending_link"] is True


# ── 2. While working: sync ──────────────────────────────────────────────────

@pytest.fixture
def working(P, G, home, gh, tmp_path):
    """Project linked to gh and in sync, plus a second clone ("the other PC")."""
    url, bare = gh
    p = P.create("site")
    P.link_git(p["id"], url)
    path = home / "devpilot" / "projects" / "site"
    other = tmp_path / "otherpc"
    sh("git", "clone", "-q", str(bare), str(other))
    return p["id"], path, other


def push_from_other(other, name, content):
    (other / name).write_text(content)
    commit_all(other, f"other: {name}")
    sh("git", "push", "-q", cwd=other)


def test_status_ahead_behind(G, working):
    pid, path, other = working
    push_from_other(other, "o.txt", "o")
    (path / "m.txt").write_text("m")
    commit_all(path, "mine")
    (path / "wip.txt").write_text("w")
    s = G.status(pid)
    assert (s["ahead"], s["behind"], s["untracked"]) == (1, 1, 1)


def test_sync_needs_commit_message_when_dirty(P, G, working):
    pid, path, _ = working
    (path / "same.txt").write_text("changed\n")
    e = err(P, G.sync, pid)
    assert e.details["needs_commit"]


def test_sync_commit_rebase_push(G, working):
    pid, path, other = working
    push_from_other(other, "o.txt", "o")
    (path / "m.txt").write_text("m")
    r = G.sync(pid, commit_message="mon travail")
    assert r["status"] == "done"
    sh("git", "pull", "-q", cwd=other)
    assert (other / "m.txt").exists() and (path / "o.txt").exists()
    assert sh("git", "rev-list", "--merges", "--count", "HEAD", cwd=path) == "0"   # rebase: straight line


def test_sync_with_stash(G, working):
    pid, path, other = working
    push_from_other(other, "o.txt", "o")
    (path / "wip.txt").write_text("unfinished")
    r = G.sync(pid, stash=True)
    assert r["status"] == "done" and (path / "wip.txt").read_text() == "unfinished" and (path / "o.txt").exists()
    assert "wip.txt" in sh("git", "status", "--porcelain", cwd=path)          # still uncommitted


def _conflict(G, working):
    pid, path, other = working
    push_from_other(other, "diff.txt", "from the other PC\n")
    (path / "diff.txt").write_text("from this PC\n")
    r = G.sync(pid, commit_message="my edit")
    assert r["status"] == "conflict" and r["conflicts"] == ["diff.txt"]
    return pid, path, other


@pytest.mark.parametrize("choice,expected", [("local", "from this PC\n"), ("github", "from the other PC\n")])
def test_conflict_pick_a_side(G, working, choice, expected):
    pid, path, other = _conflict(G, working)
    G.resolve(pid, "diff.txt", choice)
    r = G.continue_sync(pid)
    assert r["status"] == "done" and (path / "diff.txt").read_text() == expected
    assert G.status(pid)["ahead"] == 0 and not G.status(pid)["rebasing"]


def test_conflict_edit(P, G, working):
    pid, path, other = _conflict(G, working)
    err(P, G.resolve, pid, "diff.txt", "edited")                    # markers still in the file
    (path / "diff.txt").write_text("merged by hand\n")
    G.resolve(pid, "diff.txt", "edited")
    assert G.continue_sync(pid)["status"] == "done"
    sh("git", "pull", "-q", cwd=other)
    assert (other / "diff.txt").read_text() == "merged by hand\n"


def test_conflict_abort_puts_everything_back(G, working):
    pid, path, other = working
    before = sh("git", "rev-parse", "HEAD", cwd=path)
    pid, path, other = _conflict(G, working)
    mine = sh("git", "rev-parse", "HEAD@{1}", cwd=path)             # my commit, before the rebase
    G.abort(pid)
    assert not G.status(pid, fetch=False)["rebasing"]
    assert (path / "diff.txt").read_text() == "from this PC\n"
    assert sh("git", "log", "-1", "--format=%s", cwd=path) == "my edit"
    assert sh("git", "rev-parse", "HEAD~1", cwd=path) == before


def test_multi_repo_sync_targets_a_repo(P, G, home, gh):
    url, bare = gh
    root = home / "devpilot" / "projects" / "md"
    root.mkdir(parents=True)
    sh("git", "clone", "-q", url, "md_console", cwd=root)
    p = P.register("md", root)
    assert G.status(p["id"], "md_console")["branch"] == "main"
    err(P, G.status, p["id"], "../..")
