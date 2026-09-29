"""Outreach message template + WhatsApp links."""
import hashlib
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from phonenumbers import timezone as phone_tz

from . import llm, store

DEFAULT_TEMPLATE = (
    "Hi {name}! I found you on Google Maps and noticed you don't have a website yet. "
    "I build fast, affordable websites that help local businesses get found on Google and win more "
    "customers. Can I send you a free mockup of what yours could look like?"
)
TEMPLATE_VARS = ("{name}", "{category}", "{rating}", "{reviews}")


def get_template() -> str:
    return store.get_setting("template") or DEFAULT_TEMPLATE


def set_template(text: str | None):
    store.set_setting("template", text if text and text != DEFAULT_TEMPLATE else None)


async def template_for_language(language: str) -> str:
    """The current pitch in `language`. Translated once per (pitch, language) and cached, so
    repeated searches in the same country cost no LLM calls; editing the pitch re-translates."""
    template = get_template()
    if not language or language == "en" or not llm.enabled():
        return template
    key = f"pitch:{language}:{hashlib.sha1(template.encode()).hexdigest()[:12]}"
    cached = store.get_setting(key)
    if cached:
        return cached
    translated = await llm.localise_pitch(template, language)
    if translated != template:  # don't cache failures; try again next search
        store.set_setting(key, translated)
    return translated


def lead_local_time(lead: dict, now: datetime) -> datetime | None:
    """Their wall-clock time, from the phone number's region."""
    try:
        zones = [z for z in phone_tz.time_zones_for_number(phonenumbers.parse(lead.get("intl_phone") or ""))
                 if "/" in z and not z.startswith("Etc/")]
        return now.astimezone(ZoneInfo(zones[0])) if zones else None
    except (phonenumbers.NumberParseException, ZoneInfoNotFoundError, ValueError):
        return None


def lead_language(lead: dict) -> str:
    """Language of the search the lead came from (drives previews and follow-up text)."""
    search = store.get_search(lead["search_id"]) if lead.get("search_id") else None
    return (search or {}).get("options", {}).get("language") or "en"


def fill(template: str, lead: dict) -> str:
    return (template
            .replace("{name}", lead.get("name") or "")
            .replace("{category}", (lead.get("category") or "").lower())
            .replace("{rating}", str(lead["rating"]) if lead.get("rating") else "")
            .replace("{reviews}", str(lead.get("reviews") or 0)))


def whatsapp_number(lead: dict) -> str | None:
    """wa.me needs the country code, so only trust the international form."""
    src = lead.get("intl_phone") or (lead.get("phone", "") if lead.get("phone", "").strip().startswith("+") else "")
    digits = "".join(c for c in src if c.isdigit())
    return digits if len(digits) >= 8 else None


def whatsapp_link(lead: dict, template: str | None = None) -> str | None:
    n = whatsapp_number(lead)
    if not n:
        return None
    return f"https://wa.me/{n}?text={quote(fill(template or get_template(), lead))}"
