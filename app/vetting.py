"""Company / website vetting endpoints.

Deterministic anti-scam checks for a company name + website, designed to
minimize search-engine use: almost every check is a *direct lookup*
(RDAP registry data, the website itself, the Wayback Machine) rather than
a search query. The single optional search-engine query (exact company
name, for the third-party-mention check) is cached per verdict and can be
disabled entirely with ``VET_ENABLE_MENTIONS=false``.

Checks
------
- ``rdap``      domain age + registrar facts. RDAP base URLs come from the
                IANA bootstrap file; registries without a resolvable
                registration date (e.g. DENIC/.de, which is
                GDPR-redacted) fall back to the first Wayback Machine
                capture as an age lower bound.
- ``impressum`` German Impressum extraction (PLZ/city, phone, HRB
                registry numbers, VAT id, legal form) + consistency.
- ``language``  per-paragraph language fingerprint; a mid-document
                language switch is an LLM-generation marker.
- ``mentions``  ONE exact-name search via ddgs, counting third-party
                domains only (optional, cached with the verdict).
- ``registry``  derived from the Impressum by default (registry numbers
                present / legal form without one); optional live lookup
                via ``VET_REGISTRY_URL_TEMPLATE`` when a direct,
                machine-readable registry API is available
                (handelsregister.de is a stateful JS app and cannot be
                queried with a plain GET).

Verdicts: ``pass`` / ``flag`` / ``reject``. Every check returns its own
status and raw evidence so the calling agent can explain the result and
a human can audit it.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
import time
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.logging import get_logger

try:
    from langdetect import LangDetector
except ImportError:  # optional dependency - the language check degrades gracefully
    LangDetector = None  # type: ignore[assignment]

logger = get_logger(__name__)

router = APIRouter(prefix="/vet", tags=["vetting"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class VetRequest(BaseModel):
    """A company / website to vet before recommending it."""

    name: str = Field(
        ...,
        min_length=2,
        max_length=200,
        description="Company name as claimed (e.g. 'Acme GmbH')",
    )
    url: str = Field(
        ...,
        min_length=8,
        max_length=2000,
        description="Website URL of the company (http/https)",
    )
    claimed_market: str = Field(
        default="de",
        max_length=8,
        description="Market the company claims to serve (ISO country code, e.g. 'de')",
    )


class CheckResult(BaseModel):
    """Outcome of a single vetting check, with its raw evidence."""

    name: str
    status: str = Field(description="pass | flag | reject | unavailable")
    detail: str = ""
    data: dict = Field(default_factory=dict)


class VetResponse(BaseModel):
    """Structured vetting verdict with per-check evidence."""

    verdict: str = Field(description="pass | flag | reject")
    name: str
    url: str
    domain: str
    claimed_market: str
    checks: dict[str, CheckResult]
    rejections: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    summary: str = ""
    checked_at: str
    cached: bool = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Claimed-market code -> ISO country code for the registrar-country comparison.
_MARKET_CC: dict[str, str] = {
    "de": "DE",
    "at": "AT",
    "ch": "CH",
    "fr": "FR",
    "nl": "NL",
    "be": "BE",
    "uk": "GB",
    "us": "US",
    "es": "ES",
    "it": "IT",
}

# RDAP base URLs for well-known registries missing from the IANA bootstrap.
_KNOWN_RDAP_BASES: dict[str, str] = {"de": "https://rdap.denic.de"}

# In-process caches (module-level so tests can inspect/reset them).
_iana_bootstrap: dict[str, str] = {}
_iana_bootstrap_at: float = 0.0
_VET_CACHE: dict[str, tuple[float, VetResponse]] = {}
_MAX_CACHE = 256


# ---------------------------------------------------------------------------
# RDAP (direct registry lookups)
# ---------------------------------------------------------------------------


async def _load_iana_bootstrap(client: httpx.AsyncClient) -> dict[str, str]:
    """Map TLD -> first RDAP base URL from the IANA bootstrap file.

    The file lists, per TLD, the authoritative RDAP base URLs
    (``{"services": [[["tld", ...], ["base_url", ...]], ...]}``).
    Cached in-process for at most one day.
    """
    global _iana_bootstrap, _iana_bootstrap_at
    if _iana_bootstrap and time.monotonic() - _iana_bootstrap_at < 86400:
        return _iana_bootstrap
    try:
        resp = await client.get("https://data.iana.org/rdap/dns.json")
        if resp.status_code == 200:
            tld_map: dict[str, str] = {}
            for entry in resp.json().get("services", []):
                tlds, urls = entry[0], entry[1]
                for base in urls:
                    for tld in tlds:
                        tld_map.setdefault(str(tld).lower(), str(base))
            if tld_map:
                _iana_bootstrap = tld_map
                _iana_bootstrap_at = time.monotonic()
    except (httpx.RequestError, ValueError) as exc:
        logger.warning("RDAP bootstrap fetch failed: %s", exc)
    return _iana_bootstrap


def _rdap_base_url(tld_map: dict[str, str], host: str) -> str | None:
    """Find the RDAP base URL for a host (TLD first, then ccTLD 2nd level)."""
    parts = host.split(".")
    if len(parts) >= 2:
        base = tld_map.get(parts[-1].lower()) or _KNOWN_RDAP_BASES.get(parts[-1].lower())
        if base:
            return base
    if len(parts) >= 3:
        return tld_map.get(".".join(parts[-2:]).lower())
    return None


def _parse_rdap_record(data: dict) -> dict:
    """Extract registration facts from an RDAP domain record."""
    registered: datetime | None = None
    expires: datetime | None = None
    for ev in data.get("events") or []:
        if not isinstance(ev, dict):
            continue
        action = str(ev.get("eventAction") or "").lower()
        raw = ev.get("eventDate")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        if registered is None and action in {"registration", "registered"}:
            registered = dt
        elif expires is None and action in {"expiration", "expiry", "expires", "renewal"}:
            expires = dt

    registrar_name: str | None = None
    registrar_country: str | None = None

    def walk(entities) -> None:
        nonlocal registrar_name, registrar_country
        for ent in entities or []:
            if not isinstance(ent, dict):
                continue
            if "registrar" in (ent.get("roles") or []):
                vcard = ent.get("vcardArray") or [None, []]
                for item in vcard[1] or []:
                    if not isinstance(item, (list, tuple)) or len(item) < 4:
                        continue
                    value = str(item[3]).strip()
                    if not value:
                        continue
                    if registrar_name is None and item[0] == "fn":
                        registrar_name = value
                    if registrar_country is None and item[0] == "country":
                        registrar_country = value.upper()
            walk(ent.get("entities"))

    walk(data.get("entities"))

    age_days = max(0, (datetime.now(UTC) - registered).days) if registered else None
    return {
        "registered": registered.isoformat() if registered else None,
        "expires": expires.isoformat() if expires else None,
        "age_days": age_days,
        "registrar_name": registrar_name,
        "registrar_country": registrar_country,
    }


async def _first_wayback_capture(client: httpx.AsyncClient, host: str) -> str | None:
    """First Wayback Machine capture of the domain (age lower bound)."""
    url = (
        "https://web.archive.org/cdx/search/cdx"
        f"?url={quote(host, safe='')}&output=json&limit=1&fl=timestamp"
    )
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list) and len(rows) >= 2 and isinstance(rows[1], list) and rows[1]:
                ts = str(rows[1][0])
                if re.fullmatch(r"\d{14}", ts):
                    return ts[:8]
    except (httpx.RequestError, ValueError) as exc:
        logger.info("Wayback CDX lookup failed for %s: %s", host, exc)
    return None


async def _check_rdap(client: httpx.AsyncClient, host: str) -> CheckResult:
    """Domain age + registrar facts via RDAP (Wayback fallback for age)."""
    headers = {"Accept": "application/rdap+json", "User-Agent": settings.VET_USER_AGENT}
    tld_map = await _load_iana_bootstrap(client)
    base = _rdap_base_url(tld_map, host)
    candidates: list[str] = []
    if base:
        candidates.append(f"{base.rstrip('/')}/domain/{host}")
    candidates.append(f"https://rdap.org/domain/{host}")

    parsed: dict | None = None
    source: str | None = None
    not_found = False
    for url in candidates:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.RequestError:
            continue
        if resp.status_code == 404:
            not_found = True
            continue
        if resp.status_code != 200:
            continue
        try:
            parsed = _parse_rdap_record(resp.json())
        except ValueError:
            continue
        source = url
        break

    if parsed is None:
        detail = "no RDAP record found (404)" if not_found else "no reachable RDAP source"
        return CheckResult(name="rdap", status="unavailable", detail=detail, data={"host": host})

    first_archived: str | None = None
    archive_age_days: int | None = None
    if parsed["age_days"] is None:
        first_archived = await _first_wayback_capture(client, host)
        if first_archived:
            try:
                captured = datetime.strptime(first_archived, "%Y%m%d").replace(tzinfo=UTC)
                archive_age_days = max(0, (datetime.now(UTC) - captured).days)
            except ValueError:
                first_archived = None
                archive_age_days = None

    data: dict = {
        "host": host,
        "source": source,
        "registered": parsed["registered"],
        "expires": parsed["expires"],
        "age_days": parsed["age_days"],
        "first_archived": first_archived,
        "archive_age_days": archive_age_days,
        "registrar_name": parsed["registrar_name"],
        "registrar_country": parsed["registrar_country"],
    }

    if parsed["age_days"] is None and archive_age_days is None:
        return CheckResult(
            name="rdap",
            status="unavailable",
            detail="RDAP record found but no registration date and no archive history "
            "(typical for GDPR-redacted registries such as DENIC)",
            data=data,
        )

    age = parsed["age_days"] if parsed["age_days"] is not None else archive_age_days
    age_label = (
        f"registered {parsed['registered']}"
        if parsed["age_days"] is not None
        else f"first archived {first_archived}"
    )
    tail = f" (registrar: {parsed['registrar_name']})" if parsed["registrar_name"] else ""
    if parsed["registrar_country"]:
        tail += f", registrar country {parsed['registrar_country']}"

    if age < settings.VET_MIN_DOMAIN_AGE_DAYS:
        return CheckResult(
            name="rdap",
            status="flag",
            detail=f"young domain: {age_label} ({age} days){tail}",
            data=data,
        )
    return CheckResult(
        name="rdap",
        status="pass",
        detail=f"domain {age_label} ({age} days old){tail}",
        data=data,
    )


# ---------------------------------------------------------------------------
# HTML handling + Impressum extraction
# ---------------------------------------------------------------------------

_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|template|svg|head|iframe)[^>]*>.*?</\1\s*>", re.I | re.S
)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_BLOCK_END_RE = re.compile(
    r"</(p|div|li|tr|ul|ol|table|section|article|aside|blockquote|header|footer|nav|h[1-6])\s*>",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_IMP_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\'#>]+)["\'][^>]*>([^<]{0,80})</a>', re.I)
_IMP_HEADING_RE = re.compile(r"^[ \t]*(?:[A-Za-zÄÖÜäöüß0-9&. -]{0,40}[Ii]mpressum)[ \t]*$", re.M)
_PLZ_CITY_RE = re.compile(r"\b(0\d{4}|[1-9]\d{4})[ \t]+([A-ZÄÖÜ][A-Za-zÄÖÜß' .\-]{2,30})\b")
_ANY_PHONE_RE = re.compile(
    r"(\+\d{1,3}[\s\-()./]{0,3}\(?\d{1,4}\)?[\s\-()./\d]{6,18}|\b0\d{2,5}[\s\-/]{0,3}\d{4,11}\b)"
)
_HRB_RE = re.compile(
    r"\b(HRB|HRA|HRG|GPRB|KHS)\b[\s:;.,\-–]*([0-9]{1,6}[A-Za-z]?(?:\s*[/-]\s*[0-9]{1,6}[A-Za-z]?)?)",
    re.I,
)
_VAT_RE = re.compile(
    r"(?:USt[- ]?Id(?:entifier)?[- ]?Nr\.?|Umsatzsteuer[- ]?Id(?:entifier)?[- ]?Nr\.?|Steuernummer)"
    r"\s*[:\-]?\s*([A-Z]{2}\s?\d{8,13})",
    re.I,
)
_LEGAL_FORM_RE = re.compile(
    r"\b(GmbH & Co\.? ?KG|GmbH|KG|AG|SE|GbR|eG|UG \(haftungsbeschr[aä]nkt\))\b", re.I
)


def _valid_german_plz(plz: str) -> bool:
    """German PLZs range from 1051 to 99999."""
    return 1051 <= int(plz) <= 99999


def html_to_text(html: str) -> str:
    """Crude HTML -> line-oriented plain text (enough for extraction)."""
    t = _SCRIPT_RE.sub(" ", html)
    t = _BR_RE.sub("\n", t)
    t = _BLOCK_END_RE.sub("\n", t)
    t = _TAG_RE.sub(" ", t)
    t = html_mod.unescape(t)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in t.splitlines()]
    return "\n".join(line for line in lines if line)


async def _fetch_page(client: httpx.AsyncClient, url: str) -> tuple[str, str] | None:
    """Fetch a page; returns (html, final_url) or None when unreachable."""
    try:
        resp = await client.get(url, headers={"User-Agent": settings.VET_USER_AGENT})
    except httpx.RequestError as exc:
        logger.info("vetting fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code >= 400:
        return None
    return resp.content.decode("utf-8", "replace"), str(resp.url)


def _find_impressum_link(html: str, page_url: str) -> str | None:
    """Find an Impressum link in the raw HTML (text-based)."""
    for m in _IMP_LINK_RE.finditer(html):
        text = m.group(2).strip().lower()
        if "impressum" in text or "legal notice" in text or "imprint" in text:
            return urljoin(page_url, m.group(1).strip())
    return None


async def _check_impressum(
    client: httpx.AsyncClient, start_url: str, claimed_market: str
) -> CheckResult:
    """Locate and parse the Impressum; check address/registry consistency."""
    main = await _fetch_page(client, start_url)
    if main is None:
        return CheckResult(
            name="impressum",
            status="unavailable",
            detail=f"site unreachable: {start_url}",
            data={},
        )
    main_html, final_url = main
    main_text = html_to_text(main_html)

    imp_html, imp_url = main_html, final_url
    if "/impressum" not in final_url.lower():
        link = _find_impressum_link(main_html, final_url)
        if link:
            page = await _fetch_page(client, link)
            if page is not None:
                imp_html, imp_url = page

    if imp_url != final_url or "/impressum" in imp_url.lower():
        found = True
        section = html_to_text(imp_html)[:8000]
    else:
        m = _IMP_HEADING_RE.search(main_text)
        if m:
            found = True
            section = main_text[m.start() :][:8000]
        else:
            found = False
            section = ""

    plz: str | None = None
    city: str | None = None
    m = _PLZ_CITY_RE.search(section)
    if m and _valid_german_plz(m.group(1)):
        plz, city = m.group(1), m.group(2).strip()

    phone: str | None = None
    pm = _ANY_PHONE_RE.search(section)
    if pm:
        phone = re.sub(r"\s+", " ", pm.group(1)).strip()
    phone_is_de = bool(phone) and (phone.startswith("+49") or phone.startswith("0"))

    registry_numbers: list[str] = []
    for hm in _HRB_RE.finditer(section):
        registry_numbers.append(f"{hm.group(1).upper()} {hm.group(2).strip()}")
        if len(registry_numbers) >= 5:
            break

    vat: str | None = None
    vm = _VAT_RE.search(section)
    if vm:
        vat = vm.group(1).replace(" ", "")

    legal_forms = sorted({fm.group(1) for fm in _LEGAL_FORM_RE.finditer(section)})

    data: dict = {
        "found": found,
        "url": imp_url if found else None,
        "plz": plz,
        "city": city,
        "phone": phone,
        "phone_is_german_format": phone_is_de,
        "registry_numbers": registry_numbers,
        "vat_id": vat,
        "legal_forms": legal_forms,
        "excerpt": " ".join(section.split())[:300],
    }

    is_de = claimed_market.lower() == "de"
    if not found:
        return CheckResult(
            name="impressum",
            status="reject" if is_de else "flag",
            detail="no Impressum found"
            + (" (mandatory for German businesses, § 5 DDG)" if is_de else ""),
            data=data,
        )

    problems: list[str] = []
    if not plz:
        problems.append("no postal address found in the Impressum")
    if phone and not phone_is_de:
        problems.append(f"phone number is not in German format ({phone})")
    if legal_forms and not registry_numbers:
        problems.append("claims a legal form (e.g. GmbH) but lists no registry number")

    if problems:
        if is_de and not plz:
            detail = "Impressum present but no valid German postal address (§ 5 DDG)"
            extra = [p for p in problems if p != problems[0]]
            if extra:
                detail += "; " + "; ".join(extra)
            return CheckResult(name="impressum", status="reject", detail=detail, data=data)
        return CheckResult(name="impressum", status="flag", detail="; ".join(problems), data=data)

    bits = [f"Impressum at {imp_url}"]
    if plz:
        bits.append(f"PLZ {plz} {city}")
    if registry_numbers:
        bits.append(", ".join(registry_numbers[:3]))
    return CheckResult(name="impressum", status="pass", detail=" | ".join(bits), data=data)


# ---------------------------------------------------------------------------
# Language fingerprint
# ---------------------------------------------------------------------------


def _analyze_language(text: str) -> CheckResult:
    """Per-paragraph language fingerprint; mid-doc switches are an LLM marker."""
    if LangDetector is None:
        return CheckResult(
            name="language",
            status="unavailable",
            detail="langdetect not installed (pip install langdetect)",
        )
    paragraphs = [p for p in re.split(r"\n+", text) if len(" ".join(p.split())) >= 60]
    if len(paragraphs) < 4:
        return CheckResult(
            name="language", status="unavailable", detail="too little text to analyze"
        )
    try:
        detector = LangDetector(cache_size=128)
    except Exception as exc:  # defensive: never fail the pipeline over the language check
        return CheckResult(
            name="language", status="unavailable", detail=f"langdetect unavailable: {exc}"
        )

    langs: list[str] = []
    for para in paragraphs[:60]:
        try:
            langs.append(detector.detect(para[:400]))
        except Exception:
            continue
    if len(langs) < 4:
        return CheckResult(
            name="language", status="unavailable", detail="not enough detectable paragraphs"
        )

    counts = Counter(langs)
    dominant = counts.most_common(1)[0][0]
    switches = sum(1 for a, b in zip(langs, langs[1:]) if a != b)
    data: dict = {
        "dominant_language": dominant,
        "language_switches": switches,
        "paragraphs_analyzed": len(langs),
        "distribution": dict(counts),
    }
    if switches >= 3:
        return CheckResult(
            name="language",
            status="flag",
            detail=(
                f"dominant language {dominant} with {switches} mid-document "
                f"language switches (typical LLM-generation marker)"
            ),
            data=data,
        )
    return CheckResult(
        name="language",
        status="pass",
        detail=f"dominant language {dominant}, no significant language switching",
        data=data,
    )


async def _check_language(client: httpx.AsyncClient, url: str) -> CheckResult:
    page = await _fetch_page(client, url)
    if page is None:
        return CheckResult(name="language", status="unavailable", detail="site unreachable")
    return _analyze_language(html_to_text(page[0]))


# ---------------------------------------------------------------------------
# Third-party mentions (the ONE optional search)
# ---------------------------------------------------------------------------


def _mentions_search(query: str, max_results: int) -> list[dict]:
    """One exact-name DDG text search (module-level so tests can monkeypatch)."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore[no-redef]
    return list(DDGS().text(query, max_results=max_results) or [])


