"""DevPilot — access to a server with its password (OVH and others).

The provider sends a temporary password (it may be expired: it must be changed
at first login). With it DevPilot:
  1. connects, and if the password is expired changes it right away (interactive
     `passwd` in a pseudo-terminal: current / new / new)
  2. installs this PC's SSH key and checks the key works
  3. sets YOUR password (when you choose one), removes its expiry, optionally root's too
  4. makes sure the server still accepts password logins, so that a NEW PC can
     add its own key with your password later (« Ajouter la cle de ce PC »)
Passwords are never stored, logged or put on a command line: they go through
SSH to the server's prompts / stdin only.
"""

import os
import shlex
import re
import socket
import time
from pathlib import Path

import paramiko

import servers as S
from projects import ProjectError

SSH_DIR = Path.home() / ".ssh"
MIN_LEN = 12

_EXPIRED = ("password has expired", "password change required", "must change your password",
            "required to change your password", "password expired")
_OK = ("updated successfully", "password changed", "all authentication tokens updated", "password updated",
       "log in again")
_FAIL = ("bad password", "too short", "too simple", "do not match", "don't match", "mismatch",
         "authentication token manipulation error", "passwd: failure", "unchanged", "too similar",
         "is a palindrome", "dictionary", "have exhausted", "sorry, passwords", "password unchanged")


# ── connection ──────────────────────────────────────────────────────────────

def _known_hosts():
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    kh = SSH_DIR / "known_hosts"
    if not kh.exists():
        kh.touch(mode=0o600)
    return str(kh)


def connect(host, port, user, password=None, key_path=None, timeout=15):
    """paramiko client. The server's identity is checked against ~/.ssh/known_hosts
    (a new host is recorded, a CHANGED one is refused — same rule as ssh accept-new)."""
    c = paramiko.SSHClient()
    c.load_host_keys(_known_hosts())
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        sock = socket.create_connection((host, int(port)), timeout=8)
    except socket.timeout:
        raise ProjectError(f"{host}:{port} ne repond pas : verifie l'adresse et que le serveur est demarre")
    except ConnectionRefusedError:
        raise ProjectError(f"Connexion refusee sur {host}:{port} : SSH n'ecoute pas sur ce port")
    except OSError as e:
        raise ProjectError(f"Impossible de joindre {host}:{port} : {e}")
    try:
        kw = dict(hostname=host, port=int(port), username=user, timeout=timeout, banner_timeout=timeout,
                  auth_timeout=timeout, sock=sock, allow_agent=False, look_for_keys=False)
        if password is not None:
            kw["password"] = password
        if key_path:
            kw["key_filename"] = key_path
        c.connect(**kw)
    except paramiko.BadHostKeyException:
        raise ProjectError(f"L'identite du serveur {host} a change depuis la derniere connexion. Si tu l'as "
                           f"reinstalle c'est normal : ssh-keygen -R {host} puis recommence. Sinon, ne te connecte pas.")
    except paramiko.AuthenticationException:
        what = "Mot de passe incorrect" if password is not None else "Cle refusee"
        raise ProjectError(f"{what} pour {user}@{host}. (OVH : l'utilisateur est souvent « ubuntu » ou « debian », "
                           f"pas « root »)")
    except (paramiko.SSHException, socket.timeout, EOFError) as e:
        raise ProjectError(f"Connexion SSH impossible : {e}")
    return c


def run(client, cmd, stdin_data=None, timeout=20):
    """(rc, out, err) of a command over an open connection."""
    i, o, e = client.exec_command(cmd, timeout=timeout)
    if stdin_data is not None:
        i.write(stdin_data)
        i.channel.shutdown_write()
    rc = o.channel.recv_exit_status()
    return rc, o.read().decode(errors="replace"), e.read().decode(errors="replace")


def is_expired(client):
    try:
        rc, out, err = run(client, "true", timeout=15)
    except (paramiko.SSHException, socket.timeout, EOFError):
        return True
    text = (out + err).lower()
    return rc != 0 and any(k in text for k in _EXPIRED)


# ── password change through the server's own prompts ───────────────────────

def _read(ch, wait=0.6):
    buf, end = "", time.monotonic() + wait
    while time.monotonic() < end:
        if ch.recv_ready():
            buf += ch.recv(4096).decode(errors="replace")
            end = time.monotonic() + 0.4
        elif ch.closed or ch.exit_status_ready():
            break
        else:
            time.sleep(0.05)
    return buf


