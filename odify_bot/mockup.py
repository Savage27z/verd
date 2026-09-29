"""One-page website previews for leads — built from lead data only (no LLM, no external assets)."""
import hashlib
import html
import json
import re
from datetime import date, datetime, timezone
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
           "send": "Hi {name}! Here's the free website preview I made for you: {url}",
           "followup": 'Hi {name}! Just following up on my message. Would you like to see the free website preview I made for you?',
           "followup_link": "Hi {name}! Just following up. Here's the free website preview I made for you, no strings attached: {url}"},
    "pl": {"banner": "Darmowy podgląd strony dla {name}, przygotowany przez {brand}", "call": "Zadzwoń",
           "whatsapp": "WhatsApp", "book": "Zarezerwuj online", "follow": "Obserwuj nas",
           "directions": "Wyznacz trasę", "reviews": "{rating} ★ z {reviews} opinii Google", "welcome": "Witamy",
           "about": "Odwiedź {name} w {city}. Zaufało nam już {reviews} zadowolonych klientów w Google.",
           "about_new": "Odwiedź {name} w {city}. Czekamy na Ciebie!", "find": "Jak do nas trafić",
           "contact": "Kontakt", "footer": "Podgląd strony przygotowany przez {brand}",
           "send": "Cześć {name}! Oto darmowy podgląd strony, który dla Was przygotowałem: {url}",
           "followup": 'Cześć {name}! Wracam do mojej wiadomości. Chcecie zobaczyć darmowy podgląd strony, który dla Was przygotowałem?',
           "followup_link": 'Cześć {name}! Wracam do mojej wiadomości. Oto darmowy podgląd strony, który dla Was przygotowałem, bez żadnych zobowiązań: {url}'},
    "de": {"banner": "Kostenlose Website-Vorschau für {name}, erstellt von {brand}", "call": "Anrufen",
           "whatsapp": "WhatsApp", "book": "Online buchen", "follow": "Folge uns", "directions": "Route planen",
           "reviews": "{rating} ★ aus {reviews} Google-Bewertungen", "welcome": "Willkommen",
           "about": "Besuchen Sie {name} in {city}, bereits {reviews} zufriedene Kunden auf Google.",
           "about_new": "Besuchen Sie {name} in {city}. Wir freuen uns auf Sie.", "find": "So finden Sie uns",
           "contact": "Kontakt", "footer": "Website-Vorschau von {brand}",
           "send": "Hallo {name}! Hier ist die kostenlose Website-Vorschau, die ich für Sie erstellt habe: {url}",
           "followup": 'Hallo {name}! Ich wollte kurz nachhaken. Möchten Sie die kostenlose Website-Vorschau sehen, die ich für Sie erstellt habe?',
           "followup_link": 'Hallo {name}! Ich wollte kurz nachhaken. Hier ist die kostenlose Website-Vorschau, die ich für Sie erstellt habe, ganz unverbindlich: {url}'},
    "es": {"banner": "Vista previa gratuita de la web de {name}, hecha por {brand}", "call": "Llámanos",
           "whatsapp": "WhatsApp", "book": "Reserva online", "follow": "Síguenos", "directions": "Cómo llegar",
           "reviews": "{rating} ★ en {reviews} reseñas de Google", "welcome": "Bienvenidos",
           "about": "Visita {name} en {city}, con {reviews} clientes satisfechos en Google.",
           "about_new": "Visita {name} en {city}. ¡Te esperamos!", "find": "Dónde estamos",
           "contact": "Contacto", "footer": "Vista previa de la web por {brand}",
           "send": "¡Hola {name}! Aquí tienes la vista previa gratuita de la web que preparé para ti: {url}",
           "followup": '¡Hola {name}! Te escribo de nuevo por mi mensaje. ¿Te gustaría ver la vista previa gratuita de la web que preparé para ti?',
           "followup_link": '¡Hola {name}! Te escribo de nuevo. Aquí tienes la vista previa gratuita de la web que preparé para ti, sin compromiso: {url}'},
    "pt": {"banner": "Prévia gratuita do site de {name}, feita por {brand}", "call": "Ligue para nós",
           "whatsapp": "WhatsApp", "book": "Agende online", "follow": "Siga-nos", "directions": "Como chegar",
           "reviews": "{rating} ★ em {reviews} avaliações no Google", "welcome": "Bem-vindo",
           "about": "Visite {name} em {city}, com {reviews} clientes satisfeitos no Google.",
           "about_new": "Visite {name} em {city}. Esperamos por você!", "find": "Onde estamos",
           "contact": "Contato", "footer": "Prévia do site por {brand}",
           "send": "Oi {name}! Aqui está a prévia gratuita do site que fiz para você: {url}",
           "followup": 'Oi {name}! Passando para retomar minha mensagem. Quer ver a prévia gratuita do site que fiz para você?',
           "followup_link": 'Oi {name}! Passando para retomar minha mensagem. Aqui está a prévia gratuita do site que fiz para você, sem compromisso: {url}'},
    "fr": {"banner": "Aperçu gratuit du site de {name}, réalisé par {brand}", "call": "Appelez-nous",
           "whatsapp": "WhatsApp", "book": "Réserver en ligne", "follow": "Suivez-nous", "directions": "Itinéraire",
           "reviews": "{rating} ★ sur {reviews} avis Google", "welcome": "Bienvenue",
           "about": "Venez chez {name} à {city}, déjà {reviews} clients satisfaits sur Google.",
           "about_new": "Venez chez {name} à {city}. Au plaisir de vous accueillir.", "find": "Nous trouver",
           "contact": "Contact", "footer": "Aperçu du site par {brand}",
           "send": "Bonjour {name} ! Voici l'aperçu gratuit du site que j'ai préparé pour vous : {url}",
           "followup": "Bonjour {name} ! Je me permets de revenir vers vous. Voulez-vous voir l'aperçu gratuit du site que j'ai préparé pour vous ?",
           "followup_link": "Bonjour {name} ! Je me permets de revenir vers vous. Voici l'aperçu gratuit du site que j'ai préparé pour vous, sans engagement : {url}"},
    "it": {"banner": "Anteprima gratuita del sito di {name}, realizzata da {brand}", "call": "Chiamaci",
           "whatsapp": "WhatsApp", "book": "Prenota online", "follow": "Seguici", "directions": "Indicazioni",
           "reviews": "{rating} ★ su {reviews} recensioni Google", "welcome": "Benvenuti",
           "about": "Vieni da {name} a {city}, già {reviews} clienti soddisfatti su Google.",
           "about_new": "Vieni da {name} a {city}. Ti aspettiamo!", "find": "Dove siamo",
           "contact": "Contatti", "footer": "Anteprima del sito di {brand}",
           "send": "Ciao {name}! Ecco l'anteprima gratuita del sito che ho preparato per te: {url}",
           "followup": "Ciao {name}! Ti riscrivo per il mio messaggio. Vuoi vedere l'anteprima gratuita del sito che ho preparato per te?",
           "followup_link": "Ciao {name}! Ti riscrivo per il mio messaggio. Ecco l'anteprima gratuita del sito che ho preparato per te, senza impegno: {url}"},
    "nl": {"banner": "Gratis websitevoorbeeld voor {name}, gemaakt door {brand}", "call": "Bel ons",
           "whatsapp": "WhatsApp", "book": "Online boeken", "follow": "Volg ons", "directions": "Route",
           "reviews": "{rating} ★ uit {reviews} Google-reviews", "welcome": "Welkom",
           "about": "Bezoek {name} in {city}, al {reviews} tevreden klanten op Google.",
           "about_new": "Bezoek {name} in {city}. We zien je graag.", "find": "Waar vind je ons",
           "contact": "Contact", "footer": "Websitevoorbeeld door {brand}",
           "send": "Hoi {name}! Hier is het gratis websitevoorbeeld dat ik voor je heb gemaakt: {url}",
           "followup": 'Hoi {name}! Ik kom even terug op mijn bericht. Wil je het gratis websitevoorbeeld zien dat ik voor je heb gemaakt?',
           "followup_link": 'Hoi {name}! Ik kom even terug op mijn bericht. Hier is het gratis websitevoorbeeld dat ik voor je heb gemaakt, geheel vrijblijvend: {url}'},
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


