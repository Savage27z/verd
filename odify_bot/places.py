"""Find businesses on Google Maps that have no real website.

Two providers, picked by PLACES_PROVIDER (or whichever key is set):

* google — Places API (New) Text Search: one request returns up to 20 places *with*
  website + phone, so a 60-place scan costs ≤3 requests. The legacy API (one Details call
  per place) is kept only as a fallback for keys without "Places API (New)" enabled.
* serper — serper.dev's Google Maps endpoint. No Google Cloud account or card needed;
  ~20 places per request, same fields.
"""
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import phonenumbers
import requests

from .emails import find_business_email

log = logging.getLogger("odify.places")

NEW_API = "https://places.googleapis.com/v1/places:searchText"
LEGACY_API = "https://maps.googleapis.com/maps/api/place"
SERPER_API = "https://google.serper.dev/maps"
TIMEOUT = 15
MAX_PAGES = 3  # Google caps text search at 60 results either way

NEW_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.websiteUri",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.rating",
    "places.userRatingCount", "places.googleMapsUri", "places.businessStatus",
    "places.primaryTypeDisplayName", "places.regularOpeningHours.weekdayDescriptions", "nextPageToken",
])

# A "website" on one of these hosts is a social, directory or booking-platform profile, not a
# site the business owns. Those businesses are still great leads (active online, no site).
SOCIAL_HOSTS = (
    "facebook.com", "fb.com", "fb.me", "instagram.com", "linktr.ee", "wa.me", "whatsapp.com",
    "api.whatsapp.com", "twitter.com", "x.com", "tiktok.com", "linkedin.com", "youtube.com",
    "t.me", "business.site", "g.page", "snapchat.com", "pinterest.com", "yelp.com",
    "bio.link", "beacons.ai", "taplink.cc", "linkin.bio",
    # booking / listing platforms
    "booksy.com", "fresha.com", "treatwell.com", "treatwell.co.uk", "treatwell.de", "vagaro.com",
    "styleseat.com", "setmore.com", "simplybook.me", "calendly.com", "planity.com", "doctolib.fr",
    "doctolib.de", "znanylekarz.pl", "doctoralia.com", "doctoralia.com.br", "zocdoc.com", "tripadvisor.com",
    "ubereats.com", "glovoapp.com", "wolt.com",
)

CLOSED_STATUSES = ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")


class PlacesError(RuntimeError):
    pass


class _NewApiUnavailable(Exception):
    pass


def provider() -> str:
    """'google' | 'serper' — explicit PLACES_PROVIDER wins, else whichever key is configured."""
    explicit = os.getenv("PLACES_PROVIDER", "").strip().lower()
    if explicit in ("google", "serper"):
        return explicit
    if explicit:
        raise PlacesError(f"PLACES_PROVIDER must be 'google' or 'serper', not {explicit!r}")
    if os.getenv("GOOGLE_PLACES_API_KEY"):
        return "google"
    if os.getenv("SERPER_API_KEY"):
        return "serper"
    raise PlacesError("Set SERPER_API_KEY or GOOGLE_PLACES_API_KEY")


def _key(name: str) -> str:
    key = os.getenv(name, "")
    if not key:
        raise PlacesError(f"{name} not set")
    return key


def to_international(phone: str, region: str = "") -> str:
    """'22 123 45 67' + 'PL' -> '+48 22 123 45 67'. Any country's format; '+…' numbers need no
    region. Returns '' when the number can't be read safely."""
    phone = (phone or "").strip()
    if not phone:
        return ""
    try:
        num = phonenumbers.parse(phone, region.upper() or None)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_possible_number(num):
        return ""
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)


def classify_website(url: str) -> str:
    """'none' | 'social' | 'website'."""
    if not url or not url.strip():
        return "none"
    host = urlparse(url if "://" in url else f"http://{url}").netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    if any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
        return "social"
    return "website"