def _answer(ch, current, new, deadline):
    """Answer passwd prompts until success / failure. Returns (ok, tail)."""
    seen = ""
    sent_new = 0
    while time.monotonic() < deadline:
        chunk = _read(ch)
        seen += chunk
        tail = seen[-200:].lower()
        last = tail.rstrip()
        if any(k in tail for k in _FAIL):
            return False, seen
        if any(k in tail for k in _OK) and sent_new >= 2:
            return True, seen
        if re.search(r"(retype|re-enter|again|repeat|confirm)[^\n]*password[^\n]*:$", last):
            ch.send(new + "\n"); sent_new += 1; seen += "\n"
            continue
        if re.search(r"new[^\n]*password[^\n]*:$", last):
            ch.send(new + "\n"); sent_new += 1; seen += "\n"
            continue
        if re.search(r"(current|old|\(current\)|unix)[^\n]*password[^\n]*:$", last) or re.search(r"password for [^\n]*:$", last):
            ch.send(current + "\n"); seen += "\n"
            continue
        if ch.closed or ch.exit_status_ready():
            return (sent_new >= 2 and not any(k in seen.lower() for k in _FAIL)), seen
    return False, seen


def change_own_password(client, current, new, expired):
    """Change the logged-in user's password with passwd's own prompts."""
    ch = client.invoke_shell(term="dumb", width=200, height=40)
    try:
        deadline = time.monotonic() + 30
        if not expired:
            _read(ch, 1.5)                                 # banner / prompt
            ch.send("LC_ALL=C passwd\n")
        ok, out = _answer(ch, current, new, deadline)
    finally:
        try:
            ch.close()
        except Exception:
            pass
    if not ok:
        reason = next((line.strip() for line in out.splitlines()[::-1]
                       if any(k in line.lower() for k in _FAIL)), "")
        raise ProjectError("Le serveur a refuse le nouveau mot de passe" + (f" : {reason}" if reason else "")
                           + ". Choisis-en un plus long ou plus different de l'ancien.")
    return True


# ── the whole « access » setup ──────────────────────────────────────────────

def _pubkey_for(server):
    """The public key this PC uses for the server (its key, or a key dedicated to the project)."""
    kp = server.get("key_path")
    if kp and Path(kp + ".pub").is_file():
        return kp, Path(kp + ".pub").read_text().strip()
    for name in ("id_ed25519", "id_rsa"):
        p = SSH_DIR / name
        if p.is_file() and Path(str(p) + ".pub").is_file():
            return str(p), Path(str(p) + ".pub").read_text().strip()
    return None, None


def check_new_password(current, new):
    if not new or len(new) < MIN_LEN:
        raise ProjectError(f"Nouveau mot de passe trop court : {MIN_LEN} caracteres minimum")
    if new == current:
        raise ProjectError("Le nouveau mot de passe doit etre different de celui recu")
    if any(c in new for c in "\n\r"):
        raise ProjectError("Mot de passe invalide")
    if len(set(new)) < 6:
        raise ProjectError("Mot de passe trop simple")


def _sudo(client, cmd, password=None, stdin_data=""):
    """sudo, without a password if allowed, else with the user's password on stdin (never argv)."""
    rc, out, err = run(client, f"sudo -n sh -c {shlex.quote(cmd)}", stdin_data=stdin_data or None)
    if rc != 0 and password and ("password is required" in err or "a password is required" in err or "sudo:" in err):
        rc, out, err = run(client, f"sudo -S -p '' sh -c {shlex.quote(cmd)}", stdin_data=password + "\n" + (stdin_data or ""))
    return rc, out, err


def password_login_state(client, password=None):
    """Does sshd accept passwords? (needed for a new PC to add its key)"""
    rc, out, _ = _sudo(client, "sshd -T 2>/dev/null || /usr/sbin/sshd -T 2>/dev/null || /usr/sbin/sshd.pam -T", password)
    conf = {}
    for line in out.splitlines():
        k, _, v = line.strip().partition(" ")
        conf[k.lower()] = v.lower()
    if not conf:
        return {"known": False}
    pw = conf.get("passwordauthentication") == "yes" or conf.get("kbdinteractiveauthentication") == "yes" and conf.get("usepam") == "yes"
    return {"known": True, "password_login": pw, "root_login": conf.get("permitrootlogin", "")}