# Opening-hours and reviews sections.
SECTION_STRINGS = {
    "en": {"hours": "Opening hours", "today": "Today", "open_now": "Open now · until {time}",
           "closed_now": "Closed now", "reviews_title": "What customers say",
           "reviews_count": "{reviews} reviews on Google", "reviews_button": "Read reviews on Google"},
    "pl": {"hours": "Godziny otwarcia", "today": "Dziś", "open_now": "Otwarte · do {time}",
           "closed_now": "Teraz zamknięte", "reviews_title": "Co mówią klienci",
           "reviews_count": "{reviews} opinii w Google", "reviews_button": "Zobacz opinie w Google"},
    "de": {"hours": "Öffnungszeiten", "today": "Heute", "open_now": "Jetzt geöffnet · bis {time}",
           "closed_now": "Jetzt geschlossen", "reviews_title": "Das sagen unsere Kunden",
           "reviews_count": "{reviews} Bewertungen auf Google", "reviews_button": "Bewertungen auf Google lesen"},
    "es": {"hours": "Horario", "today": "Hoy", "open_now": "Abierto ahora · hasta las {time}",
           "closed_now": "Cerrado ahora", "reviews_title": "Lo que dicen nuestros clientes",
           "reviews_count": "{reviews} reseñas en Google", "reviews_button": "Leer reseñas en Google"},
    "pt": {"hours": "Horário de funcionamento", "today": "Hoje", "open_now": "Aberto agora · até {time}",
           "closed_now": "Fechado agora", "reviews_title": "O que dizem nossos clientes",
           "reviews_count": "{reviews} avaliações no Google", "reviews_button": "Ver avaliações no Google"},
    "fr": {"hours": "Horaires", "today": "Aujourd'hui", "open_now": "Ouvert · jusqu'à {time}",
           "closed_now": "Fermé actuellement", "reviews_title": "L'avis de nos clients",
           "reviews_count": "{reviews} avis sur Google", "reviews_button": "Lire les avis sur Google"},
    "it": {"hours": "Orari di apertura", "today": "Oggi", "open_now": "Aperto ora · fino alle {time}",
           "closed_now": "Chiuso ora", "reviews_title": "Cosa dicono i clienti",
           "reviews_count": "{reviews} recensioni su Google", "reviews_button": "Leggi le recensioni su Google"},
    "nl": {"hours": "Openingstijden", "today": "Vandaag", "open_now": "Nu open · tot {time}",
           "closed_now": "Nu gesloten", "reviews_title": "Wat klanten zeggen",
           "reviews_count": "{reviews} reviews op Google", "reviews_button": "Lees reviews op Google"},
}