def lead_score(lead: dict) -> int:
    """0–100 'how worth calling is this'. Many reviews + no website = active business
    that is leaving money on the table; reachable contact info makes it actionable."""
    reviews = int(lead.get("reviews") or 0)
    rating = lead.get("rating") or 0
    score = min(40.0, math.log10(reviews + 1) * 18)  # 1 review≈5, 10≈19, 100≈36, 160+=40
    if rating >= 4.5:
        score += 15
    elif rating >= 4.0:
        score += 10
    elif rating >= 3.5:
        score += 5
    if lead.get("phone"):
        score += 20
    if lead.get("email"):
        score += 15
    if lead.get("web_presence") == "social":
        score += 10  # already markets itself online — easier sell
    return int(max(0, min(100, round(score))))


def large_photo_url(url: str) -> str:
    """Google photo links default to a ~340px thumbnail; ask for a 1600x900 smart crop instead."""
    if not url or "googleusercontent.com" not in url:
        return url or ""
    return re.sub(r"=[a-z0-9-]+$", "", url, flags=re.IGNORECASE) + "=w1600-h900-p-k-no"


def hours_json(pairs: list[tuple[str, str]]) -> str:
    """Opening hours as a JSON list of [day, text] in the order Google gave them."""
    pairs = [(str(d).strip(), str(t).strip()) for d, t in pairs if str(d).strip()]
    return json.dumps(pairs, ensure_ascii=False) if pairs else ""


def _weekday_descriptions(lines: list[str]) -> list[tuple[str, str]]:
    """Google's 'Monday: 9:00 AM – 5:00 PM' lines -> [('Monday', '9:00 AM – 5:00 PM'), ...]."""
    return [tuple(x.strip() for x in line.split(":", 1)) for line in lines if ":" in line]


def _lead(**kw) -> dict:
    base = {
        "place_id": "", "name": "", "phone": "", "intl_phone": "", "address": "", "email": "",
        "website": "", "web_presence": "none", "category": "", "rating": None, "reviews": 0,
        "maps_url": "", "photo_url": "", "hours": "", "booking_url": "",
    }
    base.update({k: v for k, v in kw.items() if v is not None})
    return base


# ---- Places API (New) ----

