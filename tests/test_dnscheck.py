"""dnscheck — deterministic tests through the `_resolver` hook, plus a few
network tests that are skipped when offline."""
import socket
import urllib.error

import pytest

import dnscheck

# Decided once at collection time, before any monkeypatching.
try:
    ONLINE = bool(dnscheck.resolve("github.com", "A"))
except Exception:
    ONLINE = False
network = pytest.mark.skipif(not ONLINE, reason="hors ligne (github.com ne resout pas)")

DIG_MX = """10 alt1.gmail-smtp-in.l.google.com.
5 gmail-smtp-in.l.google.com.
"""
DIG_TXT = '''"v=spf1 include:_spf.google.com ~all"
"google-site-verification=abc"
"long " "record"
'''
DIG_A_VIA_CNAME = """github.com.
140.82.121.4
"""
NSLOOKUP_MX = """Server:\t\t127.0.0.53
Address:\t127.0.0.53#53

Non-authoritative answer:
gmail.com\tmail exchanger = 40 alt4.gmail-smtp-in.l.google.com.
gmail.com\tmail exchanger = 5 gmail-smtp-in.l.google.com.

Authoritative answers can be found from:
alt1.gmail-smtp-in.l.google.com\tinternet address = 142.250.102.26
"""
NSLOOKUP_TXT = """Non-authoritative answer:
gmail.com\ttext = "v=spf1 redirect=_spf.google.com"
gmail.com\ttext = "yahoo-verification-key=x"
"""
NSLOOKUP_CNAME = "www.github.com\tcanonical name = github.com.\n"
NSLOOKUP_A = "Server:\t\t127.0.0.53\nAddress:\t127.0.0.53#53\n\nName:\tgithub.com\nAddress: 140.82.121.4\n"


@pytest.fixture
def dns(monkeypatch):
    """A fake resolver: fill the returned dict with (domain, rtype) -> records."""
    table = {}
    monkeypatch.setattr(dnscheck, "_resolver", lambda d, t: list(table.get((d, t), [])))
    return table


# ── resolve / parsing ───────────────────────────────────────────────────────

def test_resolve_hook_normalizes_domain_and_type(dns):
    dns[("monsite.ma", "A")] = ["1.2.3.4"]
    assert dnscheck.resolve("MonSite.ma.", "a") == ["1.2.3.4"]
    assert dnscheck.resolve("https://monsite.ma/page", "A") == ["1.2.3.4"]
    assert dnscheck.resolve("monsite.ma", "SOA") == []
    assert dnscheck.resolve("", "A") == []


def test_resolve_never_raises(monkeypatch):
    def boom(d, t):
        raise RuntimeError("network down")
    monkeypatch.setattr(dnscheck, "_resolver", boom)
    assert dnscheck.resolve("monsite.ma", "A") == []


def test_parse_dig():
    assert dnscheck._parse_dig(DIG_MX, "MX") == ["10 alt1.gmail-smtp-in.l.google.com",
                                                 "5 gmail-smtp-in.l.google.com"]
    assert dnscheck._parse_dig(DIG_TXT, "TXT") == ["v=spf1 include:_spf.google.com ~all",
                                                   "google-site-verification=abc", "long record"]
    assert dnscheck._parse_dig(DIG_A_VIA_CNAME, "A") == ["140.82.121.4"]
    assert dnscheck._parse_dig("github.com.\n", "CNAME") == ["github.com"]
    assert dnscheck._parse_dig(";; communications error\n", "A") == []
    assert dnscheck._parse_dig("", "NS") == []


def test_parse_nslookup():
    assert dnscheck._parse_nslookup(NSLOOKUP_MX, "MX") == ["40 alt4.gmail-smtp-in.l.google.com",
                                                           "5 gmail-smtp-in.l.google.com"]
    assert dnscheck._parse_nslookup(NSLOOKUP_TXT, "TXT") == ["v=spf1 redirect=_spf.google.com",
                                                             "yahoo-verification-key=x"]
    assert dnscheck._parse_nslookup(NSLOOKUP_CNAME, "CNAME") == ["github.com"]
    assert dnscheck._parse_nslookup(NSLOOKUP_A, "A") == ["140.82.121.4"]


