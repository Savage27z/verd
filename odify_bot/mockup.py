"""One-page website previews for leads — built from lead data only (no LLM, no external assets)."""
import hashlib
import html
import re
from datetime import date
from urllib.parse import quote, urlparse

from . import outreach

# Fixed UI text per language. Anything missing falls back to English.
STRINGS = {
    "en": {"banner": "Free website preview for {name}, made by {brand}", "call": "Call us", "whatsapp": "WhatsApp",
           "book": "Book online", "follow": "Follow us", "directions": "Get directions",
           "reviews": "{rating} ★ from {reviews} Google reviews", "welcome": "Welcome",
           "about": "Visit {name} in {city}, trusted by {reviews} happy customers on Google.",
           "about_new": "Visit {name} in {city}. We look forward to seeing you.", "find": "Find us",
           "contact": "Get in touch", "footer": "Website preview by {brand}",
           "send": "Hi {name}! Here's the free website preview I made for you: {url}"},
    "pl": {"banner": "Darmowy podgląd strony dla {name}, przygotowany przez {brand}", "call": "Zadzwoń",
           "whatsapp": "WhatsApp", "book": "Zarezerwuj online", "follow": "Obserwuj nas",
           "directions": "Wyznacz trasę", "reviews": "{rating} ★ z {reviews} opinii Google", "welcome": "Witamy",
           "about": "Odwiedź {name} w {city}. Zaufało nam już {reviews} zadowolonych klientów w Google.",
           "about_new": "Odwiedź {name} w {city}. Czekamy na Ciebie!", "find": "Jak do nas trafić",
           "contact": "Kontakt", "footer": "Podgląd strony przygotowany przez {brand}",
           "send": "Cześć {name}! Oto darmowy podgląd strony, który dla Was przygotowałem: {url}"},
    "de": {"banner": "Kostenlose Website-Vorschau für {name}, erstellt von {brand}", "call": "Anrufen",
           "whatsapp": "WhatsApp", "book": "Online buchen", "follow": "Folge uns", "directions": "Route planen",
           "reviews": "{rating} ★ aus {reviews} Google-Bewertungen", "welcome": "Willkommen",
           "about": "Besuchen Sie {name} in {city}, bereits {reviews} zufriedene Kunden auf Google.",
           "about_new": "Besuchen Sie {name} in {city}. Wir freuen uns auf Sie.", "find": "So finden Sie uns",
           "contact": "Kontakt", "footer": "Website-Vorschau von {brand}",
           "send": "Hallo {name}! Hier ist die kostenlose Website-Vorschau, die ich für Sie erstellt habe: {url}"},
    "es": {"banner": "Vista previa gratuita de la web de {name}, hecha por {brand}", "call": "Llámanos",
           "whatsapp": "WhatsApp", "book": "Reserva online", "follow": "Síguenos", "directions": "Cómo llegar",
           "reviews": "{rating} ★ en {reviews} reseñas de Google", "welcome": "Bienvenidos",
           "about": "Visita {name} en {city}, con {reviews} clientes satisfechos en Google.",
           "about_new": "Visita {name} en {city}. ¡Te esperamos!", "find": "Dónde estamos",
           "contact": "Contacto", "footer": "Vista previa de la web por {brand}",
           "send": "¡Hola {name}! Aquí tienes la vista previa gratuita de la web que preparé para ti: {url}"},
    "pt": {"banner": "Prévia gratuita do site de {name}, feita por {brand}", "call": "Ligue para nós",
           "whatsapp": "WhatsApp", "book": "Agende online", "follow": "Siga-nos", "directions": "Como chegar",
           "reviews": "{rating} ★ em {reviews} avaliações no Google", "welcome": "Bem-vindo",
           "about": "Visite {name} em {city}, com {reviews} clientes satisfeitos no Google.",
           "about_new": "Visite {name} em {city}. Esperamos por você!", "find": "Onde estamos",
           "contact": "Contato", "footer": "Prévia do site por {brand}",
           "send": "Oi {name}! Aqui está a prévia gratuita do site que fiz para você: {url}"},
    "fr": {"banner": "Aperçu gratuit du site de {name}, réalisé par {brand}", "call": "Appelez-nous",
           "whatsapp": "WhatsApp", "book": "Réserver en ligne", "follow": "Suivez-nous", "directions": "Itinéraire",
           "reviews": "{rating} ★ sur {reviews} avis Google", "welcome": "Bienvenue",
           "about": "Venez chez {name} à {city}, déjà {reviews} clients satisfaits sur Google.",
           "about_new": "Venez chez {name} à {city}. Au plaisir de vous accueillir.", "find": "Nous trouver",
           "contact": "Contact", "footer": "Aperçu du site par {brand}",
           "send": "Bonjour {name} ! Voici l'aperçu gratuit du site que j'ai préparé pour vous : {url}"},
    "it": {"banner": "Anteprima gratuita del sito di {name}, realizzata da {brand}", "call": "Chiamaci",
           "whatsapp": "WhatsApp", "book": "Prenota online", "follow": "Seguici", "directions": "Indicazioni",
           "reviews": "{rating} ★ su {reviews} recensioni Google", "welcome": "Benvenuti",
           "about": "Vieni da {name} a {city}, già {reviews} clienti soddisfatti su Google.",
           "about_new": "Vieni da {name} a {city}. Ti aspettiamo!", "find": "Dove siamo",
           "contact": "Contatti", "footer": "Anteprima del sito di {brand}",
           "send": "Ciao {name}! Ecco l'anteprima gratuita del sito che ho preparato per te: {url}"},
    "nl": {"banner": "Gratis websitevoorbeeld voor {name}, gemaakt door {brand}", "call": "Bel ons",
           "whatsapp": "WhatsApp", "book": "Online boeken", "follow": "Volg ons", "directions": "Route",
           "reviews": "{rating} ★ uit {reviews} Google-reviews", "welcome": "Welkom",
           "about": "Bezoek {name} in {city}, al {reviews} tevreden klanten op Google.",
           "about_new": "Bezoek {name} in {city}. We zien je graag.", "find": "Waar vind je ons",
           "contact": "Contact", "footer": "Websitevoorbeeld door {brand}",
           "send": "Hoi {name}! Hier is het gratis websitevoorbeeld dat ik voor je heb gemaakt: {url}"},
}

