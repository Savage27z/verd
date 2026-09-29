import asyncio
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import fake_leads
from odify_bot import bot, mockup, store, web

OWNER = 111


def lead(**over):
    base = {"id": 1, "name": "Barber u Jacha", "category": "Barber shop", "rating": 4.9, "reviews": 156,
            "address": "ul. Długa 5, 31-147 Kraków, Poland", "intl_phone": "+48 881 413 419",
            "phone": "881 413 419", "website": "https://booksy.com/pl-pl/216417_barber-u-jacha",
            "web_presence": "social", "maps_url": "https://maps.google.com/?cid=1", "email": ""}
    base.update(over)
    return base


# ---- rendering ----

@pytest.mark.parametrize("address,expected", [
    ("ul. Długa 5, 31-147 Kraków, Poland", "Kraków"),
    ("Hauptstraße 5, 10115 Berlin, Germany", "Berlin"),
    ("123 Main St, Austin, TX 78701, USA", "Austin"),
    ("", ""),
])
def test_city_from_address(address, expected):
    assert mockup.city_from_address(address) == expected


def test_render_localised_page_with_all_contact_routes():
    page = mockup.render(lead(), "pl", "Savage Studio", "https://wa.me/48000")
    assert '<html lang="pl">' in page and "noindex" in page
    assert "Zadzwoń" in page and "Zarezerwuj online" in page and "Jak do nas trafić" in page
    assert 'href="tel:+48881413419"' in page and 'href="https://wa.me/48881413419"' in page
    assert "4.9 ★ z 156 opinii Google" in page and "💈" in page
    assert '<a href="https://wa.me/48000">Savage Studio</a>' in page  # brand linked
    assert "maps.google.com/maps?q=" in page


def test_render_escapes_business_data_and_falls_back_to_english():
    page = mockup.render(lead(name='<script>alert(1)</script>', category='"><img src=x>', website="",
                              web_presence="none"), "xx")
    assert "<script>alert" not in page and "<img src=x>" not in page
    assert "Call us" in page and "Book online" not in page


def test_send_link_uses_local_language():
    url = mockup.send_link(lead(), "https://x.up.railway.app/m/abc", "de")
    assert url.startswith("https://wa.me/48881413419?text=Hallo%20Barber%20u%20Jacha")
    assert mockup.send_link(lead(intl_phone="", phone="881 413 419"), "u", "pl") is None


# ---- photos, hours, reviews ----

from datetime import datetime, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from odify_bot import places  # noqa: E402

PL_HOURS = places.hours_json(list({
    "wtorek": "10:00–20:00", "środa": "10:00–20:00", "czwartek": "10:00–20:00", "piątek": "10:00–20:00",
    "sobota": "10:00–15:00", "niedziela": "Zamknięte", "poniedziałek": "10:00–20:00"}.items()))
TUESDAY_NOON_WARSAW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)   # 12:00 CEST
TUESDAY_2AM_WARSAW = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text,expected", [
    ("10:00–20:00", [(600, 1200)]),
    ("9 AM–10 PM", [(540, 1320)]),
    ("12–8 PM", [(720, 1200)]),                      # shared PM
    ("9:30 AM–1 PM, 2–6 PM", [(570, 780), (840, 1080)]),
    ("6 PM–2 AM", [(1080, 1560)]),                  # past midnight
    ("Open 24 hours", [(0, 1440)]),
    ("Otwarte całą dobę", [(0, 1440)]),
    ("Zamknięte", []),
    ("Closed", []),
])
def test_parse_intervals(text, expected):
    assert mockup.parse_intervals(text) == expected


def test_weekdays_in_any_language():
    assert [mockup.weekday_index(d) for d in ("Wtorek", "Tuesday", "Dienstag", "Segunda-feira", "lunedì")] == [1, 1, 1, 0, 0]
    assert mockup.weekday_index("Holiday") is None


def test_hours_sorted_monday_first_with_open_badge():
    pairs = mockup.load_hours({"hours": PL_HOURS})
    assert [d for d, _ in pairs][:2] == ["poniedziałek", "wtorek"]
    warsaw = TUESDAY_NOON_WARSAW.astimezone(ZoneInfo("Europe/Warsaw"))
    assert mockup.open_status(pairs, warsaw) == (1, True, "20:00")
    assert mockup.load_hours({"hours": "not json"}) == []


def test_large_photo_url():
    assert places.large_photo_url("https://lh3.googleusercontent.com/gps-cs-s/ABC") == \
        "https://lh3.googleusercontent.com/gps-cs-s/ABC=w1600-h900-p-k-no"
    assert places.large_photo_url("https://lh5.googleusercontent.com/p/XYZ=w80-h106-k-no") == \
        "https://lh5.googleusercontent.com/p/XYZ=w1600-h900-p-k-no"
    assert places.large_photo_url("https://example.com/a.jpg") == "https://example.com/a.jpg"


def test_render_with_photo_hours_reviews_and_booking():
    rich = lead(photo_url="https://lh3.googleusercontent.com/gps-cs-s/ABC=w1600-h900-p-k-no", hours=PL_HOURS,
                booking_url="https://northbarberkrk.booksy.com/a/", website="https://instagram.com/north")
    page = mockup.render(rich, "pl", now=TUESDAY_NOON_WARSAW)
    assert 'class="photo"' in page and "url('https://lh3.googleusercontent.com/gps-cs-s/ABC=w1600-h900-p-k-no')" in page
    assert "💈" not in page  # the photo replaces the emoji
    assert "Godziny otwarcia" in page and "Wtorek · Dziś" in page and "Otwarte · do 20:00" in page
    assert "Co mówią klienci" in page and "312" not in page and "156 opinii w Google" in page
    assert 'href="https://northbarberkrk.booksy.com/a/"' in page  # Google's booking link wins over Instagram
    night = mockup.render(rich, "pl", now=TUESDAY_2AM_WARSAW)
    assert "Teraz zamknięte" in night


