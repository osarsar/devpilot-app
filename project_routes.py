"""DevPilot — project lifecycle routes. All the logic is in projects.py; this
file only translates HTTP to it (and runs long clones as background jobs)."""

import threading
import time
import uuid

from flask import Blueprint, jsonify, request

import db
import projects as P

projects_bp = Blueprint("projects", __name__)


@projects_bp.errorhandler(P.ProjectError)
def _project_error(e):
    return jsonify({"success": False, "message": e.message, "details": e.details}), 409


def _body():
    return request.get_json(silent=True) or {}


def _flag(v):
    return v in (True, 1, "1", "true", "yes", "on")


def _ok(**kw):
    return jsonify({"success": True, **kw})


def _github_token(url):
    return db.get_setting("github_token", "") if url.startswith("https://github.com/") else ""


# ── Background jobs (clone) ─────────────────────────────────────────────────

_jobs = {}
_jobs_lock = threading.Lock()


def _start_job(kind, fn):
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "kind": kind, "status": "running", "log": [], "result": None,
           "error": None, "details": None, "started": time.time()}
    with _jobs_lock:
        _jobs[jid] = job
        for old in [k for k, j in _jobs.items() if time.time() - j["started"] > 3600]:
            _jobs.pop(old, None)

    def log(line):
        job["log"].append(line)
        del job["log"][:-200]

    def run():
        try:
            job["result"] = fn(log)
            job["status"] = "done"
        except P.ProjectError as e:
            job.update(status="error", error=e.message, details=e.details)
        except Exception as e:  # unexpected: keep the message for the UI
            job.update(status="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return job


@projects_bp.route("/api/jobs/<jid>")
def api_job(jid):
    job = _jobs.get(jid)
    if not job:
        return jsonify({"success": False, "message": "Tache inconnue"}), 404
    return jsonify(job)


# ── Add a project ───────────────────────────────────────────────────────────

@projects_bp.route("/api/projects", methods=["POST"])
def api_create():
    b = _body()
    path = (b.get("path") or "").strip()
    p = P.create(b.get("name"), path=path or None, custom_location=bool(path),
                 git_init=_flag(b.get("git_init")), description=b.get("description", ""))
    if b.get("color"):
        db.update_project(p["id"], color=b["color"])
        P.write_space(p["id"])
    if (b.get("pattern") or "").strip():
        db.add_rule(pattern=b["pattern"].strip(), project_id=p["id"], resource_type="any")
    return _ok(id=p["id"], path=p["path"], project=p)


@projects_bp.route("/api/projects/clone", methods=["POST"])
@projects_bp.route("/api/github/clone", methods=["POST"])
def api_clone():
    """Starts the clone and returns a job id: GET /api/jobs/<id> for progress."""
    b = _body()
    url = (b.get("url") or "").strip()
    name = (b.get("name") or "").strip() or P.name_from_url(url)
    path = (b.get("path") or "").strip()
    if not path and (b.get("target_dir") or "").strip():       # legacy: parent folder
        path = str(P.resolve(b["target_dir"]) / name)
    custom = bool(path) and not P.is_managed(path)
    # validate now, so obvious mistakes answer immediately
    P._prepare_target(name, path or None, custom)
    job = _start_job("clone", lambda log: P.clone(
        url, name=name, path=path or None, custom_location=custom,
        branch=(b.get("branch") or "").strip() or None, token=_github_token(url), on_output=log))
    return _ok(job_id=job["id"], name=name)


@projects_bp.route("/api/projects/adopt", methods=["POST"])
@projects_bp.route("/api/projects/from-path", methods=["POST"])
def api_adopt():
    """Existing folder. mode: move (default, into projects/) | copy | link (keep in place)."""
    b = _body()
    dest = (b.get("dest") or "").strip()
    p = P.adopt(b.get("path", ""), mode=b.get("mode", "move"), name=(b.get("name") or "").strip() or None,
                dest=dest or None, custom_location=bool(dest))
    return _ok(id=p["id"], name=p["name"], path=p["path"], project=p)


@projects_bp.route("/api/projects/unregistered")
def api_unregistered():
    return jsonify(P.unregistered_folders())


@projects_bp.route("/api/projects/import/prepare", methods=["POST"])
def api_import_prepare():
    """Folder for the import terminal. It may already hold files (an import in
    progress); it only must not belong to another project."""
    b = _body()
    name = P.validate_name(b.get("name"))
    raw = (b.get("path") or "").strip()
    path = P.resolve(raw) if raw else P.default_path(name)
    P._check_location(path, custom_location=bool(raw))
    existing = P.find_by_path(path)
    if not existing:
        other = P.find_by_name(name)
        if other:
            raise P.ProjectError(f'Un projet "{name}" existe deja ({other.get("path") or "sans dossier"})')
        over = P.overlapping(path)
        if over:
            raise P.ProjectError(f'{path} chevauche le projet "{over[0]["name"]}"')
    path.mkdir(parents=True, exist_ok=True)
    return _ok(name=name, path=str(path), sid=f"import-{name}",
               project_id=existing["id"] if existing else None,
               follow_vars=db.get_setting("import_cwd_vars", "MD_ROOT"))


@projects_bp.route("/api/projects/import/finish", methods=["POST"])
def api_import_finish():
    b = _body()
    name = P.validate_name(b.get("name"))
    raw = (b.get("path") or "").strip()
    path = P.resolve(raw) if raw else P.default_path(name)
    if not path.is_dir() or not any(path.iterdir()):
        raise P.ProjectError(f"{path} est vide — rien n'a ete cree ici. "
                             "Si tu as lance la commande avec un autre chemin, indique-le.")
    existing = P.find_by_path(path)
    if existing:
        repos = P.find_repos(path)
        db.update_project(existing["id"], git_remote=repos[0]["remote"] if repos else "")
        P.write_space(existing["id"])
        return _ok(id=existing["id"], name=existing["name"], path=str(path), repos=repos, created=False)
    p = P.register(name, path, custom_location=True)
    return _ok(id=p["id"], name=p["name"], path=p["path"], repos=p["repos"], created=True)


# ── Change a project ────────────────────────────────────────────────────────

@projects_bp.route("/api/projects/<int:pid>", methods=["PUT"])
def api_update(pid):
    """Metadata. Folder changes go through move / relocate / duplicate (the
    legacy {path, path_mode} form is mapped onto them)."""
    b = _body()
    P.get(pid)
    if b.get("path"):
        mode = b.get("path_mode", "move")
        if mode == "copy":
            p = P.duplicate(pid, P.resolve(b["path"]).name, dest=b["path"], custom_location=True)
        elif mode == "link":
            p = P.relocate(pid, b["path"], custom_location=True)
        else:
            p = P.move(pid, b["path"], custom_location=True)
        return _ok(project=p)
    if b.get("name") and b["name"] != P.get(pid)["name"]:
        P.rename(pid, b["name"])
    meta = {k: b[k] for k in ("color", "icon", "description", "status") if k in b}
    if meta:
        db.update_project(pid, **meta)
    P.write_space(pid)
    return _ok(project=db.get_project(pid))


@projects_bp.route("/api/projects/<int:pid>/rename", methods=["POST"])
def api_rename(pid):
    return _ok(project=P.rename(pid, _body().get("name")))


@projects_bp.route("/api/projects/<int:pid>/move", methods=["POST"])
def api_move(pid):
    b = _body()
    return _ok(project=P.move(pid, b.get("dest", ""), custom_location=_flag(b.get("custom_location")),
                              leave_symlink=_flag(b.get("leave_symlink"))))


@projects_bp.route("/api/projects/<int:pid>/relocate", methods=["POST"])
def api_relocate(pid):
    b = _body()
    return _ok(project=P.relocate(pid, b.get("path", ""), custom_location=True))


@projects_bp.route("/api/projects/<int:pid>/duplicate", methods=["POST"])
def api_duplicate(pid):
    b = _body()
    dest = (b.get("dest") or "").strip()
    return _ok(project=P.duplicate(pid, b.get("name"), dest=dest or None, custom_location=bool(dest)))


@projects_bp.route("/api/projects/<int:pid>/link-git", methods=["POST"])
def api_link_git(pid):
    b = _body()
    url = (b.get("url") or "").strip()
    r = P.link_git(pid, url, token=db.get_setting("github_token", ""), replace=_flag(b.get("replace")))
    return _ok(**r, path=P.get(pid)["path"])


@projects_bp.route("/api/projects/<int:pid>/inspect")
def api_inspect(pid):
    return jsonify(P.removal_report(pid))


# ── Remove ──────────────────────────────────────────────────────────────────

@projects_bp.route("/api/projects/<int:pid>", methods=["DELETE"])
def api_delete(pid):
    """Unregister. ?delete_files=1&confirm=<name> also sends the folder to the trash."""
    r = P.remove(pid, delete_files=_flag(request.args.get("delete_files")),
                 confirm_name=request.args.get("confirm"))
    return _ok(**r)


# ── Folder vs GitHub (gitsync.py) ───────────────────────────────────────────
import gitsync as G  # noqa: E402  (imports projects)


def _repo_arg():
    return (request.args.get("repo") or _body().get("repo") or "").strip() or None


@projects_bp.route("/api/projects/<int:pid>/git/compare")
def api_git_compare(pid):
    return jsonify(G.compare(pid, _repo_arg()))


@projects_bp.route("/api/projects/<int:pid>/git/reconcile", methods=["POST"])
def api_git_reconcile(pid):
    b = _body()
    return _ok(**G.reconcile(pid, b.get("mode"), b.get("choices"), _repo_arg(), b.get("message")))


@projects_bp.route("/api/projects/<int:pid>/git/reconcile/finish", methods=["POST"])
def api_git_reconcile_finish(pid):
    return _ok(**G.finish_reconcile(pid, _repo_arg(), _body().get("message")))


@projects_bp.route("/api/projects/<int:pid>/git/sync-status")
def api_git_sync_status(pid):
    return jsonify(G.status(pid, _repo_arg(), fetch=request.args.get("fetch", "1") != "0"))


@projects_bp.route("/api/projects/<int:pid>/git/sync", methods=["POST"])
def api_git_sync(pid):
    b = _body()
    return _ok(**G.sync(pid, _repo_arg(), commit_message=b.get("commit_message"), stash=_flag(b.get("stash"))))


@projects_bp.route("/api/projects/<int:pid>/git/resolve", methods=["POST"])
def api_git_resolve(pid):
    b = _body()
    return _ok(**G.resolve(pid, b.get("file", ""), b.get("choice", ""), _repo_arg()))


@projects_bp.route("/api/projects/<int:pid>/git/continue", methods=["POST"])
def api_git_continue(pid):
    return _ok(**G.continue_sync(pid, _repo_arg()))


@projects_bp.route("/api/projects/<int:pid>/git/abort", methods=["POST"])
def api_git_abort(pid):
    return _ok(**G.abort(pid, _repo_arg()))


# ── Multi-repo project linked to a GitHub organisation (ghorg.py) ────────────
import ghorg  # noqa: E402


@projects_bp.route("/api/projects/<int:pid>/github/org", methods=["POST"])
def api_github_org(pid):
    return _ok(**ghorg.link_org(pid, _body().get("org", "")))


@projects_bp.route("/api/projects/<int:pid>/github/overview")
def api_github_overview(pid):
    return jsonify(ghorg.overview(pid, fetch=request.args.get("fetch", "1") != "0"))


@projects_bp.route("/api/projects/<int:pid>/github/clone", methods=["POST"])
def api_github_clone_repo(pid):
    b = _body()
    return _ok(**ghorg.clone_repo(pid, b.get("name", ""), (b.get("dest") or "").strip() or None))


@projects_bp.route("/api/projects/<int:pid>/github/pull-all", methods=["POST"])
def api_github_pull_all(pid):
    return _ok(**ghorg.pull_all(pid))


@projects_bp.route("/api/projects/<int:pid>/github/role", methods=["POST"])
def api_github_role(pid):
    b = _body()
    return _ok(**ghorg.set_role(pid, b.get("name", ""), b.get("role", "")))


@projects_bp.route("/api/projects/<int:pid>/github/detect", methods=["GET", "POST"])
def api_github_detect(pid):
    """GET: what is cloned in the folder now (live hint). POST: record the link."""
    res = P.detect_link(pid, record=request.method == "POST")
    return _ok(**res, gh_user=P.github_user())


# ── Servers of a project (servers.py) ───────────────────────────────────────
import servers as S  # noqa: E402


@projects_bp.route("/api/servers/providers")
def api_server_providers():
    return jsonify({"providers": S.PROVIDERS, "roles": S.ROLES})


@projects_bp.route("/api/servers/ssh-hosts")
def api_server_ssh_hosts():
    return jsonify(S.ssh_config_hosts())


@projects_bp.route("/api/servers/ssh-keys")
def api_server_ssh_keys():
    return jsonify(S.ssh_keys())


@projects_bp.route("/api/servers/public-key")
def api_server_public_key():
    return _ok(public=S.public_key(request.args.get("path", "")))


@projects_bp.route("/api/servers/test", methods=["POST"])
def api_server_test():
    return _ok(**S.test_connection(S._clean(_body())))


@projects_bp.route("/api/projects/<int:pid>/servers")
def api_project_servers(pid):
    return jsonify(S.list_servers(pid))


@projects_bp.route("/api/projects/<int:pid>/servers", methods=["POST"])
def api_project_server_add(pid):
    b = _body()
    return _ok(server=S.add_server(pid, b, test=not _flag(b.get("skip_test"))))


@projects_bp.route("/api/projects/<int:pid>/servers/<sid>", methods=["PUT"])
def api_project_server_update(pid, sid):
    b = _body()
    return _ok(server=S.update_server(pid, sid, b, test=not _flag(b.get("skip_test"))))


@projects_bp.route("/api/projects/<int:pid>/servers/<sid>", methods=["DELETE"])
def api_project_server_remove(pid, sid):
    return _ok(**S.remove_server(pid, sid))


@projects_bp.route("/api/projects/<int:pid>/servers/<sid>/test", methods=["POST"])
def api_project_server_retest(pid, sid):
    return _ok(server=S.retest(pid, sid))


@projects_bp.route("/api/projects/<int:pid>/servers/<sid>/restore", methods=["POST"])
def api_project_server_restore(pid, sid):
    return _ok(server=S.restore_server(pid, sid, _body().get("index", 0)))


@projects_bp.route("/api/projects/<int:pid>/servers/generate-key", methods=["POST"])
def api_project_server_genkey(pid):
    return _ok(**S.generate_key(pid, _body().get("label", "")))


# ── « Supprimer tout » (purge.py) ───────────────────────────────────────────
import purge  # noqa: E402


@projects_bp.route("/api/projects/<int:pid>/purge-plan")
def api_purge_plan(pid):
    return jsonify(purge.plan(pid))


@projects_bp.route("/api/projects/<int:pid>/purge", methods=["POST"])
def api_purge(pid):
    b = _body()
    return _ok(**purge.execute(pid, b.get("confirm"), b.get("choices")))


# ── Project model: profile, needs, state of each need (model.py) ────────────
import model as M  # noqa: E402


@projects_bp.route("/api/model/profiles")
def api_model_profiles():
    # jsonify sorts keys: the orders are given explicitly
    return jsonify({"profiles": M.PROFILES, "needs": M.NEEDS,
                    "profiles_order": list(M.PROFILES), "needs_order": list(M.NEEDS)})


@projects_bp.route("/api/projects/<int:pid>/model")
def api_project_model(pid):
    return jsonify(M.get_model(pid))


@projects_bp.route("/api/projects/<int:pid>/model", methods=["PUT"])
def api_project_model_set(pid):
    b = _body()
    return _ok(model=M.set_model(pid, profile=b.get("profile"), needs=b.get("needs")))


@projects_bp.route("/api/projects/<int:pid>/needs")
def api_project_needs(pid):
    return jsonify(M.connections_status(pid))


# ── Client-site connections: domain, email (connections.py) ─────────────────
import connections as C  # noqa: E402


@projects_bp.route("/api/projects/<int:pid>/domain")
def api_domain(pid):
    return jsonify(C.domain_state(pid))


@projects_bp.route("/api/projects/<int:pid>/domain", methods=["POST"])
def api_domain_set(pid):
    C.set_domain(pid, _body())
    return _ok(**C.domain_state(pid))


@projects_bp.route("/api/projects/<int:pid>/domain/verify", methods=["POST"])
def api_domain_verify(pid):
    return _ok(result=C.verify_domain(pid), **C.domain_state(pid))


@projects_bp.route("/api/projects/<int:pid>/domain/site", methods=["POST"])
def api_domain_site(pid):
    return _ok(result=C.verify_site(pid), **C.domain_state(pid))


@projects_bp.route("/api/projects/<int:pid>/email")
def api_email(pid):
    return jsonify(C.email_state(pid))


@projects_bp.route("/api/projects/<int:pid>/email", methods=["POST"])
def api_email_set(pid):
    C.set_email(pid, _body())
    return _ok(**C.email_state(pid))


@projects_bp.route("/api/projects/<int:pid>/email/verify", methods=["POST"])
def api_email_verify(pid):
    return _ok(result=C.verify_email(pid), **C.email_state(pid))


@projects_bp.route("/api/projects/<int:pid>/connections/<key>", methods=["DELETE"])
def api_connection_delete(pid, key):
    P.get(pid)
    db.delete_project_component(pid, key)
    C.mirror(pid)
    return _ok(removed=key)


# ── The project's own controller / console (controller.py) ──────────────────
import controller as CT  # noqa: E402


@projects_bp.route("/api/projects/<int:pid>/controller")
def api_controller(pid):
    ctrl = CT.get(pid)
    return jsonify({"controller": ctrl, "candidates": CT.detect(P.get(pid)["path"]) if P.get(pid).get("path") else [],
                    "log": CT.log_tail(pid, 20) if ctrl else []})


@projects_bp.route("/api/projects/<int:pid>/controller", methods=["POST"])
def api_controller_declare(pid):
    return _ok(controller=CT.declare(pid, _body()))


@projects_bp.route("/api/projects/<int:pid>/controller", methods=["DELETE"])
def api_controller_clear(pid):
    return _ok(controller=CT.clear(pid))


@projects_bp.route("/api/projects/<int:pid>/controller/start", methods=["POST"])
def api_controller_start(pid):
    if _flag(_body().get("fresh")):
        st, restarted = CT.open_fresh(pid)
        return _ok(status=st, controller=CT.get(pid), restarted=restarted)
    return _ok(status=CT.start(pid), controller=CT.get(pid))


@projects_bp.route("/api/projects/<int:pid>/controller/stop", methods=["POST"])
def api_controller_stop(pid):
    return _ok(status=CT.stop(pid), controller=CT.get(pid))


@projects_bp.route("/api/projects/<int:pid>/controller/log")
def api_controller_log(pid):
    return jsonify(CT.log_tail(pid, int(request.args.get("n", 80))))


# ── Phases / roadmap of a project (phases.py) ───────────────────────────────
import phases as PH  # noqa: E402


@projects_bp.route("/api/projects/<int:pid>/roadmap")
def api_roadmap(pid):
    return jsonify(PH.roadmap(pid))


@projects_bp.route("/api/projects/<int:pid>/roadmap/skip", methods=["POST"])
def api_roadmap_skip(pid):
    b = _body()
    return _ok(roadmap=PH.skip(pid, b.get("key", ""), later=b.get("later", True) not in (False, "false", 0, "0")))


@projects_bp.route("/api/projects/<int:pid>/roadmap/mark", methods=["POST"])
def api_roadmap_mark(pid):
    b = _body()
    return _ok(roadmap=PH.mark(pid, b.get("key", ""), done=b.get("done", True) not in (False, "false", 0, "0")))


@projects_bp.route("/api/projects/<int:pid>/roadmap/custom", methods=["POST"])
def api_roadmap_custom(pid):
    b = _body()
    return _ok(roadmap=PH.add_custom(pid, b.get("phase", ""), b.get("label", ""), b.get("hint", "")))


@projects_bp.route("/api/projects/<int:pid>/roadmap/custom/<key>", methods=["DELETE"])
def api_roadmap_custom_del(pid, key):
    return _ok(roadmap=PH.remove_custom(pid, key))


@projects_bp.route("/api/phases")
def api_phases():
    return jsonify({"phases": PH.PHASES, "steps": PH.STEPS})


# ── What the project runs: ports, close (ports.py) ──────────────────────────
import ports as PO  # noqa: E402


@projects_bp.route("/api/projects/<int:pid>/ports")
def api_project_ports(pid):
    return jsonify(PO.project_ports(pid))


@projects_bp.route("/api/projects/<int:pid>/ports/<int:port>/stop", methods=["POST"])
def api_project_port_stop(pid, port):
    return _ok(**PO.stop_port(pid, port))


@projects_bp.route("/api/projects/<int:pid>/close", methods=["POST"])
def api_project_close(pid):
    b = _body()
    return _ok(**PO.close_project(pid, stop_console=b.get("console", True) not in (False, "false", 0),
                                   stop_terminals=b.get("terminals", True) not in (False, "false", 0),
                                   stop_folder=b.get("folder", True) not in (False, "false", 0)))


@projects_bp.route("/api/projects/<int:pid>/controller/restart", methods=["POST"])
def api_controller_restart(pid):
    return _ok(status=CT.restart(pid), controller=CT.get(pid))


# ── Access to a server with its password (sshaccess.py) ─────────────────────
import sshaccess as SA  # noqa: E402


@projects_bp.route("/api/projects/<int:pid>/servers/<sid>/access", methods=["POST"])
def api_server_access(pid, sid):
    """{current, new?, also_root?, allow_password_login?, no_expiry?} — passwords are never stored."""
    b = _body()
    r = SA.setup_access(pid, sid, b.get("current") or "", new=(b.get("new") or None),
                        also_root=_flag(b.get("also_root")), allow_password_login=b.get("allow_password_login", True) not in (False, "false", 0),
                        no_expiry=b.get("no_expiry", True) not in (False, "false", 0))
    return _ok(**r)


# ── développer : dépôts, branches, outils ───────────────────────────────────
import dev as DV  # noqa: E402


@projects_bp.route("/api/projects/<int:pid>/dev/repos")
def api_dev_repos(pid):
    return _ok(**DV.repos(pid, fetch=_flag(request.args.get("fetch"))), tools=DV.tools())


@projects_bp.route("/api/projects/<int:pid>/dev/previews")
def api_dev_previews(pid):
    import preview
    return _ok(previews=preview.previews(pid))


@projects_bp.route("/api/projects/<int:pid>/dev/maj")
def api_dev_plan_maj(pid):
    return _ok(**DV.plan_maj(pid))


@projects_bp.route("/api/projects/<int:pid>/dev/maj", methods=["POST"])
def api_dev_appliquer_maj(pid):
    return _ok(**DV.appliquer_maj(pid, _body().get("choix") or {}))


@projects_bp.route("/api/projects/<int:pid>/dev/prs")
def api_dev_prs(pid):
    return _ok(prs=DV.prs(pid))


@projects_bp.route("/api/projects/<int:pid>/dev/branches")
def api_dev_branches(pid):
    return _ok(**DV.branches(pid, request.args.get("repo") or "."))


@projects_bp.route("/api/projects/<int:pid>/dev/switch", methods=["POST"])
def api_dev_switch(pid):
    b = _body()
    return _ok(**DV.switch(pid, b.get("repo") or ".", b.get("branch") or "", stash=_flag(b.get("stash"))))


@projects_bp.route("/api/projects/<int:pid>/dev/new-branch", methods=["POST"])
def api_dev_new_branch(pid):
    b = _body()
    return _ok(**DV.new_branch(pid, b.get("repo") or ".", b.get("name") or "", stash=_flag(b.get("stash")),
                               carry=_flag(b.get("carry"))))


@projects_bp.route("/api/projects/<int:pid>/dev/update-main", methods=["POST"])
def api_dev_update_main(pid):
    return _ok(**DV.update_main(pid, _body().get("repo") or "."))


@projects_bp.route("/api/projects/<int:pid>/dev/open", methods=["POST"])
def api_dev_open(pid):
    """{repo, tool: editor | external | external-claude} — VS Code or an external window IN the repo."""
    b = _body()
    repo, tool = b.get("repo") or ".", b.get("tool")
    if tool == "editor":
        return _ok(**DV.open_editor(pid, repo))
    if tool in ("external", "external-claude"):
        return _ok(**DV.open_external(pid, repo, claude=tool == "external-claude"))
    raise P.ProjectError("Outil inconnu")