# Category keyword → emoji. First match wins; checked against the lead's category + name.
ICONS = [
    (("barber", "fryzjer męski", "friseur", "barbier", "barbearia", "barbería"), "💈"),
    (("hair", "salon", "fryzjer", "coiffeur", "peluquer", "cabeleireir", "kapper"), "💇"),
    (("nail", "manicure", "paznok"), "💅"), (("beauty", "kosmetyk", "spa", "massage", "masaż"), "💆"),
    (("dent", "zahn", "stomatolog", "odonto"), "🦷"), (("doctor", "clinic", "klinik", "przychodnia", "medic"), "🩺"),
    (("vet", "weteryn", "tierarzt"), "🐾"), (("pharm", "apteka", "apotheke"), "💊"),
    (("restaurant", "restaurac", "ristorante", "food", "grill", "pizza", "kebab", "sushi", "burger"), "🍽️"),
    (("cafe", "café", "coffee", "kawiar", "kaffee"), "☕"), (("bakery", "piekarn", "bäckerei", "padaria", "panader"), "🥐"),
    (("bar", "pub"), "🍻"), (("hotel", "hostel", "guest", "pensjonat"), "🛏️"),
    (("car", "auto", "mechani", "warsztat", "werkstatt", "oficina", "taller", "tyre", "tire", "opony"), "🚗"),
    (("wash", "myjnia", "lavado", "lavagem"), "🧽"), (("clean", "sprząt", "reinig", "limpieza", "limpeza"), "🧹"),
    (("plumb", "hydraul", "klempner", "encanador", "fontaner"), "🔧"),
    (("electric", "elektryk", "elektriker", "eletricista", "electricista"), "💡"),
    (("build", "construct", "budow", "bau", "roof", "dach"), "🏗️"), (("gym", "fitness", "siłown", "yoga"), "🏋️"),
    (("flor", "kwiac", "blumen", "flower"), "💐"), (("tattoo", "tatu"), "🖋️"), (("photo", "foto"), "📷"),
    (("law", "prawn", "anwalt", "advog", "abogad"), "⚖️"), (("account", "księgow", "steuer", "contab"), "📊"),
    (("school", "szkoł", "schule", "escola", "tutor", "lesson"), "🎓"), (("pet", "dog", "groom"), "🐶"),
    (("shop", "store", "sklep", "laden", "loja", "tienda"), "🛍️"),
]

# (primary, deep) colour pairs; each business gets one deterministically from its name.
PALETTES = [("#0f766e", "#134e4a"), ("#1d4ed8", "#1e3a8a"), ("#b45309", "#78350f"), ("#be123c", "#881337"),
            ("#7c3aed", "#4c1d95"), ("#15803d", "#14532d"), ("#0e7490", "#164e63"), ("#374151", "#111827")]

