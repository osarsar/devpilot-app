"""DevPilot — GitHub API + Git CLI operations."""

import subprocess
import os
import json
import requests
from pathlib import Path

GITHUB_API = "https://api.github.com"


def _run_git(cmd, cwd=None, timeout=60):
    """Run a git command and return (success, output)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        output = r.stdout.strip()
        if r.returncode != 0:
            err = r.stderr.strip()
            return False, err or output or f"Exit code {r.returncode}"
        return True, output
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def _github_headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "DevPilot",
    }


# ═══════════════════════════════════════════════════════════════════════════
# GITHUB API
# ═══════════════════════════════════════════════════════════════════════════

def github_test_connection(token):
    """Test GitHub connection. Returns user info or error."""
    try:
        r = requests.get(f"{GITHUB_API}/user", headers=_github_headers(token), timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "connected": True,
                "login": data.get("login", ""),
                "name": data.get("name", ""),
                "avatar_url": data.get("avatar_url", ""),
                "public_repos": data.get("public_repos", 0),
                "private_repos": data.get("total_private_repos", 0),
            }
        return {"connected": False, "message": r.json().get("message", f"HTTP {r.status_code}")}
    except requests.RequestException as e:
        return {"connected": False, "message": str(e)}


def github_list_repos(token, page=1, per_page=50, sort="updated", visibility=None):
    """List authenticated user's repos."""
    try:
        params = {"page": page, "per_page": per_page, "sort": sort}
        if visibility:
            params["visibility"] = visibility
        r = requests.get(f"{GITHUB_API}/user/repos", headers=_github_headers(token), params=params, timeout=15)
        if r.status_code != 200:
            return []
        repos = r.json()
        if not isinstance(repos, list):
            return []
        return [{
            "name": repo.get("name", ""),
            "full_name": repo.get("full_name", ""),
            "clone_url": repo.get("clone_url", ""),
            "ssh_url": repo.get("ssh_url", ""),
            "html_url": repo.get("html_url", ""),
            "private": repo.get("private", False),
            "language": repo.get("language", ""),
            "description": repo.get("description", "") or "",
            "default_branch": repo.get("default_branch", "main"),
            "updated_at": repo.get("updated_at", "")[:10] if repo.get("updated_at") else "",
            "size": repo.get("size", 0),
            "open_issues": repo.get("open_issues_count", 0),
        } for repo in repos]
    except requests.RequestException:
        return []


def github_get_repo(token, owner, repo):
    """Get details of a specific repo."""
    try:
        r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=_github_headers(token), timeout=10)
        if r.status_code == 200:
            return {"success": True, "repo": r.json()}
        return {"success": False, "message": r.json().get("message", f"HTTP {r.status_code}")}
    except requests.RequestException as e:
        return {"success": False, "message": str(e)}


def github_list_branches(token, owner, repo):
    """List branches of a repo."""
    try:
        r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/branches",
                         headers=_github_headers(token), params={"per_page": 100}, timeout=10)
        if r.status_code != 200:
            return []
        return [{
            "name": b.get("name", ""),
            "sha": b.get("commit", {}).get("sha", "")[:8],
            "protected": b.get("protected", False),
        } for b in r.json()]
    except requests.RequestException:
        return []


def github_list_commits(token, owner, repo, branch="main", limit=20):
    """List recent commits of a repo."""
    try:
        r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/commits",
                         headers=_github_headers(token),
                         params={"sha": branch, "per_page": limit}, timeout=10)
        if r.status_code != 200:
            return []
        return [{
            "sha": c.get("sha", "")[:8],
            "sha_full": c.get("sha", ""),
            "message": c.get("commit", {}).get("message", "").split("\n")[0],
            "author": c.get("commit", {}).get("author", {}).get("name", ""),
            "date": c.get("commit", {}).get("author", {}).get("date", "")[:10],
            "url": c.get("html_url", ""),
        } for c in r.json()]
    except requests.RequestException:
        return []


