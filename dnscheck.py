"""DevPilot — checks that a client's domain, email and site are configured.

Standard library only (no dnspython). DNS goes through `dig +short` when it
is installed, else `socket.getaddrinfo` (A/AAAA) and `nslookup` (other types).
Nothing here raises on network errors: lookups return [] and HTTP checks
return ok=False with a message. Every user-facing message is French.

  resolve("monsite.ma", "MX")                 -> ["10 mx1.mail.ovh.net", ...]
  check_domain("monsite.ma", {"type": "A", "value": "1.2.3.4"})
  check_mx("monsite.ma", provider="google")
  check_spf_dmarc("monsite.ma")
  check_http("https://monsite.ma")
  check_site("monsite.ma", expected=expected_for("vercel"), provider="google")

Tests inject records by setting the module-level `_resolver` hook to a
callable (domain, rtype) -> list[str].
"""

import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

# Test hook: when set, resolve() calls it instead of touching the network.
_resolver = None
_urlopen = urllib.request.urlopen


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# Seconds per dig/nslookup attempt, and dig attempts before the fallback. Some
# corporate resolvers answer cold queries in 8-12 s: dig gives up at
# DNS_TIMEOUT but the stub resolver caches the late answer, so a retry gets it.
DNS_TIMEOUT = _env_int("DEVPILOT_DNS_TIMEOUT", 3)
DNS_ATTEMPTS = _env_int("DEVPILOT_DNS_ATTEMPTS", 4)

RTYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT")
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 DevPilot")

# Email provider by MX host suffix (host == suffix or host ends with "." + suffix).
KNOWN_MX = {
    "google":     ("aspmx.l.google.com", "gmail-smtp-in.l.google.com", "googlemail.com", "google.com"),
    "microsoft":  ("mail.protection.outlook.com",),
    "zoho":       ("zoho.com", "zoho.eu"),
    "ovh":        ("mail.ovh.net",),
    "ionos":      ("ionos.com", "ionos.fr", "kundenserver.de"),
    "infomaniak": ("infomaniak.com",),
    "gandi":      ("gandi.net",),
    "proton":     ("protonmail.ch",),
    "hostinger":  ("hostinger.com",),
    "namecheap":  ("privateemail.com",),
    "cloudflare": ("mx.cloudflare.net",),
}
MX_LABELS = {
    "google": "Google Workspace", "microsoft": "Microsoft 365", "zoho": "Zoho Mail",
    "ovh": "OVH", "ionos": "IONOS", "infomaniak": "Infomaniak", "gandi": "Gandi",
    "proton": "Proton Mail", "hostinger": "Hostinger", "namecheap": "Namecheap Private Email",
    "cloudflare": "Cloudflare Email Routing",
}