BOOKING_HOSTS = ("booksy.", "fresha.", "treatwell.", "vagaro.", "styleseat.", "setmore.", "simplybook.",
                 "calendly.", "planity.", "doctolib.", "znanylekarz.", "doctoralia.", "zocdoc.")


def strings(language: str) -> dict:
    return {**STRINGS["en"], **STRINGS.get((language or "en").lower(), {})}


def icon_for(lead: dict) -> str:
    text = f"{lead.get('category', '')} {lead.get('name', '')}".lower()
    for keywords, emoji in ICONS:
        if any(k in text for k in keywords):
            return emoji
    return "✨"


def palette_for(lead: dict) -> tuple[str, str]:
    h = int(hashlib.sha1((lead.get("name") or "").encode()).hexdigest(), 16)
    return PALETTES[h % len(PALETTES)]


def city_from_address(address: str, fallback: str = "") -> str:
    """'ul. Długa 5, 31-147 Kraków, Poland' -> 'Kraków'. Skips the street (first part) and the
    country (last part), then takes the first remaining part that has words without digits."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    middle = parts[1:-1] if len(parts) >= 3 else parts[1:]
    for part in middle:
        words = [w for w in re.split(r"[\s-]+", part) if w and not re.search(r"\d", w)]
        if words:
            return " ".join(words)
    return fallback.split(",")[0].strip() if fallback else ""


def _link_last(text: str, brand: str, brand_html: str) -> str:
    """Escape `text` and turn the last occurrence of the brand name into a link."""
    escaped, eb = html.escape(text), html.escape(brand)
    head, sep, tail = escaped.rpartition(eb)
    return f"{head}{brand_html}{tail}" if sep else escaped


def link_kind(url: str) -> str | None:
    if not url:
        return None
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    return "book" if any(b in host for b in BOOKING_HOSTS) else "follow"


def send_text(lead: dict, url: str, language: str) -> str:
    return strings(language)["send"].format(name=lead.get("name") or "", url=url)


def send_link(lead: dict, url: str, language: str) -> str | None:
    n = outreach.whatsapp_number(lead)
    return f"https://wa.me/{n}?text={quote(send_text(lead, url, language))}" if n else None


def render(lead: dict, language: str = "en", brand: str = "Odify", brand_url: str = "", fallback_city: str = "") -> str:
    t = strings(language)
    e = html.escape
    name = lead.get("name") or "Your business"
    city = city_from_address(lead.get("address", ""), fallback_city)
    rating, reviews = lead.get("rating"), int(lead.get("reviews") or 0)
    primary, deep = palette_for(lead)
    phone = lead.get("intl_phone") or lead.get("phone") or ""
    tel = "tel:" + re.sub(r"[^\d+]", "", phone) if phone else ""
    wa = outreach.whatsapp_number(lead)
    wa_url = f"https://wa.me/{wa}" if wa else ""
    kind = link_kind(lead.get("website", "")) if lead.get("web_presence") == "social" else None
    maps_url = lead.get("maps_url") or f"https://www.google.com/maps/search/?api=1&query={quote(name + ' ' + lead.get('address', ''))}"
    embed = f"https://maps.google.com/maps?q={quote(name + ', ' + lead.get('address', ''))}&output=embed"
    fmt = {"name": name, "brand": brand, "city": city or "", "rating": rating, "reviews": reviews}

    buttons = []
    if tel:
        buttons.append(f'<a class="btn primary" href="{e(tel)}">📞 {e(t["call"])}</a>')
    if wa_url:
        buttons.append(f'<a class="btn wa" href="{e(wa_url)}">💬 {e(t["whatsapp"])}</a>')
    if kind:
        buttons.append(f'<a class="btn ghost" href="{e(lead["website"])}" rel="noopener">'
                       f'{"📅" if kind == "book" else "📱"} {e(t[kind])}</a>')
    buttons.append(f'<a class="btn ghost" href="{e(maps_url)}" rel="noopener">📍 {e(t["directions"])}</a>')

    rating_html = ""
    if rating:
        stars = "★" * int(round(float(rating))) + "☆" * (5 - int(round(float(rating))))
        rating_html = (f'<div class="rating"><span class="stars">{stars}</span> '
                       f'{e(t["reviews"].format(**fmt))}</div>')
    about = (t["about"] if reviews >= 5 else t["about_new"]).format(**fmt) if city else ""
    sub = " · ".join(x for x in (lead.get("category") or "", city) if x)
    brand_html = f'<a href="{e(brand_url)}">{e(brand)}</a>' if brand_url else e(brand)
    banner = _link_last(t["banner"].format(**fmt), brand, brand_html)
    footer = _link_last(t["footer"].format(**fmt), brand, brand_html)

    return f"""<!doctype html>
