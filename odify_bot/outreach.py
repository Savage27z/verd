"""Outreach message template + WhatsApp links."""
from urllib.parse import quote

from . import store

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