def test_resolve_fallback_without_dig(monkeypatch):
    monkeypatch.setattr(dnscheck, "_tool", lambda n: None if n == "dig" else "/usr/bin/" + n)
    monkeypatch.setattr(dnscheck, "_run", lambda args, timeout=None: NSLOOKUP_MX if args[0] == "nslookup" else None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, fam=0, typ=0, *a: [(fam, typ, 6, "", ("9.9.9.9", 0))])
    assert dnscheck.resolve("gmail.com", "MX") == ["40 alt4.gmail-smtp-in.l.google.com",
                                                   "5 gmail-smtp-in.l.google.com"]
    assert dnscheck.resolve("gmail.com", "A") == ["9.9.9.9"]


def test_resolve_retries_dig_then_falls_back(monkeypatch):
    # dig installed but the resolver is slow/unreachable (rc != 0 -> _run gives None)
    calls = []

    def run(args, timeout=None):
        calls.append(args[0])
        return None if args[0] == "dig" else NSLOOKUP_CNAME
    monkeypatch.setattr(dnscheck, "_tool", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(dnscheck, "_run", run)
    assert dnscheck.resolve("www.github.com", "CNAME") == ["github.com"]
    assert calls == ["dig"] * dnscheck.DNS_ATTEMPTS + ["nslookup"]

    # a late answer on the second attempt is enough
    calls.clear()
    monkeypatch.setattr(dnscheck, "_run", lambda args, timeout=None: (calls.append(1), None)[1] if len(calls) < 1 else "github.com.\n")
    assert dnscheck.resolve("www.github.com", "CNAME") == ["github.com"]
    assert calls == [1]


def test_resolve_no_tools_at_all(monkeypatch):
    monkeypatch.setattr(dnscheck, "_tool", lambda n: None)
    assert dnscheck.resolve("monsite.ma", "MX") == []


# ── check_domain ────────────────────────────────────────────────────────────

def test_check_domain_ok(dns):
    dns[("monsite.ma", "A")] = ["1.2.3.4"]
    dns[("www.monsite.ma", "CNAME")] = ["monsite.ma"]
    r = dnscheck.check_domain("monsite.ma", {"type": "A", "value": "1.2.3.4"})
    assert r["ok"] is True
    assert r["message"] == "DNS OK : monsite.ma -> 1.2.3.4"
    assert r["found"] == {"A": ["1.2.3.4"], "AAAA": [], "CNAME": []}
    assert r["expected"] == {"type": "A", "values": ["1.2.3.4"]}
    assert r["www"]["ok"] is True
    assert r["www"]["found"]["CNAME"] == ["monsite.ma"]


def test_check_domain_wrong_ip(dns):
    dns[("monsite.ma", "A")] = ["5.6.7.8"]
    r = dnscheck.check_domain("monsite.ma", {"type": "A", "value": "1.2.3.4"})
    assert r["ok"] is False
    assert r["message"] == "monsite.ma pointe vers 5.6.7.8 au lieu de 1.2.3.4"


def test_check_domain_missing(dns):
    r = dnscheck.check_domain("monsite.ma", {"type": "A", "value": "1.2.3.4"})
    assert r["ok"] is False
    assert r["message"] == "Aucun enregistrement A pour monsite.ma : ajoute A -> 1.2.3.4 chez ton registrar"
    assert r["www"]["ok"] is False


def test_check_domain_any_of_values(dns):
    dns[("monsite.ma", "A")] = ["185.199.110.153"]
    exp = dnscheck.expected_for("github_pages")
    r = dnscheck.check_domain("monsite.ma", exp)
    assert r["ok"] is True
    assert r["message"].startswith("DNS OK : monsite.ma -> 185.199.110.153")


def test_check_domain_cname(dns):
    dns[("monsite.ma", "CNAME")] = ["cname.vercel-dns.com"]
    dns[("www.monsite.ma", "CNAME")] = ["cname.vercel-dns.com"]
    r = dnscheck.check_domain("monsite.ma", {"type": "CNAME", "value": "cname.vercel-dns.com"})
    assert r["ok"] is True and r["www"]["ok"] is True
    assert r["message"] == "DNS OK : monsite.ma -> cname.vercel-dns.com"


def test_check_domain_cname_flattened(dns):
    # Cloudflare-style: the apex has no CNAME but resolves to the target's IPs.
    dns[("cname.vercel-dns.com", "A")] = ["76.76.21.98", "76.76.21.123"]
    dns[("monsite.ma", "A")] = ["76.76.21.123"]
    dns[("www.monsite.ma", "A")] = ["76.76.21.98"]
    r = dnscheck.check_domain("monsite.ma", {"type": "CNAME", "value": "cname.vercel-dns.com"})
    assert r["ok"] is True
    assert "aplati" in r["message"]
    assert r["www"]["ok"] is True


def test_check_domain_cname_apex_a(dns):
    dns[("monsite.ma", "A")] = ["76.76.21.21"]
    dns[("www.monsite.ma", "CNAME")] = ["monsite.ma"]
    r = dnscheck.check_domain("monsite.ma", dnscheck.expected_for("vercel"))
    assert r["ok"] is True and r["www"]["ok"] is True


def test_check_domain_cname_missing_mentions_apex_a(dns):
    r = dnscheck.check_domain("monsite.ma", dnscheck.expected_for("vercel"))
    assert r["ok"] is False
    assert r["message"] == ("Aucun enregistrement CNAME pour monsite.ma : ajoute A -> 76.76.21.21 "
                            "(ou CNAME -> cname.vercel-dns.com) chez ton registrar")


def test_check_domain_cname_wrong(dns):
    dns[("monsite.ma", "CNAME")] = ["autre.example.com"]
    r = dnscheck.check_domain("monsite.ma", {"type": "CNAME", "value": "cname.vercel-dns.com"})
    assert r["ok"] is False
    assert r["message"] == "monsite.ma pointe vers autre.example.com au lieu de cname.vercel-dns.com"


def test_check_domain_www_missing_keeps_apex_ok(dns):
    dns[("monsite.ma", "A")] = ["1.2.3.4"]
    r = dnscheck.check_domain("monsite.ma", {"type": "A", "value": "1.2.3.4"})
    assert r["ok"] is True
    assert r["www"]["ok"] is False
    assert r["message"] == ("DNS OK : monsite.ma -> 1.2.3.4 ; "
                            "Aucun enregistrement pour www.monsite.ma : ajoute CNAME www -> monsite.ma")


def test_check_domain_www_wrong(dns):
    dns[("monsite.ma", "A")] = ["1.2.3.4"]
    dns[("www.monsite.ma", "A")] = ["9.9.9.9"]
    r = dnscheck.check_domain("monsite.ma", {"type": "A", "value": "1.2.3.4"})
    assert r["ok"] is True and r["www"]["ok"] is False
    assert "www.monsite.ma pointe vers 9.9.9.9" in r["message"]


def test_check_domain_www_to_apex_not_ok_when_apex_wrong(dns):
    dns[("monsite.ma", "A")] = ["5.6.7.8"]
    dns[("www.monsite.ma", "CNAME")] = ["monsite.ma"]
    r = dnscheck.check_domain("monsite.ma", {"type": "A", "value": "1.2.3.4"})
    assert r["ok"] is False and r["www"]["ok"] is False


def test_check_domain_accepts_www_and_url_input(dns):
    dns[("monsite.ma", "A")] = ["1.2.3.4"]
    assert dnscheck.check_domain("https://www.monsite.ma/", {"type": "A", "value": "1.2.3.4"})["domain"] == "monsite.ma"


def test_check_domain_without_expectation(dns):
    dns[("monsite.ma", "A")] = ["1.2.3.4"]
    r = dnscheck.check_domain("monsite.ma", None)
    assert r["ok"] is True and r["expected"] is None and r["message"] == "monsite.ma -> 1.2.3.4"
    assert dnscheck.check_domain("vide.ma", None)["message"] == "Aucun enregistrement DNS pour vide.ma"


# ── expected_for ────────────────────────────────────────────────────────────

def test_expected_for():
    assert dnscheck.expected_for(server_ip="1.2.3.4") == {"type": "A", "value": "1.2.3.4"}
    assert dnscheck.expected_for(server_ip="1.2.3.4, 5.6.7.8") == {"type": "A", "values": ["1.2.3.4", "5.6.7.8"]}
    assert dnscheck.expected_for("vercel") == {"type": "CNAME", "value": "cname.vercel-dns.com",
                                               "apex_a": ["76.76.21.21"]}
    assert dnscheck.expected_for("netlify", target="monsite") == {"type": "CNAME", "value": "monsite.netlify.app",
                                                                  "apex_a": ["75.2.60.5"]}
    assert dnscheck.expected_for("netlify", target="monsite.netlify.app")["value"] == "monsite.netlify.app"
    assert dnscheck.expected_for("netlify") == {"type": "A", "values": ["75.2.60.5"]}
    assert dnscheck.expected_for("cloudflare_pages", target="monsite") == {"type": "CNAME", "value": "monsite.pages.dev"}
    assert dnscheck.expected_for("cloudflare_pages") is None
    assert dnscheck.expected_for("github_pages") == {"type": "A", "values": [
        "185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"]}
    assert dnscheck.expected_for("render", target="monapp")["value"] == "monapp.onrender.com"
    assert dnscheck.expected_for("inconnu") is None
    assert dnscheck.expected_for(None) is None


# ── check_mx ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hosts,provider", [
    (["10 aspmx.l.google.com", "20 alt1.aspmx.l.google.com"], "google"),
    (["5 gmail-smtp-in.l.google.com"], "google"),
    (["1 mx1.mail.ovh.net", "5 mx2.mail.ovh.net", "100 mx3.mail.ovh.net"], "ovh"),
    (["0 monsite-ma.mail.protection.outlook.com"], "microsoft"),
    (["10 mx.zoho.eu"], "zoho"),
    (["10 mx00.ionos.fr"], "ionos"),
    (["10 mail.infomaniak.com"], "infomaniak"),
    (["10 spool.mail.gandi.net"], "gandi"),
    (["10 mail.protonmail.ch"], "proton"),
    (["5 mx1.hostinger.com"], "hostinger"),
    (["10 mx1.privateemail.com"], "namecheap"),
    (["86 isaac.mx.cloudflare.net"], "cloudflare"),
    (["10 mail.monsite.ma"], None),
])
def test_check_mx_detects_provider(dns, hosts, provider):
    dns[("monsite.ma", "MX")] = hosts
    r = dnscheck.check_mx("monsite.ma")
    assert r["detected"] == provider
    assert r["ok"] is True
    assert r["message"].startswith("MX OK : monsite.ma -> ")