def _check_mentions(name: str, domain: str) -> CheckResult:
    """Count third-party domains mentioning the exact company name.

    Deliberately the *only* search-engine query in the pipeline, and
    optional (``VET_ENABLE_MENTIONS=false``) so the pipeline can run
    fully search-free.
    """
    if not settings.VET_ENABLE_MENTIONS:
        return CheckResult(
            name="mentions",
            status="unavailable",
            detail="disabled (VET_ENABLE_MENTIONS=false)",
        )
    try:
        results = _mentions_search(f'"{name}"', settings.VET_MENTION_MAX_RESULTS)
    except Exception as exc:
        return CheckResult(
            name="mentions",
            status="unavailable",
            detail=f"search failed — treated as inconclusive: {exc}",
        )

    third_party: list[dict] = []
    seen: set[str] = set()
    for r in results or []:
        if not isinstance(r, dict):
            continue
        href = str(r.get("href") or r.get("url") or "")
        try:
            host = (urlparse(href).hostname or "").lower()
        except ValueError:
            continue
        if not host or host == domain or host.endswith("." + domain):
            continue
        if host in seen:
            continue
        seen.add(host)
        third_party.append({"host": host, "url": href, "title": str(r.get("title") or "")[:120]})

    count = len(third_party)
    if count == 0:
        return CheckResult(
            name="mentions",
            status="flag",
            detail=f"no third-party mentions found for exact name '{name}' (strongest single scam signal)",
            data={"third_party_count": 0, "samples": []},
        )
    return CheckResult(
        name="mentions",
        status="pass",
        detail=f"{count} independent domain(s) mention the name",
        data={"third_party_count": count, "samples": third_party[:5]},
    )