# Monday..Sunday, lowercase, as Google writes them in each language.
WEEKDAYS = {
    "en": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    "pl": ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"],
    "de": ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"],
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
    "pt": ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"],
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
    "it": ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"],
    "nl": ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"],
}

_TIME = r"(\d{1,2})(?:[:.](\d{2}))?\s*([AaPp]\.?[Mm]\.?)?"
_RANGE = re.compile(_TIME + r"\s*[–—-]\s*" + _TIME)
_ALL_DAY = re.compile(r"24\s*(hours|h\b|godz|stunden|horas|heures|ore|uur)|całą dobę|24/7", re.IGNORECASE)


def strings(language: str) -> dict:
    lang = (language or "en").lower()
    return {**STRINGS["en"], **SECTION_STRINGS["en"], **STRINGS.get(lang, {}), **SECTION_STRINGS.get(lang, {})}


def weekday_index(day: str) -> int | None:
    """'Wtorek' / 'Tuesday' / 'segunda' -> 0..6 (Monday=0), in any supported language."""
    d = day.strip().lower().rstrip(":")
    if not d:
        return None
    for names in WEEKDAYS.values():
        for i, name in enumerate(names):
            if name == d or name.startswith(d) or d.startswith(name):
                return i
    return None


def parse_intervals(text: str) -> list[tuple[int, int]]:
    """Opening ranges in minutes since midnight. [] = closed that day.
    Handles '10:00–20:00', '9 AM–10 PM', '12–8 PM' (shared AM/PM), split shifts, '24 hours'."""
    if _ALL_DAY.search(text):
        return [(0, 24 * 60)]
    out = []
    for h1, m1, s1, h2, m2, s2 in _RANGE.findall(text):
        s1 = s1 or s2  # "12–8 PM": the start takes the end's AM/PM

        def minutes(h: str, m: str, suffix: str) -> int:
            hour = int(h) % 24
            if suffix:
                hour = hour % 12 + (12 if suffix.lower().startswith("p") else 0)
            return hour * 60 + int(m or 0)

        start, end = minutes(h1, m1, s1), minutes(h2, m2, s2)
        if end <= start:
            end += 24 * 60  # past midnight
        out.append((start, end))
    return out