def test_check_mx_records_sorted_and_matching_provider(dns):
    dns[("monsite.ma", "MX")] = ["100 mx3.mail.ovh.net", "1 mx1.mail.ovh.net", "5 mx2.mail.ovh.net"]
    r = dnscheck.check_mx("monsite.ma", provider="ovh")
    assert r["ok"] is True
    assert r["records"] == [{"priority": 1, "host": "mx1.mail.ovh.net"},
                            {"priority": 5, "host": "mx2.mail.ovh.net"},
                            {"priority": 100, "host": "mx3.mail.ovh.net"}]
    assert r["message"] == "MX OK : monsite.ma -> OVH"


def test_check_mx_provider_mismatch(dns):
    dns[("monsite.ma", "MX")] = ["1 mx1.mail.ovh.net"]
    r = dnscheck.check_mx("monsite.ma", provider="google")
    assert r["ok"] is False
    assert r["detected"] == "ovh"
    assert r["message"] == "Les MX de monsite.ma pointent vers OVH au lieu de Google Workspace"


def test_check_mx_none(dns):
    r = dnscheck.check_mx("monsite.ma", provider="google")
    assert r["ok"] is False and r["records"] == [] and r["detected"] is None
    assert r["message"] == ("Aucun enregistrement MX pour monsite.ma : les emails ne sont pas configures "
                            "(attendu : Google Workspace)")
    assert dnscheck.check_mx("monsite.ma")["message"] == \
        "Aucun enregistrement MX pour monsite.ma : les emails ne sont pas configures"