# ---------------------------------------------------------------------------
# Company registry
# ---------------------------------------------------------------------------


async def _check_registry(
    client: httpx.AsyncClient, name: str, impressum: CheckResult
) -> CheckResult:
    """Company-registry check.

    Default: derived from the Impressum (registry numbers present / legal
    form without one). Optional: a live lookup via
    ``VET_REGISTRY_URL_TEMPLATE`` (``{query}`` = URL-encoded company name)
    when a direct, machine-readable registry API is available —
    handelsregister.de itself is a stateful JS app and is not queryable
    via plain GET.
    """
    nums = list(impressum.data.get("registry_numbers") or [])
    template = (settings.VET_REGISTRY_URL_TEMPLATE or "").strip()
    if not template:
        detail = (
            ("Impressum lists registry number(s): " + ", ".join(nums[:3]))
            if nums
            else "Impressum lists no registry number"
        )
        detail += (
            " — no live registry lookup configured (set VET_REGISTRY_URL_TEMPLATE; "
            "handelsregister.de is not plain-GET queryable)"
        )
        return CheckResult(
            name="registry",
            status="unavailable",
            detail=detail,
            data={"registry_numbers": nums, "live_lookup": False},
        )

    url = template.replace("{query}", quote(name))
    try:
        resp = await client.get(url)
    except httpx.RequestError as exc:
        return CheckResult(
            name="registry",
            status="unavailable",
            detail=f"registry lookup failed: {exc}",
            data={"registry_numbers": nums, "live_lookup": True},
        )
    if resp.status_code != 200:
        return CheckResult(
            name="registry",
            status="unavailable",
            detail=f"registry lookup returned HTTP {resp.status_code}",
            data={"registry_numbers": nums, "live_lookup": True},
        )

    matches: int | None = None
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("matches", "count", "total", "resultCount", "hits"):
            if isinstance(payload.get(key), int):
                matches = payload[key]
                break
        if matches is None and isinstance(payload.get("results"), list):
            matches = len(payload["results"])
    if matches is None:
        body = (resp.text or "").lower()
        if "keine treffer" in body or "no results" in body:
            matches = 0
    if matches is None:
        return CheckResult(
            name="registry",
            status="unavailable",
            detail="could not parse the registry response",
            data={"registry_numbers": nums, "live_lookup": True},
        )

    if matches > 0:
        return CheckResult(
            name="registry",
            status="pass",
            detail=f"{matches} registry hit(s) for '{name}'",
            data={"registry_numbers": nums, "matches": matches, "live_lookup": True},
        )
    return CheckResult(
        name="registry",
        status="reject",
        detail=f"company '{name}' has no registry hit",
        data={"registry_numbers": nums, "matches": 0, "live_lookup": True},
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score_checks(
    checks: dict[str, CheckResult], claimed_market: str
) -> tuple[str, list[str], list[str], str]:
    """Deterministic scoring: reject > flag > pass.

    Rejections (any one => verdict ``reject``):
    - R1: registrar country != claimed market AND domain < VET_FOREIGN_REGISTRAR_MAX_AGE_DAYS
    - R2: no Impressum found (claimed_market de)
    - R3: Impressum without a valid German postal address (claimed_market de)
    - R4: zero third-party mentions AND (legal form without registry number OR very young domain)
    - R5: mid-document language switching AND domain < VET_FOREIGN_REGISTRAR_MAX_AGE_DAYS
    - R6: live registry lookup (when configured) found no hit

    Everything else degrades to flags or is treated as inconclusive.
    """
    rdap = checks["rdap"]
    imp = checks["impressum"]
    lang = checks["language"]
    men = checks["mentions"]
    reg = checks["registry"]

    market = claimed_market.lower()
    market_cc = _MARKET_CC.get(market)

    rdap_age = rdap.data.get("age_days")
    if rdap_age is None:
        rdap_age = rdap.data.get("archive_age_days")
    reg_country = rdap.data.get("registrar_country")

    young = isinstance(rdap_age, int) and rdap_age < settings.VET_MIN_DOMAIN_AGE_DAYS
    mid_age = isinstance(rdap_age, int) and rdap_age < settings.VET_FOREIGN_REGISTRAR_MAX_AGE_DAYS
    zero_mentions = men.status == "flag"
    claims_form_without_registry = bool(imp.data.get("legal_forms")) and not imp.data.get(
        "registry_numbers"
    )

    rejections: list[str] = []
    flags: list[str] = []

    # R1: registrar country differs from the claimed market.
    if reg_country and market_cc and reg_country != market_cc:
        if mid_age:
            rejections.append(
                f"registrar country {reg_country} != claimed market {market} "
                f"and domain only {rdap_age} days old"
            )
        else:
            flags.append(f"registrar country {reg_country} differs from claimed market {market}")

    # R2/R3: Impressum (status already encodes missing / no valid address).
    if imp.status == "reject":
        rejections.append(imp.detail)
    elif imp.status == "flag":
        flags.append("impressum: " + imp.detail)
    elif imp.status == "unavailable":
        flags.append("website unreachable — nothing could be verified directly")

    # R4: ghost footprint — zero third-party mentions combined with a
    # legal form but no registry number, or a very young domain.
    r4_fired = False
    if zero_mentions:
        if claims_form_without_registry or (
            isinstance(rdap_age, int) and rdap_age < settings.VET_MIN_DOMAIN_AGE_DAYS
        ):
            rejections.append(
                "no third-party mentions and "
                + (
                    "claims a legal form but lists no registry number"
                    if claims_form_without_registry
                    else f"domain only {rdap_age} days old"
                )
            )
            r4_fired = True
        else:
            flags.append("no third-party mentions found for the exact company name")

    # R5: generated content — language switches on a mid-age domain.
    if lang.status == "flag":
        if mid_age:
            rejections.append(
                f"mid-document language switching on a {rdap_age}-day-old domain"
            )
        else:
            flags.append("language: " + lang.detail)

    # Standalone young-domain flag (skip when already used in R4).
    if young and not r4_fired:
        flags.append(f"domain only {rdap_age} days old")

    # R6: live registry lookup verdicts (when VET_REGISTRY_URL_TEMPLATE is set).
    if reg.status == "reject":
        rejections.append(reg.detail)
    elif reg.status == "flag":
        flags.append(reg.detail)

    verdict = "reject" if rejections else ("flag" if flags else "pass")
    parts = ["verdict=" + verdict]
    parts += [f"{c.name}={c.status}" for c in checks.values()]
    if rejections:
        parts.append("rejected because: " + "; ".join(rejections))
    elif flags:
        parts.append("flagged: " + "; ".join(flags))
    else:
        parts.append("no scam signals found")
    return verdict, rejections, flags, " | ".join(parts)


# ---------------------------------------------------------------------------
# Verdict cache
# ---------------------------------------------------------------------------


def _cache_key(name: str, domain: str) -> str:
    return f"{name.strip().casefold()}|{domain}"


def _cache_get(key: str) -> VetResponse | None:
    item = _VET_CACHE.get(key)
    if item is None:
        return None
    ts, resp = item
    if time.monotonic() - ts >= settings.VET_CACHE_TTL:
        _VET_CACHE.pop(key, None)
        return None
    cached = resp.model_copy(deep=True)
    cached.cached = True
    return cached


def _cache_put(key: str, resp: VetResponse) -> None:
    if len(_VET_CACHE) >= _MAX_CACHE:
        _VET_CACHE.clear()
    _VET_CACHE[key] = (time.monotonic(), resp)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/company",
    response_model=VetResponse,
    summary="Vet a company/website for scam signals",
    operation_id="VetCompany",
)
async def vet_company(req: VetRequest) -> VetResponse:
    """Run the deterministic vetting pipeline for a company + website.

    Call this before recommending any vendor/company/website.
    Verdict ``reject`` -> do not recommend, tell the user why, and offer
    alternatives. Verdict ``flag`` -> recommend only as explicitly
    unverified. Verdict ``pass`` -> recommend normally.
    """
    netloc = req.url if "//" in req.url else "//" + req.url
    host = (urlparse(netloc).hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="url must contain a hostname")

    name = req.name.strip()
    market = req.claimed_market.lower()
    key = _cache_key(name, host)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(
        timeout=settings.VET_HTTP_TIMEOUT, follow_redirects=True
    ) as client:
        rdap, imp, lang, men = await asyncio.gather(
            _check_rdap(client, host),
            _check_impressum(client, req.url, market),
            _check_language(client, req.url),
            asyncio.to_thread(_check_mentions, name, host),
        )
        reg = await _check_registry(client, name, imp)

    checks = {
        "rdap": rdap,
        "impressum": imp,
        "language": lang,
        "mentions": men,
        "registry": reg,
    }
    verdict, rejections, flags, summary = _score_checks(checks, market)
    resp = VetResponse(
        verdict=verdict,
        name=name,
        url=req.url,
        domain=host,
        claimed_market=market,
        checks=checks,
        rejections=rejections,
        flags=flags,
        summary=summary,
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        cached=False,
    )
    _cache_put(key, resp)
    logger.info(
        "vetting %s (%s): %s — %s",
        host,
        name,
        verdict,
        "; ".join(rejections) if rejections else ("; ".join(flags) if flags else "clean"),
    )
    return resp


@router.get(
    "/health",
    summary="Vetting service status",
    operation_id="VetHealth",
)
async def vet_health() -> dict:
    """Report vetting dependency availability."""
    ddgs_state = "not installed"
    try:
        import ddgs  # noqa: F401

        ddgs_state = "available"
    except ImportError:
        try:
            import duckduckgo_search  # noqa: F401

            ddgs_state = "available (duckduckgo_search)"
        except ImportError:
            pass
    return {
        "status": "ok",
        "mentions_enabled": settings.VET_ENABLE_MENTIONS,
        "langdetect": "available" if LangDetector is not None else "not installed",
        "ddgs": ddgs_state,
        "registry_lookup_configured": bool((settings.VET_REGISTRY_URL_TEMPLATE or "").strip()),
    }
