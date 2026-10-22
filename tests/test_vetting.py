"""Tests for the company vetting endpoints.

Fully hermetic: RDAP (IANA bootstrap + registry), the Wayback Machine and
the company website itself are all mocked with respx; the only search
(ddgs) is monkeypatched at module level.
"""

from __future__ import annotations

import respx
from respx import MockRouter

from app.vetting import _VET_CACHE

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

BOOTSTRAP_JSON = {
    "description": "IANA RDAP bootstrap (test subset)",
    "publication": "2024-01-01",
    "services": [[["com"], ["https://rdap.verisign.com/com/v1/"]]],
    "version": "1.0",
}

SCAM_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><title>Acme Solutions - Premium B2B Supplies</title></head>
<body>
<h1>Premium B2B Supply Chain Solutions</h1>
<p>Acme Solutions is your premium partner for enterprise-grade supply chain
optimization, certified quality management and global logistics excellence
since day one. We serve customers across all industries and regions.</p>
<ul>
<li>Enterprise-grade quality management certified by leading international bodies</li>
<li>Global logistics excellence with dedicated account management for every customer</li>
<li>Trusted by companies worldwide for dependable long-term partnership value</li>
</ul>
<p>Trusted by leading companies worldwide for dependable supply chain excellence.</p>
<nav>
  <a href="/impressum">Impressum</a>
  <a href="/privacy">Privacy</a>