<html lang="{e(language or 'en')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{e(name)}</title>
<meta property="og:title" content="{e(name)}">
<meta property="og:description" content="{e(sub)}">
<style>
:root {{ --p: {primary}; --d: {deep}; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #1f2937; background: #f8fafc; }}
a {{ color: inherit; }}
.banner {{ background: #111827; color: #f9fafb; text-align: center; font-size: 13px; padding: 8px 16px; }}
.banner a {{ color: #fbbf24; }}
header {{ background: linear-gradient(135deg, var(--p), var(--d)); color: #fff; padding: 56px 20px 64px; text-align: center; }}
.icon {{ font-size: 56px; line-height: 1; }}
h1 {{ font-size: clamp(30px, 7vw, 48px); line-height: 1.15; margin: 14px auto 6px; max-width: 760px; overflow-wrap: anywhere; }}
.sub {{ opacity: .9; font-size: 18px; }}
.rating {{ display: inline-block; margin-top: 16px; background: rgba(255,255,255,.15); border-radius: 999px; padding: 6px 14px; font-size: 15px; }}
.stars {{ color: #fbbf24; letter-spacing: 1px; }}
.cta {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-top: 26px; }}
.btn {{ display: inline-block; text-decoration: none; font-weight: 600; padding: 12px 20px; border-radius: 12px; }}
.btn.primary {{ background: #fff; color: var(--d); }}
.btn.wa {{ background: #22c55e; color: #fff; }}
.btn.ghost {{ border: 1.5px solid rgba(255,255,255,.7); color: #fff; }}
main {{ max-width: 860px; margin: -28px auto 0; padding: 0 16px 40px; }}
section {{ background: #fff; border-radius: 16px; box-shadow: 0 6px 24px rgba(15,23,42,.06); padding: 26px; margin-bottom: 18px; }}
h2 {{ margin: 0 0 10px; font-size: 22px; color: var(--d); }}
.map {{ width: 100%; height: 300px; border: 0; border-radius: 12px; margin-top: 10px; }}
.contact a {{ display: block; margin: 6px 0; font-weight: 600; color: var(--p); text-decoration: none; overflow-wrap: anywhere; }}
footer {{ text-align: center; color: #6b7280; font-size: 13px; padding: 10px 16px 90px; }}
.sticky {{ position: fixed; left: 0; right: 0; bottom: 0; display: flex; gap: 8px; padding: 10px; background: rgba(255,255,255,.95); box-shadow: 0 -4px 16px rgba(15,23,42,.08); }}
.sticky a {{ flex: 1; text-align: center; }}
.sticky .btn.primary {{ background: var(--p); color: #fff; }}
@media (min-width: 720px) {{ .sticky {{ display: none; }} footer {{ padding-bottom: 24px; }} }}
</style>
</head>
<body>
<div class="banner">{banner}</div>
<header>
  <div class="icon">{icon_for(lead)}</div>
  <h1>{e(name)}</h1>
  <div class="sub">{e(sub)}</div>
  {rating_html}
  <div class="cta">{"".join(buttons)}</div>
</header>
<main>
  {f'<section><h2>{e(t["welcome"])}</h2><p>{e(about)}</p></section>' if about else ""}
  <section>
    <h2>{e(t["find"])}</h2>
    <p>{e(lead.get("address") or city)}</p>
    <iframe class="map" src="{e(embed)}" loading="lazy" referrerpolicy="no-referrer-when-downgrade" title="map"></iframe>
  </section>
  <section class="contact">
    <h2>{e(t["contact"])}</h2>
    {f'<a href="{e(tel)}">📞 {e(phone)}</a>' if tel else ""}
    {f'<a href="{e(wa_url)}">💬 WhatsApp</a>' if wa_url else ""}
    {f'<a href="mailto:{e(lead["email"])}">✉️ {e(lead["email"])}</a>' if lead.get("email") else ""}
    {f'<a href="{e(lead["website"])}" rel="noopener">🔗 {e(t[kind])}</a>' if kind else ""}
  </section>
</main>
<footer>© {date.today().year} {e(name)} · {footer}</footer>
<div class="sticky">
  {f'<a class="btn primary" href="{e(tel)}">📞 {e(t["call"])}</a>' if tel else ""}
  {f'<a class="btn wa" href="{e(wa_url)}">💬 {e(t["whatsapp"])}</a>' if wa_url else ""}
</div>
</body>
</html>"""