def test_render_rejects_suspicious_photo_urls():
    page = mockup.render(lead(photo_url="javascript:alert(1)"), "en")
    assert "javascript:" not in page and 'class="photo"' not in page
    page = mockup.render(lead(photo_url="https://x.googleusercontent.com/a');}body{background:red"), "en")
    assert "background:red" not in page


def test_serper_captures_photo_hours_and_booking(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("PLACES_PROVIDER", raising=False)
    monkeypatch.setattr(places, "find_business_email", lambda *a, **k: "")
    place = {"title": "North", "placeId": "ChIJ1", "cid": "1", "phoneNumber": "+48 791 711 671", "rating": 4.9,
             "ratingCount": 312, "thumbnailUrl": "https://lh3.googleusercontent.com/gps-cs-s/ABC",
             "openingHours": {"wtorek": "10:00–20:00", "niedziela": "Zamknięte"},
             "bookingLinks": ["https://northbarberkrk.booksy.com/a/", "https://booksy.com/x"]}
    monkeypatch.setattr(places.requests, "post", lambda *a, **k: SimpleNamespace(
        status_code=200, ok=True, json=lambda: {"places": [place]}, text=""))
    [found] = places.search_queries(["fryzjer Kraków"], 5, region="PL", language="pl")
    assert found["photo_url"].endswith("=w1600-h900-p-k-no")
    assert found["booking_url"] == "https://northbarberkrk.booksy.com/a/"
    assert mockup.load_hours(found) == [("wtorek", "10:00–20:00"), ("niedziela", "Zamknięte")]
    sid = store.save_search("barbers", "Kraków", 1, {"language": "pl"}, [found])
    assert store.get_leads(sid)[0]["booking_url"] == "https://northbarberkrk.booksy.com/a/"


# ---- storage ----

def test_mockup_token_is_stable_and_views_are_throttled():
    sid = store.save_search("b", "Kraków", 1, {"language": "pl"}, fake_leads(1))
    lead_id = store.get_leads(sid)[0]["id"]
    token = store.mockup_token(lead_id)
    assert token == store.mockup_token(lead_id) and len(token) >= 10
    found, search = store.mockup_by_token(token)
    assert found["id"] == lead_id and search["options"]["language"] == "pl"
    assert store.record_view(token) == (1, True)
    assert store.record_view(token) == (2, False)  # within 30 min: counted, no second ping
    assert store.mockup_views(lead_id) == 2
    assert store.mockup_by_token("nope") is None


# ---- web server ----

@pytest.fixture()
def server(monkeypatch):
    seen = []
    srv = web.start(lambda lead_, views: seen.append((lead_["name"], views)), port=0)
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def get(url, ua="Mozilla/5.0"):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as err:
        return err.code, "", {}


def test_server_serves_previews_and_counts_real_opens(server):
    base, seen = server
    sid = store.save_search("barbers", "Kraków", 1, {"language": "pl"}, fake_leads(1))
    token = store.mockup_token(store.get_leads(sid)[0]["id"])

    assert get(f"{base}/health")[0] == 200
    assert get(f"{base}/m/doesnotexist")[0] == 404
    status, body, headers = get(f"{base}/m/{token}", ua="WhatsApp/2.24.1 A")  # link preview fetcher
    assert status == 200 and "Biz 0" in body and headers["X-Robots-Tag"].startswith("noindex")
    assert seen == []
    get(f"{base}/m/{token}")
    assert seen == [("Biz 0", 1)]


# ---- bot ----

def test_mockup_button_needs_public_url_then_sends_preview(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", str(OWNER))
    sid = store.save_search("barbers", "Kraków", 1, {"language": "pl"}, fake_leads(2))
    lead_id = store.get_leads(sid)[1]["id"]
    sent = []

    async def send_message(chat_id, text, **kw):
        sent.append((text, kw))

    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))

    def press():
        q = MagicMock(data=f"mk:{lead_id}", message=SimpleNamespace(chat_id=5))
        q.answer = MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0))
        asyncio.run(bot.on_button(SimpleNamespace(effective_user=SimpleNamespace(id=OWNER, username="me"),
                                                  callback_query=q, effective_message=None), ctx))
        return q

    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    q = press()
    assert "public URL" in q.answer.call_args.args[0] and not sent

    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "verd-bot.up.railway.app")
    press()
    text, kw = sent[0]
    token = store.mockup_token(lead_id)
    assert f"https://verd-bot.up.railway.app/m/{token}" in text
    markup = str(kw["reply_markup"])
    assert "Open preview" in markup and "wa.me/2348030000001?text=Cze" in markup  # Polish send text


def test_view_ping_only_after_contact(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", str(OWNER))
    sent = []

    async def send_message(chat_id, text, **kw):
        sent.append((chat_id, text))

    app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    new_lead = {**fake_leads(1)[0], "id": 1, "status": "new", "search_id": None}
    asyncio.run(bot.notify_view(app, new_lead, 1))
    assert sent == []  # you opening your own preview before pitching isn't news
    asyncio.run(bot.notify_view(app, {**new_lead, "status": "contacted"}, 3))
    assert sent[0][0] == OWNER and "just opened their website preview (view 3)" in sent[0][1]