def _search_new(query: str, key: str, want: int, keep, region: str = "", language: str = "") -> list[dict]:
    out: list[dict] = []
    token = None
    for page in range(MAX_PAGES):
        body = {"textQuery": query, "pageSize": 20}
        if region:
            body["regionCode"] = region
        if language:
            body["languageCode"] = language
        if token:
            body["pageToken"] = token
        resp = None
        for attempt in range(3):
            resp = requests.post(
                NEW_API, json=body, timeout=TIMEOUT,
                headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": NEW_FIELDS},
            )
            # Fresh page tokens are occasionally not ready yet; back off briefly.
            if resp.status_code == 400 and token and attempt < 2:
                time.sleep(1.0 + attempt)
                continue
            break
        if resp.status_code == 403 and page == 0:
            raise _NewApiUnavailable(resp.text[:300])
        if not resp.ok:
            raise PlacesError(f"Places API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for p in data.get("places", []):
            if p.get("businessStatus") in CLOSED_STATUSES:
                continue
            website = p.get("websiteUri", "") or ""
            lead = _lead(
                place_id=p.get("id", ""),
                name=(p.get("displayName") or {}).get("text", ""),
                phone=p.get("nationalPhoneNumber", ""),
                intl_phone=p.get("internationalPhoneNumber", ""),
                address=p.get("formattedAddress", ""),
                website=website,
                web_presence=classify_website(website),
                category=(p.get("primaryTypeDisplayName") or {}).get("text", ""),
                rating=p.get("rating"),
                reviews=p.get("userRatingCount", 0),
                maps_url=p.get("googleMapsUri", ""),
                hours=hours_json(_weekday_descriptions(
                    (p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or [])),
            )
            if keep(lead):
                out.append(lead)
        token = data.get("nextPageToken")
        if not token or len(out) >= want:
            break
    return out


# ---- Legacy Places API (fallback) ----

def _search_legacy(query: str, key: str, want: int, keep, region: str = "", language: str = "") -> list[dict]:
    candidates: list[dict] = []
    token = None
    for _ in range(MAX_PAGES):
        params = {"query": query, "key": key}
        if region:
            params["region"] = region.lower()
        if language:
            params["language"] = language
        if token:
            params["pagetoken"] = token
            time.sleep(2)  # legacy page tokens need ~2s to become valid
        resp = requests.get(f"{LEGACY_API}/textsearch/json", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            raise PlacesError(f"Places API error: {data.get('status')} — {data.get('error_message', '')}")
        for p in data.get("results", []):
            if p.get("business_status") in CLOSED_STATUSES or not p.get("place_id"):
                continue
            candidates.append(p)
        token = data.get("next_page_token")
        if not token or len(candidates) >= want * 2:
            break

    def details(p: dict) -> dict | None:
        try:
            r = requests.get(
                f"{LEGACY_API}/details/json", timeout=TIMEOUT,
                params={
                    "place_id": p["place_id"], "key": key,
                    "fields": "name,formatted_phone_number,international_phone_number,formatted_address,website,url",
                },
            )
            r.raise_for_status()
            d = r.json().get("result", {})
        except Exception:
            return None
        website = d.get("website", "") or ""
        types = p.get("types") or []
        return _lead(
            place_id=p["place_id"],
            name=d.get("name", p.get("name", "")),
            phone=d.get("formatted_phone_number", ""),
            intl_phone=d.get("international_phone_number", ""),
            address=d.get("formatted_address", p.get("formatted_address", "")),
            website=website,
            web_presence=classify_website(website),
            category=types[0].replace("_", " ").title() if types else "",
            rating=p.get("rating"),
            reviews=p.get("user_ratings_total", 0),
            maps_url=d.get("url", ""),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        leads = [lead for lead in pool.map(details, candidates) if lead]
    return [lead for lead in leads if keep(lead)]


# ---- serper.dev ----

def _search_serper(query: str, key: str, want: int, keep, region: str = "", language: str = "") -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    ll = ""  # map viewport from page 1; Serper requires it for page > 1
    for page in range(1, MAX_PAGES + 1):
        body = {"q": query}
        if region:
            body["gl"] = region.lower()
        if language:
            body["hl"] = language
        if page > 1:
            if not ll:
                break
            body["page"] = page
            body["ll"] = ll
        try:
            resp = requests.post(SERPER_API, json=body, timeout=TIMEOUT,
                                 headers={"X-API-KEY": key, "Content-Type": "application/json"})
        except requests.RequestException as e:
            raise PlacesError(f"Could not reach Serper: {e}")
        if resp.status_code in (401, 403):
            raise PlacesError("Serper rejected the API key — check SERPER_API_KEY")
        if resp.status_code in (402, 429):
            raise PlacesError("Serper says you're out of credits or rate-limited — check serper.dev dashboard")
        if not resp.ok:
            raise PlacesError(f"Serper error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        ll = ll or data.get("ll") or ""
        found = data.get("places") or []
        new = 0
        for p in found:
            cid = str(p.get("cid") or "")
            place_id = p.get("placeId") or (f"cid:{cid}" if cid else "")
            if not place_id or place_id in seen:
                continue
            seen.add(place_id)
            new += 1
            website = p.get("website") or ""
            phone = p.get("phoneNumber") or ""
            lead = _lead(
                place_id=place_id,
                name=p.get("title", ""),
                phone=phone,
                intl_phone=phone if phone.startswith("+") else "",
                address=p.get("address", ""),
                website=website,
                web_presence=classify_website(website),
                category=p.get("type") or ", ".join((p.get("types") or [])[:1]),
                rating=p.get("rating"),
                reviews=p.get("ratingCount") or 0,
                maps_url=f"https://maps.google.com/?cid={cid}" if cid else "",
                photo_url=large_photo_url(p.get("thumbnailUrl") or ""),
                hours=hours_json(list((p.get("openingHours") or {}).items())),
                booking_url=next((u for u in p.get("bookingLinks") or [] if isinstance(u, str)
                                  and u.startswith("https://")), ""),
            )
            if keep(lead):
                out.append(lead)
        if not new or len(out) >= want:
            break
    return out


def _enrich_emails(leads: list[dict], location: str) -> None:
    if os.getenv("EMAIL_LOOKUP", "on").lower() in ("off", "0", "false", "no"):
        return

    def work(lead: dict) -> None:
        try:
            lead["email"] = find_business_email(lead.get("name", ""), location, website=lead.get("website", ""))
        except Exception:
            lead["email"] = ""

    if leads:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, leads))
        log.info("Email lookup: %d/%d found in %.0fs", sum(1 for lead in leads if lead["email"]), len(leads),
                 time.monotonic() - started)


def _run_query(source: str, query: str, want: int, keep, region: str, language: str) -> list[dict]:
    if source == "serper":
        return _search_serper(query, _key("SERPER_API_KEY"), want, keep, region, language)
    mode = os.getenv("PLACES_API_MODE", "auto").lower()
    key = _key("GOOGLE_PLACES_API_KEY")
    if mode == "legacy":
        return _search_legacy(query, key, want, keep, region, language)
    try:
        return _search_new(query, key, want, keep, region, language)
    except _NewApiUnavailable as e:
        if mode == "new":
            raise PlacesError(f"Places API (New) not enabled for this key: {e}")
        log.warning("Places API (New) unavailable, falling back to legacy (costly): %s", e)
        return _search_legacy(query, key, want, keep, region, language)


def search_queries(
    queries: list[str],
    max_results: int = 30,
    include_social: bool = True,
    exclude_place_ids: set[str] | frozenset[str] = frozenset(),
    region: str = "",
    language: str = "",
    email_location: str = "",
    enrich_emails: bool = True,
    progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """Run several Maps queries (e.g. one per city), merge them, and return the best
    `max_results` open businesses with no website (optionally: social-only), best first.
    `region` (ISO country) and `language` bias results and let local numbers become
    international so WhatsApp links work."""
    source = provider()
    report = progress or (lambda _msg: None)
    allowed = {"none", "social"} if include_social else {"none"}
    collected: dict[str, dict] = {}

    def keep(lead: dict) -> bool:
        pid = lead["place_id"]
        return lead["web_presence"] in allowed and pid not in exclude_place_ids and pid not in collected

    # One query can fill the whole order; with a fan-out each city only needs its share
    # (plus slack, since the best leads are picked across all cities afterwards).
    per_query = max_results if len(queries) == 1 else max(10, math.ceil(max_results * 1.5 / len(queries)))
    errors: list[Exception] = []
    for i, query in enumerate(queries, 1):
        report(f"Scanning Google Maps: {query}" + (f" ({i}/{len(queries)})" if len(queries) > 1 else "") + "…")
        try:
            found = _run_query(source, query, per_query, keep, region, language)
        except PlacesError as e:
            log.warning("Query %r failed: %s", query, e)
            errors.append(e)
            continue
        for lead in found:
            collected.setdefault(lead["place_id"], lead)
    if errors and len(errors) == len(queries):
        raise errors[-1]

    leads = list(collected.values())
    for lead in leads:
        if not lead["intl_phone"]:
            lead["intl_phone"] = to_international(lead["phone"], region)
        lead["score"] = lead_score(lead)
    # Best leads first so a small max_results gets the cream, then enrich only those.
    leads.sort(key=lambda lead: lead["score"], reverse=True)
    leads = leads[:max_results]
    if enrich_emails and leads:
        report(f"Found {len(leads)} without a website — hunting for emails…")
        _enrich_emails(leads, email_location)
    for lead in leads:
        lead["score"] = lead_score(lead)
    leads.sort(key=lambda lead: lead["score"], reverse=True)
    return leads


def search_without_website(
    niche: str,
    location: str,
    max_results: int = 30,
    include_social: bool = True,
    exclude_place_ids: set[str] | frozenset[str] = frozenset(),
    enrich_emails: bool = True,
    progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """Single '<niche> in <location>' search (used when no LLM is configured)."""
    return search_queries([f"{niche} in {location}"], max_results, include_social, exclude_place_ids,
                          email_location=location, enrich_emails=enrich_emails, progress=progress)