# What a domain should point to per hosting platform. "<...>" placeholders are
# filled by expected_for(target=...). "apex_a" is the IP the bare domain may use
# when the registrar does not support CNAME/ALIAS at the apex.
EXPECTED_FOR_PLATFORM = {
    "vercel":           {"type": "CNAME", "value": "cname.vercel-dns.com", "apex_a": ["76.76.21.21"]},
    "netlify":          {"type": "CNAME", "value": "<site>.netlify.app", "apex_a": ["75.2.60.5"]},
    "cloudflare_pages": {"type": "CNAME", "value": "<project>.pages.dev"},
    "github_pages":     {"type": "CNAME", "value": "<user>.github.io",
                         "apex_a": ["185.199.108.153", "185.199.109.153",
                                    "185.199.110.153", "185.199.111.153"]},
    "render":           {"type": "CNAME", "value": "<app>.onrender.com", "apex_a": ["216.24.57.1"]},
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _norm(name):
    """Lowercase host name without scheme, path or trailing dot."""
    s = str(name or "").strip().lower()
    if "://" in s:
        s = urlsplit(s).hostname or ""
    return s.split("/")[0].rstrip(".")


def _apex(name):
    """The bare domain a client gives us ("www.monsite.ma" -> "monsite.ma")."""
    s = _norm(name)
    return s[4:] if s.startswith("www.") else s


def _strip_dot(s):
    return s.strip().rstrip(".").lower()


def _is_ip(s, rtype):
    fam = socket.AF_INET if rtype == "A" else socket.AF_INET6
    try:
        socket.inet_pton(fam, s)
        return True
    except (OSError, ValueError):
        return False


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _unquote_txt(line):
    """'"v=spf1 " "-all"' -> 'v=spf1 -all' (dig/nslookup split long TXT records)."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', line)
    if not parts:
        return ""
    return "".join(p.replace('\\"', '"').replace("\\\\", "\\") for p in parts)


def _tool(name):
    return shutil.which(name)


def _run(args, timeout=None):
    """stdout of a command, or None when it fails or is missing (never raises)."""
    try:
        r = subprocess.run(list(args), capture_output=True, text=True,
                           timeout=timeout or DNS_TIMEOUT + 5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _parallel(calls):
    """Results of zero-argument callables, in order, run concurrently."""
    if len(calls) <= 1:
        return [c() for c in calls]
    with ThreadPoolExecutor(max_workers=min(8, len(calls))) as ex:
        return list(ex.map(lambda c: c(), calls))


def _resolve_many(pairs):
    """{(domain, rtype): records} for several lookups done concurrently."""
    pairs = _dedupe(list(pairs))
    return dict(zip(pairs, _parallel([lambda d=d, t=t: resolve(d, t) for d, t in pairs])))


# ── Resolution ──────────────────────────────────────────────────────────────

def _parse_dig(text, rtype):
    """Records from `dig +short` output (CNAME chain lines are skipped for A/AAAA)."""
    recs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if rtype in ("A", "AAAA"):
            if _is_ip(line, rtype):
                recs.append(line)
        elif rtype == "MX":
            m = re.match(r"^(\d+)\s+(\S+)$", line)
            if m and _strip_dot(m.group(2)):
                recs.append(f"{int(m.group(1))} {_strip_dot(m.group(2))}")
        elif rtype == "TXT":
            recs.append(_unquote_txt(line))
        else:  # CNAME, NS
            if re.match(r"^[A-Za-z0-9_.-]+$", line):
                recs.append(_strip_dot(line))
    return _dedupe(recs)


def _parse_nslookup(text, rtype):
    """Records from `nslookup -type=<T>` output."""
    recs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if rtype == "MX":
            m = re.search(r"mail exchanger = (\d+)\s+(\S+)", line)
            if m and _strip_dot(m.group(2)):
                recs.append(f"{int(m.group(1))} {_strip_dot(m.group(2))}")
        elif rtype == "CNAME":
            m = re.search(r"canonical name = (\S+)", line)
            if m:
                recs.append(_strip_dot(m.group(1)))
        elif rtype == "NS":
            m = re.search(r"nameserver = (\S+)", line)
            if m:
                recs.append(_strip_dot(m.group(1)))
        elif rtype == "TXT":
            m = re.search(r'text = (".*)$', line)
            if m:
                recs.append(_unquote_txt(m.group(1)))
        else:  # A, AAAA: "Address: 1.2.3.4" (the server line has a "#port" suffix)
            m = re.match(r"^Address:\s*(\S+)$", line)
            if m and _is_ip(m.group(1), rtype):
                recs.append(m.group(1))
    return _dedupe(recs)


def _getaddrinfo(domain, rtype):
    fam = socket.AF_INET if rtype == "A" else socket.AF_INET6
    try:
        infos = socket.getaddrinfo(domain, None, fam, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return _dedupe([i[4][0] for i in infos])


def _fallback(domain, rtype):
    if rtype in ("A", "AAAA"):
        return _getaddrinfo(domain, rtype)
    if _tool("nslookup"):
        return _parse_nslookup(
            _run(["nslookup", f"-timeout={DNS_TIMEOUT}", "-retry=1", f"-type={rtype}", domain]),
            rtype)
    return []


def resolve(domain, rtype):
    """Records of type A/AAAA/CNAME/MX/NS/TXT for a domain; [] on any error.

    MX records are "prio host", TXT records are unquoted, host names have no
    trailing dot. Uses dig when installed (falls back when dig itself fails,
    e.g. unreachable resolver), else getaddrinfo / nslookup.
    """
    domain = _norm(domain)
    rtype = str(rtype or "").strip().upper()
    if not domain or rtype not in RTYPES:
        return []
    if _resolver is not None:
        try:
            return [str(r) for r in (_resolver(domain, rtype) or [])]
        except Exception:
            return []
    try:
        if _tool("dig"):
            # dig exits non-zero when it gets no reply within DNS_TIMEOUT; the
            # stub resolver keeps waiting for the upstream, so a retry gets it.
            for _ in range(DNS_ATTEMPTS):
                out = _run(["dig", "+short", f"+time={DNS_TIMEOUT}", "+tries=1", domain, rtype])
                if out is not None:
                    return _parse_dig(out, rtype)
        return _fallback(domain, rtype)
    except Exception:
        return []


# ── Domain / web ────────────────────────────────────────────────────────────

def _norm_expected(expected):
    """{"type", "value"|"values", "apex_a"?} -> {"type", "values", ("apex_a")}."""
    if not expected:
        return None
    t = str(expected.get("type") or "A").strip().upper()
    vals = expected.get("values") or ([expected.get("value")] if expected.get("value") else [])
    out = {"type": t, "values": _dedupe([_norm(v) if t == "CNAME" else str(v).strip() for v in vals])}
    apex_a = _dedupe([str(v).strip() for v in (expected.get("apex_a") or [])])
    if apex_a:
        out["apex_a"] = apex_a
    return out


def _want(exp, apex=True):
    """'A -> 1.2.3.4' / 'CNAME -> cname.vercel-dns.com' (with the apex A alternative)."""
    vals = " ou ".join(exp["values"])
    if exp["type"] == "CNAME" and apex and exp.get("apex_a"):
        return f"A -> {' ou '.join(exp['apex_a'])} (ou CNAME -> {vals})"
    return f"{exp['type']} -> {vals}"


def _match(found, exp, target_ips, apex=None, apex_ok=False):
    """(ok, what_it_points_to). `apex` is set when checking www.<apex>."""
    cn, a = found.get("CNAME", []), found.get("A", [])
    if exp["type"] in ("A", "AAAA"):
        hits = [ip for ip in found.get(exp["type"], []) if ip in exp["values"]]
        if hits:
            return True, hits[0]
    else:
        hits = [c for c in cn if c in exp["values"]]
        if hits:
            return True, hits[0]
        hits = [ip for ip in a if ip in exp.get("apex_a", [])]
        if hits:
            return True, hits[0]
        hits = [ip for ip in a if ip in target_ips]  # CNAME flattening (Cloudflare, ALIAS)
        if hits:
            return True, f"{hits[0]} (CNAME aplati vers {exp['values'][0]})"
    if apex and apex_ok and apex in cn:
        return True, apex
    return False, None


def _current(found):
    for t in ("CNAME", "A", "AAAA"):
        if found.get(t):
            return found[t][0]
    return None


def check_domain(domain, expected):
    """Does <domain> (and www.<domain>) point where `expected` says?

    expected: {"type": "A", "value": "1.2.3.4"} | {"type": "A", "values": [...]} (any of)
            | {"type": "CNAME", "value": "cname.vercel-dns.com", "apex_a": [...]?}
    A CNAME expectation is also satisfied when the name resolves (A) to the same
    IPs as the CNAME target (flattening), or to one of "apex_a".
    """
    domain = _apex(domain)
    exp = _norm_expected(expected)
    www = "www." + domain
    targets = exp["values"] if exp and exp["type"] == "CNAME" else []
    # All lookups at once: the apex and www (A, AAAA, CNAME) and, for a CNAME
    # expectation, the target's A records (to recognise flattening).
    recs = _resolve_many([(n, t) for n in (domain, www) for t in ("A", "AAAA", "CNAME")]
                         + [(t, "A") for t in targets])
    found = {t: recs[(domain, t)] for t in ("A", "AAAA", "CNAME")}
    wfound = {t: recs[(www, t)] for t in ("A", "AAAA", "CNAME")}
    target_ips = _dedupe([ip for t in targets for ip in recs[(t, "A")]])
    res = {"domain": domain, "ok": False, "found": found, "expected": exp,
           "www": {"ok": False, "found": wfound, "message": ""}, "message": ""}
    if exp is None:
        ok = any(found.values())
        res["ok"] = ok
        res["www"]["ok"] = any(wfound.values())
        res["message"] = (f"{domain} -> {_current(found)}" if ok
                          else f"Aucun enregistrement DNS pour {domain}")
        return res

    ok, how = _match(found, exp, target_ips)
    res["ok"] = ok
    if ok:
        res["message"] = f"DNS OK : {domain} -> {how}"
    elif not any(found.values()):
        res["message"] = (f"Aucun enregistrement {exp['type']} pour {domain} : "
                          f"ajoute {_want(exp)} chez ton registrar")
    else:
        res["message"] = f"{domain} pointe vers {_current(found)} au lieu de {' ou '.join(exp['values'])}"

    wok, whow = _match(wfound, exp, target_ips, apex=domain, apex_ok=ok)
    res["www"]["ok"] = wok
    fix = f"CNAME www -> {domain}" if exp["type"] != "CNAME" else f"CNAME www -> {exp['values'][0]}"
    if wok:
        res["www"]["message"] = f"www OK : {www} -> {whow}"
    elif not any(wfound.values()):
        res["www"]["message"] = f"Aucun enregistrement pour {www} : ajoute {fix}"
    else:
        res["www"]["message"] = f"{www} pointe vers {_current(wfound)} au lieu de {domain}"
    if ok and not wok:
        res["message"] += " ; " + res["www"]["message"]
    return res


def expected_for(platform=None, server_ip=None, target=None):
    """An `expected` dict for check_domain: from a server IP (A) or a platform.

    expected_for(server_ip="1.2.3.4")              -> {"type": "A", "value": "1.2.3.4"}
    expected_for("vercel")                         -> CNAME cname.vercel-dns.com + apex_a
    expected_for("netlify", target="monsite")      -> CNAME monsite.netlify.app + apex_a
    expected_for("github_pages")                   -> {"type": "A", "values": [4 IPs]}
    None when the platform is unknown or its CNAME needs a `target` and none fits.
    """
    if server_ip:
        ips = _dedupe([ip.strip() for ip in re.split(r"[,\s]+", str(server_ip)) if ip.strip()])
        if len(ips) == 1:
            return {"type": "A", "value": ips[0]}
        return {"type": "A", "values": ips} if ips else None
    p = EXPECTED_FOR_PLATFORM.get(str(platform or "").strip().lower().replace("-", "_"))
    if not p:
        return None
    value = p.get("value")
    if value and "<" in value:
        suffix = value[value.index(">") + 1:]  # ".netlify.app"
        target = _norm(target)
        if not target:
            value = None
        elif target.endswith(suffix):
            value = target  # already a full host name
        else:
            value = re.sub(r"<[^>]+>", target, value)
    if value:
        out = {"type": "CNAME", "value": value}
        if p.get("apex_a"):
            out["apex_a"] = list(p["apex_a"])
        return out
    if p.get("apex_a"):
        return {"type": "A", "values": list(p["apex_a"])}
    return None


# ── Email ───────────────────────────────────────────────────────────────────

def detect_mx_provider(hosts):
    """Provider key from KNOWN_MX for a list of MX hosts, or None."""
    for h in hosts:
        h = _strip_dot(str(h))
        for key, suffixes in KNOWN_MX.items():
            if any(h == s or h.endswith("." + s) for s in suffixes):
                return key
    return None


def _label(key):
    return MX_LABELS.get(key, key) if key else None


def check_mx(domain, provider=None):
    """MX records of a domain, the detected provider, and (if given) whether it
    matches `provider` (a KNOWN_MX key)."""
    domain = _apex(domain)
    provider = str(provider or "").strip().lower() or None
    recs = []
    for r in resolve(domain, "MX"):
        m = re.match(r"^\s*(\d+)\s+(\S+)\s*$", r)
        if m and _strip_dot(m.group(2)):
            recs.append({"priority": int(m.group(1)), "host": _strip_dot(m.group(2))})
    recs.sort(key=lambda x: (x["priority"], x["host"]))
    detected = detect_mx_provider([x["host"] for x in recs])
    res = {"domain": domain, "records": recs, "detected": detected, "provider": provider,
           "ok": False, "message": ""}
    if not recs:
        res["message"] = f"Aucun enregistrement MX pour {domain} : les emails ne sont pas configures"
        if provider:
            res["message"] += f" (attendu : {_label(provider)})"
    elif provider and detected != provider:
        res["message"] = (f"Les MX de {domain} pointent vers {_label(detected) or recs[0]['host']} "
                          f"au lieu de {_label(provider)}")
    else:
        res["ok"] = True
        res["message"] = f"MX OK : {domain} -> {_label(detected) or recs[0]['host']}"
    return res


def check_spf_dmarc(domain):
    """SPF (TXT at the domain) and DMARC (TXT at _dmarc.<domain>) records."""
    domain = _apex(domain)
    recs = _resolve_many([(domain, "TXT"), ("_dmarc." + domain, "TXT")])
    spf = next((t for t in recs[(domain, "TXT")] if "v=spf1" in t.lower()), None)
    dmarc = next((t for t in recs[("_dmarc." + domain, "TXT")] if "v=dmarc1" in t.lower()), None)
    policy = None
    if dmarc:
        m = re.search(r"\bp\s*=\s*([a-z]+)", dmarc, re.I)
        policy = m.group(1).lower() if m else None
    res = {"domain": domain, "spf": spf, "dmarc": dmarc, "dmarc_policy": policy,
           "ok": bool(spf and dmarc), "message": ""}
    if spf and dmarc:
        res["message"] = f"SPF et DMARC OK pour {domain}"
    elif spf:
        res["message"] = (f"SPF OK, DMARC manquant pour {domain} : ajoute TXT _dmarc.{domain} -> "
                          f"\"v=DMARC1; p=none; rua=mailto:dmarc@{domain}\"")
    elif dmarc:
        res["message"] = (f"DMARC OK, SPF manquant pour {domain} : ajoute TXT {domain} -> "
                          f"\"v=spf1 include:<ton fournisseur> ~all\"")
    else:
        res["message"] = (f"SPF et DMARC manquants pour {domain} : "
                          f"les emails risquent d'arriver en spam")
    return res


# ── HTTP / TLS ──────────────────────────────────────────────────────────────

def cert_days_left(host, port=443, timeout=6):
    """Days before the TLS certificate of host:port expires; None on any error."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                cert = ss.getpeercert()
        return int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400)
    except Exception:
        return None