</nav>
</body>
</html>
"""

SCAM_IMPRESSION = """
<!DOCTYPE html>
<html><head><title>404</title></head><body>
<h1>This page does not exist</h1>
<p>404 - Not Found. Please return to the start page.</p>
</body></html>
"""

GOOD_PAGE = """
<!DOCTYPE html>
<html lang="de">
<head><title>Acme Industrial GmbH</title></head>
<body>
<h1>Industriekomponenten seit 1985</h1>
<p>Seit über 35 Jahren sind wir Ihr Partner für hochwertige Industriekomponenten
in der Region München. Qualität und Zuverlässigkeit stehen bei uns an erster
Stelle – persönlich, regional und partnerschaftlich für unsere Kunden.</p>
<p>Wir liefern in ganz Deutschland und Europa und stehen für langfristige
Kooperationen mit Festpreisgarantie. Unser Team berät Sie persönlich vor
Ort in München und den umliegenden Regionen zu allen Themen.</p>
<p>Zertifizierungen nach DIN EN ISO 9001 und ISO 14001, regelmäßige
Audits durch unabhängige Prüfstellen und lückenlose Dokumentation –
das ist unser Standard. Sprechen Sie uns an, wir freuen uns auf Sie.</p>
<p>Über drei Jahrzehnte Erfahrung in der Zulieferindustrie, ein fundiertes
technisches Vertriebsnetz und faire Konditionen – dazu unsere verbindliche
Lieferfristen-Garantie für Stammkunden. Kontaktieren Sie uns unverbindlich
und wir stellen Ihnen ein persönliches Angebot zusammen.</p>
<nav><a href="/impressum">Impressum</a></nav>
</body></html>
"""

GOOD_IMPRESSION = """
<!DOCTYPE html>
<html lang="de">
<head><title>Impressum - Acme Industrial GmbH</title></head>
<body>
<h1>Impressum</h1>
<p>Angaben gemäß § 5 DDG</p>
<p>Acme Industrial GmbH<br>Musterstraße 12<br>80331 München<br>Deutschland</p>
<p>Telefon: +49 89 1234567-0</p>
<p>Handelsregister: HRB 123456 Amtsgericht München</p>
<p>USt-IdNr.: DE 123456789</p>
</body>
</html>
"""


def _scam_routes(m: MockRouter, domain: str) -> None:
    """Mock the full external surface for a young, foreign-registrar scam domain."""
    m.get("https://data.iana.org/rdap/dns.json").respond(json=BOOTSTRAP_JSON)
    m.get(f"https://rdap.denic.de/domain/{domain}").respond(status_code=404, json={"error": "not found"})
    m.get(
        f"https://web.archive.org/cdx/search/cdx?url={domain}&output=json&limit=1&fl=timestamp"
    ).respond(json=[["timestamp"], ["20190601120000"]])
    m.get(f"https://{domain}/").respond(
        text=SCAM_HTML, headers={"content-type": "text/html; charset=utf-8"}
    )
    m.get(f"https://{domain}/impressum").respond(
        text=SCAM_IMPRESSION, headers={"content-type": "text/html; charset=utf-8"}
    )


def _good_routes(m: MockRouter, domain: str) -> None:
    """Mock the full external surface for an established German company."""
    m.get("https://data.iana.org/rdap/dns.json").respond(json=BOOTSTRAP_JSON)
    m.get(f"https://rdap.denic.de/domain/{domain}").respond(status_code=404, json={"error": "not found"})
    m.get(
        f"https://web.archive.org/cdx/search/cdx?url={domain}&output=json&limit=1&fl=timestamp"
    ).respond(json=[["timestamp"], ["20110101120000"]])
    m.get(f"https://{domain}/").respond(
        text=GOOD_PAGE, headers={"content-type": "text/html; charset=utf-8"}
    )
    m.get(f"https://{domain}/impressum").respond(
        text=GOOD_IMPRESSION, headers={"content-type": "text/html; charset=utf-8"}
    )


# ---------------------------------------------------------------------------
# VetCompany
# ---------------------------------------------------------------------------


class TestVetCompany:
    def test_scam_domain_rejected(self, client, monkeypatch):
        monkeypatch.setattr("app.vetting._mentions_search", lambda q, n: [])
        with respx.mock() as m:
            _scam_routes(m, "scam.example")
            r = client.post(
                "/vet/company", json={"name": "Scam GmbH", "url": "https://scam.example/"}
            )
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "reject"
        assert any("registrar country JP" in x for x in body["rejections"])
        assert body["checks"]["impressum"]["status"] == "reject"
        assert body["checks"]["rdap"]["data"]["first_archived"] == "20190601"
        assert body["checks"]["mentions"]["status"] == "flag"

    def test_established_german_company_passes(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.vetting._mentions_search",
            lambda q, n: [
                {
                    "href": "https://www.company-index.de/entry/acme-industrial-gmbh",
                    "title": "Acme Industrial GmbH",
                },
                {"href": "https://acme-industrial.de/", "title": "official"},
            ],
        )
        with respx.mock() as m:
            _good_routes(m, "acme-industrial.de")
            r = client.post(
                "/vet/company",
                json={"name": "Acme Industrial GmbH", "url": "https://acme-industrial.de/"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "pass", body["summary"]
        assert body["checks"]["impressum"]["status"] == "pass"
        assert body["checks"]["impressum"]["data"]["plz"] == "80331"
        assert body["checks"]["registry"]["data"]["registry_numbers"] == ["HRB 123456"]
        assert body["checks"]["mentions"]["data"]["third_party_count"] == 1

    def test_vet_company_mentions_disabled(self, client, monkeypatch):
        monkeypatch.setattr("app.vetting.settings.VET_ENABLE_MENTIONS", False)
        monkeypatch.setattr("app.vetting._mentions_search", lambda q, n: [])
        with respx.mock() as m:
            _scam_routes(m, "scam2.example")
            r = client.post(
                "/vet/company", json={"name": "Scam Two GmbH", "url": "https://scam2.example/"}
            )
        assert r.status_code == 200
        body = r.json()
        assert body["checks"]["mentions"]["status"] == "unavailable"
        assert "disabled" in body["checks"]["mentions"]["detail"]
        # The reject verdict still holds via the direct checks
        # (foreign registrar + young domain + missing Impressum address).
        assert body["verdict"] == "reject"

    def test_vet_company_cached_second_call(self, client, monkeypatch):
        monkeypatch.setattr("app.vetting._mentions_search", lambda q, n: [])
        with respx.mock() as m:
            _scam_routes(m, "scam4.example")
            r1 = client.post(
                "/vet/company",
                json={"name": "Scam Four GmbH", "url": "https://scam4.example/"},
            )
            r2 = client.post(
                "/vet/company",
                json={"name": "Scam Four GmbH", "url": "https://scam4.example/"},
            )
        assert r1.status_code == 200
        body1 = r1.json()
        body2 = r2.json()
        assert body1["cached"] is False
        assert body2["cached"] is True
        assert body2["verdict"] == body1["verdict"]
        assert body2["summary"] == body1["summary"]
        assert len(_VET_CACHE) >= 1


# ---------------------------------------------------------------------------
# VetHealth
# ---------------------------------------------------------------------------


class TestVetHealth:
    def test_health_reports_dependencies(self, client):
        r = client.get("/vet/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["mentions_enabled"] is True
        assert body["langdetect"] in {"available", "not installed"}