def enable_password_login(client, password=None, recharger=True):
    """sshd drop-in that wins over cloud-init's « PasswordAuthentication no » (first match wins)."""
    script = ("mkdir -p /etc/ssh/sshd_config.d && "
              "printf 'PasswordAuthentication yes\\nKbdInteractiveAuthentication yes\\n' > /etc/ssh/sshd_config.d/00-devpilot.conf && "
              "(grep -q '^Include /etc/ssh/sshd_config.d' /etc/ssh/sshd_config || sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config) && "
              "(sshd -t 2>/dev/null || /usr/sbin/sshd -t 2>/dev/null || /usr/sbin/sshd.pam -t)")
    if recharger:      # inutile quand les mots de passe sont DEJA acceptes : rien ne change
        script += " && (systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || pkill -HUP -o sshd)"
    rc, out, err = _sudo(client, script, password)
    if rc != 0:
        raise ProjectError("Impossible d'activer la connexion par mot de passe : " + (err.strip()[-200:] or "sudo refuse"))
    return True


def setup_access(pid, sid, current, new=None, also_root=False, allow_password_login=True, no_expiry=True):
    """Everything in one go. Returns the steps done; raises with a clear message on the first blocking error."""
    srv = S._find(S.list_servers(pid), sid)
    host, port, user = srv["host"], srv.get("port") or 22, srv["user"]
    if new:
        check_new_password(current, new)
    log = []

    c = connect(host, port, user, password=current)
    try:
        expired = is_expired(c)
        if expired and not new:
            return {"needs_new": True, "log": ["Le mot de passe recu est expire : le serveur exige d'en choisir un nouveau"]}
        if new:
            change_own_password(c, current, new, expired)
            log.append(f"mot de passe de {user} change" + (" (l'ancien etait expire)" if expired else ""))
    finally:
        c.close()

    pw = new or current
    c = connect(host, port, user, password=pw)                      # proves the (new) password works
    try:
        if is_expired(c):
            raise ProjectError("Le serveur considere encore le mot de passe comme expire")
        log.append(f"connexion avec {'le nouveau ' if new else 'le '}mot de passe verifiee")

        key_path, pub = _pubkey_for(srv)
        if not pub:
            gen = S.generate_key(pid, srv.get("name") or "serveur")
            key_path, pub = gen["path"], gen["public"]
            log.append(f"cle dediee creee : {key_path.replace(str(Path.home()), '~')}")
        rc, _, err = run(c, 'umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && '
                            'k="$(cat)"; grep -qxF "$k" ~/.ssh/authorized_keys || printf "%s\\n" "$k" >> ~/.ssh/authorized_keys',
                         stdin_data=pub + "\n")
        if rc != 0:
            raise ProjectError("Installation de la cle impossible : " + err.strip()[-200:])
        log.append("cle SSH de ce PC installee")

        if new and no_expiry:
            rc, _, _ = _sudo(c, f"chage -M 99999 -E -1 {shlex.quote(user)}", pw)
            log.append("expiration du mot de passe retiree" if rc == 0 else "expiration : non modifiable (pas de sudo)")
        if new and also_root:
            rc, _, err = _sudo(c, "chpasswd", pw, stdin_data=f"root:{new}\n")
            log.append("mot de passe de root change (meme mot de passe)" if rc == 0 else "root : non modifie (" + err.strip()[-80:] + ")")

        state = password_login_state(c, pw)
        if state.get("known") and not state["password_login"]:
            if allow_password_login:
                enable_password_login(c, pw)
                log.append("connexion par mot de passe activee (pour ajouter la cle d'un nouveau PC)")
            else:
                log.append("attention : le serveur refuse les mots de passe, un nouveau PC ne pourra pas s'y connecter")
        elif state.get("known") and allow_password_login:
            # deja accepte (souvent par 50-cloud-init.conf) : on ECRIT quand meme le choix,
            # sinon un durcissement ulterieur (preparer-serveur.sh) le couperait sans le savoir
            enable_password_login(c, pw, recharger=False)
            log.append("le serveur accepte les mots de passe : choix enregistre, un nouveau PC pourra ajouter sa cle")
        elif state.get("known"):
            log.append("le serveur accepte les mots de passe : un nouveau PC pourra ajouter sa cle")
    finally:
        c.close()

    # the key alone must work now
    if key_path and key_path != srv.get("key_path"):
        S.update_server(pid, sid, {"key_path": key_path}, test=False)
    srv = S.retest(pid, sid)
    if not (srv.get("test") or {}).get("ok"):
        raise ProjectError("La cle a ete installee mais la connexion par cle echoue : " + (srv.get("test") or {}).get("error", ""))
    log.append("connexion par cle verifiee")
    return {"ok": True, "log": log, "server": srv, "password_changed": bool(new)}
