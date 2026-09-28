"""Find businesses on Google Maps that have no real website.

Primary path is Places API (New) Text Search: one request returns up to 20 places *with*
website + phone, so a 60-place scan costs ≤3 requests. The legacy API needed a separate
Place Details call per place (up to ~60 billable calls per search), and is kept only as a
fallback for keys that don't have "Places API (New)" enabled.
"""
import logging
import math
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from .emails import find_business_email

log = logging.getLogger("odify.places")

NEW_API = "https://places.googleapis.com/v1/places:searchText"
LEGACY_API = "https://maps.googleapis.com/maps/api/place"
TIMEOUT = 15
MAX_PAGES = 3  # Google caps text search at 60 results either way

NEW_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.websiteUri",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.rating",
    "places.userRatingCount", "places.googleMapsUri", "places.businessStatus",
    "places.primaryTypeDisplayName", "nextPageToken",
])

# A "website" on one of these hosts is a social/profile page, not a site the business owns.
# Those businesses are still great leads (they're active online but have no site).
SOCIAL_HOSTS = (
    "facebook.com", "fb.com", "fb.me", "instagram.com", "linktr.ee", "wa.me", "whatsapp.com",
    "api.whatsapp.com", "twitter.com", "x.com", "tiktok.com", "linkedin.com", "youtube.com",
    "t.me", "business.site", "g.page", "snapchat.com", "pinterest.com", "yelp.com",
    "bio.link", "beacons.ai", "taplink.cc", "linkin.bio",
)

CLOSED_STATUSES = ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")


class PlacesError(RuntimeError):
    pass


class _NewApiUnavailable(Exception):
    pass


def _api_key() -> str:
    key = os.getenv("GOOGLE_PLACES_API_KEY", "")
    if not key:
        raise PlacesError("GOOGLE_PLACES_API_KEY not set in .env")
    return key


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


def _lead(**kw) -> dict:
    base = {
        "place_id": "", "name": "", "phone": "", "intl_phone": "", "address": "", "email": "",
        "website": "", "web_presence": "none", "category": "", "rating": None, "reviews": 0,
        "maps_url": "",
    }
    base.update({k: v for k, v in kw.items() if v is not None})
    return base


# ---- Places API (New) ----

def _search_new(query: str, key: str, want: int, keep) -> list[dict]:
    out: list[dict] = []
    token = None
    for page in range(MAX_PAGES):
        body = {"textQuery": query, "pageSize": 20}
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
            )
            if keep(lead):
                out.append(lead)
        token = data.get("nextPageToken")
        if not token or len(out) >= want:
            break
    return out


# ---- Legacy Places API (fallback) ----

def _search_legacy(query: str, key: str, want: int, keep) -> list[dict]:
    candidates: list[dict] = []
    token = None
    for _ in range(MAX_PAGES):
        params = {"query": query, "key": key}
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


def _enrich_emails(leads: list[dict], location: str) -> None:
    def work(lead: dict) -> None:
        try:
            lead["email"] = find_business_email(lead.get("name", ""), location, website=lead.get("website", ""))
        except Exception:
            lead["email"] = ""

    if leads:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, leads))


def search_without_website(
    niche: str,
    location: str,
    max_results: int = 30,
    include_social: bool = True,
    exclude_place_ids: set[str] | frozenset[str] = frozenset(),
    enrich_emails: bool = True,
    progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """Up to `max_results` open businesses with no website (optionally: social-only),
    skipping places in `exclude_place_ids`, sorted best lead first."""
    key = _api_key()
    report = progress or (lambda _msg: None)
    report("Scanning Google Maps…")
    query = f"{niche} in {location}"
    allowed = {"none", "social"} if include_social else {"none"}

    def keep(lead: dict) -> bool:
        return lead["web_presence"] in allowed and lead["place_id"] not in exclude_place_ids

    mode = os.getenv("PLACES_API_MODE", "auto").lower()
    if mode == "legacy":
        leads = _search_legacy(query, key, max_results, keep)
    else:
        try:
            leads = _search_new(query, key, max_results, keep)
        except _NewApiUnavailable as e:
            if mode == "new":
                raise PlacesError(f"Places API (New) not enabled for this key: {e}")
            log.warning("Places API (New) unavailable, falling back to legacy (costly): %s", e)
            leads = _search_legacy(query, key, max_results, keep)

    # Best leads first so a small max_results gets the cream, then enrich only those.
    for lead in leads:
        lead["score"] = lead_score(lead)
    leads.sort(key=lambda lead: lead["score"], reverse=True)
    leads = leads[:max_results]
    if enrich_emails and leads:
        report(f"Found {len(leads)} without a website — hunting for emails…")
        _enrich_emails(leads, location)
    for lead in leads:
        lead["score"] = lead_score(lead)
    leads.sort(key=lambda lead: lead["score"], reverse=True)
    return leads