# ── check_spf_dmarc ─────────────────────────────────────────────────────────

def test_spf_dmarc_both(dns):
    dns[("monsite.ma", "TXT")] = ["google-site-verification=x", "v=spf1 include:_spf.google.com ~all"]
    dns[("_dmarc.monsite.ma", "TXT")] = ["v=DMARC1; p=quarantine; rua=mailto:dmarc@monsite.ma"]
    r = dnscheck.check_spf_dmarc("monsite.ma")
    assert r["ok"] is True
    assert r["spf"] == "v=spf1 include:_spf.google.com ~all"
    assert r["dmarc"].startswith("v=DMARC1")
    assert r["dmarc_policy"] == "quarantine"
    assert r["message"] == "SPF et DMARC OK pour monsite.ma"


def test_spf_only(dns):
    dns[("monsite.ma", "TXT")] = ["v=spf1 -all"]
    r = dnscheck.check_spf_dmarc("monsite.ma")
    assert r["ok"] is False and r["spf"] == "v=spf1 -all" and r["dmarc"] is None
    assert r["message"].startswith("SPF OK, DMARC manquant pour monsite.ma : ajoute TXT _dmarc.monsite.ma")


def test_dmarc_only(dns):
    dns[("_dmarc.monsite.ma", "TXT")] = ["v=DMARC1; p=none"]
    r = dnscheck.check_spf_dmarc("monsite.ma")
    assert r["ok"] is False and r["spf"] is None and r["dmarc_policy"] == "none"
    assert r["message"].startswith("DMARC OK, SPF manquant pour monsite.ma")


