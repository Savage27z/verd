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
