import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import fake_leads
from odify_bot import bot, outreach, store

OWNER = 111


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", str(OWNER))


# ---- parsing ----

@pytest.mark.parametrize("text,expected", [
    ("barbers in Ikeja", ("barbers", "Ikeja", bot.DEFAULT_RESULTS, True, True)),
    ("30 dentists in Austin, TX", ("dentists", "Austin, TX", 30, True, True)),
    ("hair salons in Port-Harcourt --no-social", ("hair salons", "Port-Harcourt", bot.DEFAULT_RESULTS, False, True)),
    ("5 car wash in lekki --all", ("car wash", "lekki", 5, True, False)),
    ("500 gyms in Abuja", ("gyms", "Abuja", bot.MAX_RESULTS, True, True)),
    ("bed and breakfast in Cape Town IN South Africa", ("bed and breakfast in Cape Town", "South Africa",
                                                        bot.DEFAULT_RESULTS, True, True)),
])
def test_parse_query(text, expected):
    q = bot.parse_query(text)
    assert (q["niche"], q["location"], q["count"], q["include_social"], q["exclude_seen"]) == expected


@pytest.mark.parametrize("text", ["barbers", "in Lagos", "barbers in ", "--all"])
def test_parse_query_rejects(text):
    assert bot.parse_query(text) is None


# ---- store ----

def test_store_roundtrip_and_pipeline():
    sid = store.save_search("barbers", "Ikeja", 5, {"include_social": True}, fake_leads(3))
    assert store.get_search(sid)["returned"] == 3
    leads = store.get_leads(sid)
    assert [lead["name"] for lead in leads] == ["Biz 0", "Biz 1", "Biz 2"]
    assert store.seen_place_ids() == {"p0", "p1", "p2"}
    store.set_status(leads[0]["id"], "won")
    store.set_notes(leads[1]["id"], "call Fri")
    assert store.pipeline_counts() == {"new": 2, "contacted": 0, "replied": 0, "won": 1, "lost": 0}
    assert store.get_lead(leads[1]["id"])["notes"] == "call Fri"
    with pytest.raises(ValueError):
        store.set_status(leads[0]["id"], "bogus")
    store.link_message(9, 42, leads[2]["id"])
    assert store.lead_for_message(9, 42) == leads[2]["id"]
    assert len(store.all_leads()) == 3


# ---- outreach ----

def test_template_and_whatsapp_link():
    lead = fake_leads(2)[1]
    assert outreach.get_template() == outreach.DEFAULT_TEMPLATE
    outreach.set_template("Hi {name} ({category}) {rating}★/{reviews}")
    assert outreach.fill(outreach.get_template(), lead) == "Hi Biz 1 (barber shop) 4.5★/10"
    link = outreach.whatsapp_link(lead)
    assert link.startswith("https://wa.me/2348030000001?text=Hi%20Biz%201")
    outreach.set_template(None)
    assert outreach.get_template() == outreach.DEFAULT_TEMPLATE
    # a national number without country code can't be turned into a wa.me link
    assert outreach.whatsapp_number({"phone": "0803 000 0000", "intl_phone": ""}) is None


def test_lead_card_escapes_html():
    lead = {**fake_leads(1)[0], "id": 1, "name": "<b>Tunde & Sons</b>", "status": "new", "notes": "a<b"}
    card = bot.lead_card(lead)
    assert "&lt;b&gt;Tunde &amp; Sons&lt;/b&gt;" in card
    assert "a&lt;b" in card


# ---- handlers (Telegram mocked) ----

def make_update(text: str = "", user_id: int = OWNER, reply_to=None):
    status = MagicMock()
    status.edit_text = AsyncMock()
    msg = MagicMock()
    msg.text, msg.chat_id, msg.reply_to_message = text, 5, reply_to
    msg.reply_text = AsyncMock(return_value=status)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="me"),
                             effective_message=msg, effective_chat=SimpleNamespace(id=5), callback_query=None)
    return update, msg, status