def test_spf_dmarc_none(dns):
    r = dnscheck.check_spf_dmarc("monsite.ma")
    assert r["ok"] is False and r["spf"] is None and r["dmarc"] is None
    assert r["message"] == "SPF et DMARC manquants pour monsite.ma : les emails risquent d'arriver en spam"


# ── check_http (faked transport) ────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, url="https://monsite.ma/", server="nginx"):
        self.status, self.url, self.headers = status, url, {"Server": server}

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_check_http_ok(monkeypatch):
    monkeypatch.setattr(dnscheck, "_urlopen", lambda req, timeout: FakeResp())
    monkeypatch.setattr(dnscheck, "cert_days_left", lambda host, port=443, timeout=6: 42)
    r = dnscheck.check_http("monsite.ma")
    assert r == {"url": "https://monsite.ma", "ok": True, "status": 200, "final_url": "https://monsite.ma/",
                 "https": True, "server": "nginx", "cert_days_left": 42,
                 "message": "Site OK : https://monsite.ma/ (HTTP 200, certificat valide 42 j)"}


def test_check_http_cert_expiring_soon(monkeypatch):
    monkeypatch.setattr(dnscheck, "_urlopen", lambda req, timeout: FakeResp())
    monkeypatch.setattr(dnscheck, "cert_days_left", lambda host, port=443, timeout=6: 3)
    assert "expire dans 3 j" in dnscheck.check_http("https://monsite.ma")["message"]


def test_check_http_no_https(monkeypatch):
    monkeypatch.setattr(dnscheck, "_urlopen", lambda req, timeout: FakeResp(url="http://monsite.ma/"))
    r = dnscheck.check_http("http://monsite.ma")
    assert r["ok"] is True and r["https"] is False and r["cert_days_left"] is None
    assert r["message"] == "Site OK mais sans HTTPS : http://monsite.ma/ (HTTP 200)"


def test_check_http_error_status(monkeypatch):
    def raise_404(req, timeout):
        raise urllib.error.HTTPError("https://monsite.ma/", 404, "Not Found", {"Server": "nginx"}, None)
    monkeypatch.setattr(dnscheck, "_urlopen", raise_404)
    monkeypatch.setattr(dnscheck, "cert_days_left", lambda host, port=443, timeout=6: 42)
    r = dnscheck.check_http("https://monsite.ma")
    assert r["ok"] is False and r["status"] == 404 and r["https"] is True
    assert r["message"] == "Erreur HTTP 404 sur https://monsite.ma"


def test_check_http_unreachable(monkeypatch):
    def fail(req, timeout):
        raise urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
    monkeypatch.setattr(dnscheck, "_urlopen", fail)
    r = dnscheck.check_http("https://nexistepas.ma")
    assert r["ok"] is False and r["status"] is None
    assert r["message"] == "Site injoignable : https://nexistepas.ma (domaine introuvable)"

    monkeypatch.setattr(dnscheck, "_urlopen", lambda req, timeout: (_ for _ in ()).throw(TimeoutError()))
    assert "delai depasse" in dnscheck.check_http("https://lent.ma")["message"]