def load_hours(lead: dict) -> list[tuple[str, str]]:
    """Stored hours as [(day, text)], Monday first when the day names are recognised."""
    try:
        pairs = [(str(d), str(t)) for d, t in json.loads(lead.get("hours") or "[]")]
    except (ValueError, TypeError):
        return []
    idx = [weekday_index(d) for d, _ in pairs]
    if None not in idx and len(set(idx)) == len(idx):
        pairs = [p for _, p in sorted(zip(idx, pairs))]
    return pairs


def open_status(pairs: list[tuple[str, str]], local: datetime) -> tuple[int | None, bool, str]:
    """(index of today's row, open now?, closing time 'HH:MM')."""
    today = next((i for i, (d, _) in enumerate(pairs) if weekday_index(d) == local.weekday()), None)
    if today is None:
        return None, False, ""
    now = local.hour * 60 + local.minute
    for start, end in parse_intervals(pairs[today][1]):
        if start <= now < end:
            return today, True, "24:00" if end - start >= 24 * 60 else f"{end // 60 % 24:02d}:{end % 60:02d}"
    return today, False, ""


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


def render(lead: dict, language: str = "en", brand: str = "Odify", brand_url: str = "", fallback_city: str = "",
           now: datetime | None = None) -> str:
    t = strings(language)
    e = html.escape
    now = now or datetime.now(timezone.utc)
    name = lead.get("name") or "Your business"
    city = city_from_address(lead.get("address", ""), fallback_city)
    rating, reviews = lead.get("rating"), int(lead.get("reviews") or 0)
    primary, deep = palette_for(lead)
    phone = lead.get("intl_phone") or lead.get("phone") or ""
    tel = "tel:" + re.sub(r"[^\d+]", "", phone) if phone else ""
    wa = outreach.whatsapp_number(lead)
    wa_url = f"https://wa.me/{wa}" if wa else ""
    kind = link_kind(lead.get("website", "")) if lead.get("web_presence") == "social" else None
    # A booking link from Google beats a generic social link for the "Book online" button.
    book_url = lead.get("booking_url") or (lead.get("website") if kind == "book" else "")
    follow_url = lead.get("website") if kind == "follow" else ""
    maps_url = lead.get("maps_url") or f"https://www.google.com/maps/search/?api=1&query={quote(name + ' ' + lead.get('address', ''))}"
    embed = f"https://maps.google.com/maps?q={quote(name + ', ' + lead.get('address', ''))}&output=embed"
    photo = lead.get("photo_url") or ""
    photo = photo if photo.startswith("https://") and not re.search(r"[\"'()\\\s]", photo) else ""
    fmt = {"name": name, "brand": brand, "city": city or "", "rating": rating, "reviews": reviews}

    buttons = []
    if tel:
        buttons.append(f'<a class="btn primary" href="{e(tel)}">📞 {e(t["call"])}</a>')
    if wa_url:
        buttons.append(f'<a class="btn wa" href="{e(wa_url)}">💬 {e(t["whatsapp"])}</a>')
    if book_url:
        buttons.append(f'<a class="btn ghost" href="{e(book_url)}" rel="noopener">📅 {e(t["book"])}</a>')
    elif follow_url:
        buttons.append(f'<a class="btn ghost" href="{e(follow_url)}" rel="noopener">📱 {e(t["follow"])}</a>')
    buttons.append(f'<a class="btn ghost" href="{e(maps_url)}" rel="noopener">📍 {e(t["directions"])}</a>')

    stars = ""
    if rating:
        full = int(round(float(rating)))
        stars = "★" * full + "☆" * (5 - full)
    rating_html = (f'<div class="rating"><span class="stars">{stars}</span> {e(t["reviews"].format(**fmt))}</div>'
                   if rating else "")

    # Opening hours, with today's row highlighted and a live open/closed badge in *their* timezone.
    pairs = load_hours(lead)
    hours_html, badge_html = "", ""
    if pairs:
        local = outreach.lead_local_time(lead, now) or now
        today, is_open, closes = open_status(pairs, local)
        rows = "".join(
            f'<tr class="{"today" if i == today else ""}"><th>{e(day.capitalize())}'
            f'{" · " + e(t["today"]) if i == today else ""}</th><td>{e(text)}</td></tr>'
            for i, (day, text) in enumerate(pairs))
        hours_html = f'<section><h2>{e(t["hours"])}</h2><table class="hours">{rows}</table></section>'
        if today is not None:
            badge_html = (f'<div class="badge open">● {e(t["open_now"].format(time=closes))}</div>' if is_open
                          else f'<div class="badge closed">● {e(t["closed_now"])}</div>')

    reviews_html = ""
    if rating and reviews:
        score = f"{float(rating):.1f}"
        reviews_html = (
            f'<section class="reviews"><h2>{e(t["reviews_title"])}</h2>'
            f'<div class="score"><span class="big">{e(score)}</span>'
            f'<div><div class="stars lg">{stars}</div><div>{e(t["reviews_count"].format(**fmt))}</div></div></div>'
            f'<a class="btn outline" href="{e(maps_url)}" rel="noopener">⭐ {e(t["reviews_button"])}</a></section>')

    about = (t["about"] if reviews >= 5 else t["about_new"]).format(**fmt) if city else ""
    sub = " · ".join(x for x in (lead.get("category") or "", city) if x)
    brand_html = f'<a href="{e(brand_url)}">{e(brand)}</a>' if brand_url else e(brand)
    banner = _link_last(t["banner"].format(**fmt), brand, brand_html)
    footer = _link_last(t["footer"].format(**fmt), brand, brand_html)
    header_class = "photo" if photo else ""
    # Single quotes: this goes inside style="…" (the URL was already checked for quotes/brackets).
    header_bg = (f"background: linear-gradient(180deg, rgba(15,23,42,.35), rgba(15,23,42,.8)), url('{e(photo)}') "
                 f"center / cover no-repeat, var(--d);" if photo else "")
    icon_html = "" if photo else f'<div class="icon">{icon_for(lead)}</div>'
    og_image = f'<meta property="og:image" content="{e(photo)}">' if photo else ""
    welcome_html = f'<section><h2>{e(t["welcome"])}</h2><p>{e(about)}</p></section>' if about else ""
    contact_links = "".join([
        f'<a href="{e(tel)}">📞 {e(phone)}</a>' if tel else "",
        f'<a href="{e(wa_url)}">💬 WhatsApp</a>' if wa_url else "",
        f'<a href="mailto:{e(lead["email"])}">✉️ {e(lead["email"])}</a>' if lead.get("email") else "",
        f'<a href="{e(book_url)}" rel="noopener">📅 {e(t["book"])}</a>' if book_url else "",
        f'<a href="{e(follow_url)}" rel="noopener">🔗 {e(t["follow"])}</a>' if follow_url else "",
    ])
    sticky = "".join([
        f'<a class="btn primary" href="{e(tel)}">📞 {e(t["call"])}</a>' if tel else "",
        f'<a class="btn wa" href="{e(wa_url)}">💬 {e(t["whatsapp"])}</a>' if wa_url else "",
    ])

    return f"""<!doctype html>
<html lang="{e(language or 'en')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{e(name)}</title>
<meta property="og:title" content="{e(name)}">
<meta property="og:description" content="{e(sub)}">
{og_image}
<style>
:root {{ --p: {primary}; --d: {deep}; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #1f2937; background: #f8fafc; }}
a {{ color: inherit; }}
.banner {{ background: #111827; color: #f9fafb; text-align: center; font-size: 13px; padding: 8px 16px; }}
.banner a {{ color: #fbbf24; }}
header {{ background: linear-gradient(135deg, var(--p), var(--d)); color: #fff; padding: 56px 20px 64px; text-align: center; }}
header.photo {{ min-height: 70vh; display: flex; flex-direction: column; justify-content: flex-end; padding-top: 120px; text-shadow: 0 2px 12px rgba(0,0,0,.45); }}
.icon {{ font-size: 56px; line-height: 1; }}
h1 {{ font-size: clamp(30px, 7vw, 52px); line-height: 1.12; margin: 14px auto 6px; max-width: 760px; overflow-wrap: anywhere; }}
.sub {{ opacity: .92; font-size: 18px; }}
.rating, .badge {{ display: inline-block; margin: 14px 4px 0; background: rgba(255,255,255,.16); backdrop-filter: blur(6px); border-radius: 999px; padding: 6px 14px; font-size: 15px; text-shadow: none; }}
.badge.open {{ color: #bbf7d0; }}
.badge.closed {{ color: #fecaca; }}
.stars {{ color: #fbbf24; letter-spacing: 1px; }}
.cta {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-top: 26px; text-shadow: none; }}
.btn {{ display: inline-block; text-decoration: none; font-weight: 600; padding: 12px 20px; border-radius: 12px; }}
.btn.primary {{ background: #fff; color: var(--d); }}
.btn.wa {{ background: #22c55e; color: #fff; }}
.btn.ghost {{ border: 1.5px solid rgba(255,255,255,.75); color: #fff; background: rgba(15,23,42,.2); }}
.btn.outline {{ border: 1.5px solid var(--p); color: var(--p); margin-top: 14px; }}
main {{ max-width: 860px; margin: -28px auto 0; padding: 0 16px 40px; position: relative; }}
section {{ background: #fff; border-radius: 16px; box-shadow: 0 6px 24px rgba(15,23,42,.06); padding: 26px; margin-bottom: 18px; }}
h2 {{ margin: 0 0 12px; font-size: 22px; color: var(--d); }}
.hours {{ width: 100%; border-collapse: collapse; }}
.hours th, .hours td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #f1f5f9; font-weight: 500; }}
.hours td {{ text-align: right; color: #475569; }}
.hours tr.today {{ background: #f0fdf4; }}
.hours tr.today th, .hours tr.today td {{ font-weight: 700; color: #14532d; }}
.score {{ display: flex; align-items: center; gap: 16px; }}
.score .big {{ font-size: 56px; font-weight: 800; color: var(--d); line-height: 1; }}
.stars.lg {{ font-size: 24px; }}
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
<header class="{header_class}" style="{header_bg}">
  {icon_html}
  <h1>{e(name)}</h1>
  <div class="sub">{e(sub)}</div>
  <div>{rating_html}{badge_html}</div>
  <div class="cta">{"".join(buttons)}</div>
</header>
<main>
  {welcome_html}
  {reviews_html}
  {hours_html}
  <section>
    <h2>{e(t["find"])}</h2>
    <p>{e(lead.get("address") or city)}</p>
    <iframe class="map" src="{e(embed)}" loading="lazy" referrerpolicy="no-referrer-when-downgrade" title="map"></iframe>
  </section>
  <section class="contact">
    <h2>{e(t["contact"])}</h2>
    {contact_links}
  </section>
</main>
<footer>© {date.today().year} {e(name)} · {footer}</footer>
<div class="sticky">{sticky}</div>
</body>
</html>"""