def github_create_repo(token, name, private=True, description=""):
    """Create a new repo on GitHub."""
    try:
        r = requests.post(f"{GITHUB_API}/user/repos",
                          headers=_github_headers(token),
                          json={"name": name, "private": private, "description": description},
                          timeout=15)
        if r.status_code in (200, 201):
            data = r.json()
            return {
                "success": True,
                "full_name": data.get("full_name", ""),
                "clone_url": data.get("clone_url", ""),
                "ssh_url": data.get("ssh_url", ""),
                "html_url": data.get("html_url", ""),
            }
        return {"success": False, "message": r.json().get("message", f"HTTP {r.status_code}")}
    except requests.RequestException as e:
        return {"success": False, "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# GIT CLI OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

def git_status_detailed(project_path):
    """Get detailed git status for a project directory."""
    if not project_path or not Path(project_path).exists():
        return None
    git_dir = Path(project_path) / ".git"
    if not git_dir.exists():
        return None

    ok, branch = _run_git(f"git branch --show-current", cwd=project_path)
    _, last_commit = _run_git(f"git log -1 --format='%cr'", cwd=project_path)
    _, last_commit_msg = _run_git(f"git log -1 --format='%s'", cwd=project_path)
    _, last_commit_sha = _run_git(f"git log -1 --format='%h'", cwd=project_path)
    _, status_raw = _run_git(f"git status --porcelain", cwd=project_path)
    _, remote = _run_git(f"git remote get-url origin", cwd=project_path)

    untracked = 0
    modified = 0
    staged = 0
    files_modified = []
    files_staged = []
    files_untracked = []

    for line in (status_raw or "").splitlines():
        if not line or len(line) < 3:
            continue
        fname = line[3:].strip()
        if line.startswith("??"):
            untracked += 1
            files_untracked.append(fname)
        elif line[0] in ("M", "A", "D", "R"):
            staged += 1
            files_staged.append(fname)
        elif line[1] in ("M", "D"):
            modified += 1
            files_modified.append(fname)

    # Ahead/behind remote
    ahead = 0
    behind = 0
    if branch:
        _, ab = _run_git(f"git rev-list --left-right --count origin/{branch}...HEAD", cwd=project_path)
        if ab:
            parts = ab.split()
            if len(parts) == 2:
                try:
                    behind = int(parts[0])
                    ahead = int(parts[1])
                except ValueError:
                    pass

    is_clean = (untracked == 0 and modified == 0 and staged == 0)

    return {
        "branch": branch or "—",
        "last_commit": last_commit or "—",
        "last_commit_msg": last_commit_msg or "",
        "last_commit_sha": last_commit_sha or "",
        "untracked": untracked,
        "modified": modified,
        "staged": staged,
        "ahead": ahead,
        "behind": behind,
        "is_clean": is_clean,
        "remote": remote or "",
        "files_modified": files_modified[:20],
        "files_staged": files_staged[:20],
        "files_untracked": files_untracked[:20],
    }


def git_clone(url, dest, token=""):
    """Clone a git repo. Uses token for private HTTPS repos."""
    if token and "https://" in url:
        auth_url = url.replace("https://", f"https://x-access-token:{token}@")
        ok, output = _run_git(f"git clone '{auth_url}' '{dest}'", timeout=120)
    else:
        ok, output = _run_git(f"git clone '{url}' '{dest}'", timeout=120)
    return ok, output


def git_pull(project_path, remote="origin", branch=""):
    """Pull latest changes."""
    cmd = f"git pull {remote}"
    if branch:
        cmd += f" {branch}"
    return _run_git(cmd, cwd=project_path, timeout=60)


def git_push(project_path, remote="origin", branch="", set_upstream=False):
    """Push changes to remote."""
    if not branch:
        ok, branch = _run_git("git branch --show-current", cwd=project_path)
        if not ok or not branch:
            return False, "Cannot determine current branch"
    cmd = f"git push {remote} {branch}"
    if set_upstream:
        cmd = f"git push -u {remote} {branch}"
    return _run_git(cmd, cwd=project_path, timeout=60)


def git_commit(project_path, message, add_all=False, files=None):
    """Create a commit. Optionally stage files first."""
    if add_all:
        ok, out = _run_git("git add -A", cwd=project_path)
        if not ok:
            return False, f"git add failed: {out}"
    elif files:
        for f in files:
            _run_git(f"git add '{f}'", cwd=project_path)

    # Check if there's anything to commit
    ok, status = _run_git("git status --porcelain", cwd=project_path)
    if ok and not status:
        return False, "Nothing to commit"

    return _run_git(f"git commit -m '{message}'", cwd=project_path)


def git_checkout(project_path, branch, create=False):
    """Switch or create a branch."""
    if create:
        return _run_git(f"git checkout -b '{branch}'", cwd=project_path)
    return _run_git(f"git checkout '{branch}'", cwd=project_path)


def git_log(project_path, limit=20):
    """Get commit history."""
    ok, output = _run_git(
        f"git log --format='%h|%s|%an|%cr|%aI' -n {limit}",
        cwd=project_path
    )
    if not ok or not output:
        return []
    commits = []
    for line in output.splitlines():
        parts = line.split("|", 4)
        if len(parts) >= 4:
            commits.append({
                "sha": parts[0],
                "message": parts[1],
                "author": parts[2],
                "relative_date": parts[3],
                "date": parts[4][:10] if len(parts) > 4 else "",
            })
    return commits


def git_stash(project_path, pop=False):
    """Stash or pop stash."""
    if pop:
        return _run_git("git stash pop", cwd=project_path)
    return _run_git("git stash", cwd=project_path)


def git_diff_stat(project_path):
    """Get diff stats (files changed, insertions, deletions)."""
    ok, output = _run_git("git diff --stat", cwd=project_path)
    return output if ok else ""


def git_init(project_path):
    """Initialize a new git repo."""
    return _run_git("git init", cwd=project_path)


def git_remote_add(project_path, url, name="origin"):
    """Add a remote."""
    return _run_git(f"git remote add {name} '{url}'", cwd=project_path)


def git_remote_set_url(project_path, url, name="origin"):
    """Update a remote URL."""
    return _run_git(f"git remote set-url {name} '{url}'", cwd=project_path)