# ── check_site ──────────────────────────────────────────────────────────────

def test_check_site_combines_everything(dns, monkeypatch):
    dns[("monsite.ma", "A")] = ["1.2.3.4"]
    dns[("www.monsite.ma", "CNAME")] = ["monsite.ma"]
    dns[("monsite.ma", "MX")] = ["1 aspmx.l.google.com"]
    dns[("monsite.ma", "TXT")] = ["v=spf1 include:_spf.google.com ~all"]
    dns[("_dmarc.monsite.ma", "TXT")] = ["v=DMARC1; p=none"]
    monkeypatch.setattr(dnscheck, "_urlopen", lambda req, timeout: FakeResp())
    monkeypatch.setattr(dnscheck, "cert_days_left", lambda host, port=443, timeout=6: 60)
    r = dnscheck.check_site("monsite.ma", expected={"type": "A", "value": "1.2.3.4"}, provider="google")
    assert r["ok"] is True
    assert r["summary"] == ["DNS OK : monsite.ma -> 1.2.3.4",
                            "MX OK : monsite.ma -> Google Workspace",
                            "SPF et DMARC OK pour monsite.ma",
                            "Site OK : https://monsite.ma/ (HTTP 200, certificat valide 60 j)"]


def test_check_site_http_fallback_and_optional_parts(dns, monkeypatch):
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if req.full_url.startswith("https://"):
            raise urllib.error.URLError(ConnectionRefusedError())
        return FakeResp(url="http://monsite.ma/")
    monkeypatch.setattr(dnscheck, "_urlopen", urlopen)
    r = dnscheck.check_site("monsite.ma")
    assert calls == ["https://monsite.ma", "http://monsite.ma"]
    assert r["dns"] is None and r["mx"] is None
    assert r["http"]["ok"] is True and r["http"]["https"] is False
    assert r["http"]["https_error"] == "Site injoignable : https://monsite.ma (connexion refusee)"
    # SPF/DMARC missing is only a warning when no email provider is expected
    assert r["spf_dmarc"]["ok"] is False and r["ok"] is True
    assert len(r["summary"]) == 2


def test_check_site_fails_when_dns_wrong(dns, monkeypatch):
    dns[("monsite.ma", "A")] = ["5.6.7.8"]
    monkeypatch.setattr(dnscheck, "_urlopen", lambda req, timeout: FakeResp())
    monkeypatch.setattr(dnscheck, "cert_days_left", lambda host, port=443, timeout=6: 60)
    r = dnscheck.check_site("monsite.ma", expected={"type": "A", "value": "1.2.3.4"})
    assert r["ok"] is False
    assert r["summary"][0] == "monsite.ma pointe vers 5.6.7.8 au lieu de 1.2.3.4"


# ── Network (skipped offline) ───────────────────────────────────────────────

@network
def test_net_resolve_github():
    assert dnscheck.resolve("github.com", "A")
    assert "github.com" in dnscheck.resolve("www.github.com", "CNAME")
    assert dnscheck.resolve("ce-domaine-n-existe-pas-devpilot.example", "A") == []


@network
def test_net_gmail_mx_is_google():
    r = dnscheck.check_mx("gmail.com")
    assert r["detected"] == "google" and r["ok"] is True
    assert all(isinstance(x["priority"], int) and "." in x["host"] for x in r["records"])


@network
def test_net_gmail_spf_dmarc():
    r = dnscheck.check_spf_dmarc("gmail.com")
    if r["spf"] is None and r["dmarc"] is None:
        pytest.skip("TXT lookups timed out (slow upstream DNS on this machine)")
    assert r["spf"] and r["spf"].startswith("v=spf1")
    assert r["dmarc"] and r["dmarc"].startswith("v=DMARC1")


@network
def test_net_check_http_github():
    r = dnscheck.check_http("https://github.com")
    assert r["ok"] is True and r["status"] == 200 and r["https"] is True
    assert r["cert_days_left"] is not None and r["cert_days_left"] > 0
    assert r["message"].startswith("Site OK : https://github.com")