def make_context():
    sent = []

    async def send_message(chat_id, text, **kw):
        sent.append(("msg", text, kw))
        return SimpleNamespace(message_id=100 + len(sent))

    async def send_document(chat_id, document, filename=None, **kw):
        sent.append(("doc", filename, document))

    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=send_message, send_document=send_document), args=[])
    return ctx, sent


def test_stranger_is_rejected(monkeypatch):
    called = []
    monkeypatch.setattr(bot, "run_search", AsyncMock(side_effect=lambda *a: called.append(1)))
    update, msg, _ = make_update("barbers in Ikeja", user_id=999)
    ctx, _ = make_context()
    asyncio.run(bot.on_text(update, ctx))
    assert not called
    assert "private bot" in msg.reply_text.call_args.args[0]


def test_search_flow_saves_and_sends_cards(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # plain parser path
    calls = {}

    def fake_search(queries, n, include_social, exclude_place_ids, region, language, email_location, progress):
        calls.update(queries=queries, n=n, exclude=set(exclude_place_ids), region=region)
        progress("Scanning Google Maps…")
        return fake_leads(7)[:n]

    monkeypatch.setattr(bot.places, "search_queries", fake_search)
    update, msg, status = make_update("10 barbers in Ikeja")
    ctx, sent = make_context()
    asyncio.run(bot.on_text(update, ctx))

    assert calls == {"queries": ["barbers in Ikeja"], "n": 10, "exclude": set(), "region": ""}
    summary = status.edit_text.call_args.args[0]
    assert "<b>7 leads</b>" in summary and "7 WhatsApp-ready" in summary
    cards = [s for s in sent if s[0] == "msg" and "Barber shop" in s[1]]
    assert len(cards) == bot.PAGE
    assert sent[-1][1] == "Showing 5 of 7"  # pager
    assert store.lead_for_message(5, 101) is not None

    # second search skips what we already have
    update2, _, _ = make_update("barbers in Ikeja")
    asyncio.run(bot.on_text(update2, make_context()[0]))
    assert calls["exclude"] == {f"p{i}" for i in range(7)}


def test_reply_to_card_saves_note():
    sid = store.save_search("b", "l", 1, {}, fake_leads(1))
    lead = store.get_leads(sid)[0]
    store.link_message(5, 77, lead["id"])
    card = MagicMock(message_id=77)
    card.edit_text = AsyncMock()
    update, msg, _ = make_update("Owner says call back Friday", reply_to=card)
    asyncio.run(bot.on_text(update, make_context()[0]))
    assert store.get_lead(lead["id"])["notes"] == "Owner says call back Friday"
    assert "Note saved" in msg.reply_text.call_args.args[0]


def test_buttons_status_and_exports():
    sid = store.save_search("barbers", "Ikeja", 2, {}, fake_leads(2))
    lead = store.get_leads(sid)[0]
    ctx, sent = make_context()

    def press(data):
        q = MagicMock(data=data, message=SimpleNamespace(chat_id=5))
        q.answer, q.edit_message_text, q.edit_message_reply_markup = AsyncMock(), AsyncMock(), AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER, username="me"), callback_query=q,
                                 effective_message=None)
        asyncio.run(bot.on_button(update, ctx))
        return q

    q = press(f"st:{lead['id']}:won")
    assert store.get_lead(lead["id"])["status"] == "won"
    assert "✓ Won" in str(q.edit_message_text.call_args.kwargs["reply_markup"])

    press(f"csv:{sid}")
    press(f"vcf:{sid}")
    press(f"vc:{lead['id']}")
    docs = [(name, data) for kind, name, data in sent if kind == "doc"]
    assert [d[0] for d in docs] == ["odify-barbers-ikeja.csv", "odify-barbers-ikeja.vcf", "biz-0.vcf"]
    assert docs[0][1].decode("utf-8-sig").startswith("Business Name,")
    assert docs[1][1].decode().count("BEGIN:VCARD") == 2