def _explain(exc):
    """Short French reason for an HTTP/network error."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "certificat SSL invalide"
    if isinstance(reason, ssl.SSLError):
        return "erreur SSL"
    if isinstance(reason, socket.gaierror):
        return "domaine introuvable"
    if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
        return "delai depasse"
    if isinstance(reason, ConnectionRefusedError):
        return "connexion refusee"
    if isinstance(reason, ConnectionError):
        return "connexion interrompue"
    return str(reason) or exc.__class__.__name__


def check_http(url, timeout=6):
    """GET url (redirects followed) with a browser-like User-Agent. Never raises."""
    url = str(url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    res = {"url": url, "ok": False, "status": None, "final_url": None, "https": False,
           "server": None, "cert_days_left": None, "message": ""}
    if not url:
        res["message"] = "URL vide"
        return res
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "fr,en;q=0.8"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            status, final, server = r.status, r.geturl(), r.headers.get("Server")
    except urllib.error.HTTPError as e:
        status, final = e.code, e.geturl() or url
        server = e.headers.get("Server") if e.headers else None
    except Exception as e:
        res["message"] = f"Site injoignable : {url} ({_explain(e)})"
        return res
    res.update(status=status, final_url=final, server=server, ok=status < 400,
               https=str(final).lower().startswith("https://"))
    if res["https"]:
        host = urlsplit(final).hostname
        if host:
            res["cert_days_left"] = cert_days_left(host, urlsplit(final).port or 443, timeout)
    days = res["cert_days_left"]
    if not res["ok"]:
        res["message"] = f"Erreur HTTP {status} sur {url}"
    elif not res["https"]:
        res["message"] = f"Site OK mais sans HTTPS : {final} (HTTP {status})"
    elif days is None:
        res["message"] = f"Site OK : {final} (HTTP {status})"
    elif days < 15:
        res["message"] = f"Site OK : {final} (HTTP {status}) mais le certificat expire dans {days} j !"
    else:
        res["message"] = f"Site OK : {final} (HTTP {status}, certificat valide {days} j)"
    return res


# ── All in one ──────────────────────────────────────────────────────────────

def check_site(domain, expected=None, provider=None):
    """Everything about a client's domain: DNS (if `expected`), MX (if `provider`),
    SPF/DMARC, HTTPS (then HTTP). "summary" is the list of French one-liners;
    "ok" is DNS + MX + HTTP (+ SPF/DMARC when an email provider is expected)."""
    domain = _apex(domain)

    def http():
        r = check_http("https://" + domain)
        if not r["ok"]:
            plain = check_http("http://" + domain)
            if plain["ok"]:
                plain["https_error"] = r["message"]
                r = plain
        return r

    jobs = {}
    if expected:
        jobs["dns"] = lambda: check_domain(domain, expected)
    if provider:
        jobs["mx"] = lambda: check_mx(domain, provider)
    jobs["spf_dmarc"] = lambda: check_spf_dmarc(domain)
    jobs["http"] = http
    out = {"domain": domain, "dns": None, "mx": None, "spf_dmarc": None, "http": None}
    out.update(zip(jobs, _parallel(list(jobs.values()))))  # the four checks run concurrently
    parts = [out[k]["ok"] for k in ("dns", "mx", "http") if out[k]]
    if provider:  # SPF/DMARC only count when email is expected to work
        parts.append(out["spf_dmarc"]["ok"])
    out["ok"] = all(parts)
    out["summary"] = [x["message"] for x in (out["dns"], out["mx"], out["spf_dmarc"], out["http"]) if x]
    return out
