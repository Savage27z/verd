import asyncio
import sqlite3
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest

from conftest import fake_leads
from odify_bot import bot, digest, store

OWNER = 111
LAGOS = ZoneInfo("Africa/Lagos")
NOW = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)  # 09:00 in Lagos


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", str(OWNER))
    monkeypatch.setenv("DIGEST_TZ", "Africa/Lagos")
    monkeypatch.setenv("DIGEST_TIME", "09:00")
    monkeypatch.delenv("FOLLOWUP_DAYS", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


def ts(dt: datetime) -> str:
    return digest.sql_utc(dt)


def sql(query: str, *args):
    with store.connect() as conn:
        conn.execute(query, args)


def make_leads(n: int, language: str = "pl", phones: list[str] | None = None) -> list[dict]:
    leads = fake_leads(n)
    for i, lead in enumerate(leads):
        lead["name"] = f"Shop {i}"
        lead["intl_phone"] = (phones or [])[i] if phones and i < len(phones) else "+48 601 000 00" + str(i)
    sid = store.save_search("barbers", "Kraków", n, {"language": language}, leads)
    return store.get_leads(sid)


def contact(lead_id: int, when: datetime, followups: int = 0):
    store.set_status(lead_id, "contacted")
    sql("UPDATE leads SET contacted_at=?, followups=? WHERE id=?", ts(when), followups, lead_id)


# ---- scheduling ----

def test_schedule_in_lagos_time():
    tz, at, days = digest.settings()
    assert (tz.key, at, days) == ("Africa/Lagos", time(9, 0), 3)
    assert not digest.due_now(NOW - timedelta(minutes=1), tz, at, None)       # 08:59 Lagos
    assert digest.due_now(NOW, tz, at, None)                                   # 09:00 Lagos
    assert not digest.due_now(NOW, tz, at, "2026-09-29")                       # already sent today
    assert digest.due_now(NOW + timedelta(hours=5), tz, at, "2026-09-28")      # catch up after a restart
    assert digest.next_run(NOW - timedelta(minutes=30), tz, at) == datetime(2026, 9, 29, 9, 0, tzinfo=LAGOS)
    assert digest.next_run(NOW + timedelta(minutes=1), tz, at) == datetime(2026, 9, 30, 9, 0, tzinfo=LAGOS)


def test_bad_settings_fall_back(monkeypatch):
    monkeypatch.setenv("DIGEST_TZ", "Mars/Olympus")
    monkeypatch.setenv("DIGEST_TIME", "nine")
    tz, at, _ = digest.settings()
    assert tz.key == "UTC" and at == time(9, 0)


# ---- storage ----

def test_contact_starts_clock_and_followups_restart_it():
    [lead] = make_leads(1)
    assert store.get_lead(lead["id"])["contacted_at"] is None
    contacted = store.set_status(lead["id"], "contacted")
    assert contacted["contacted_at"] and contacted["followups"] == 0
    after = store.mark_followed_up(lead["id"])
    assert after["followups"] == 1
    counts = store.event_counts("2000-01-01 00:00:00", "2100-01-01 00:00:00")
    assert counts == {"contacted": 1, "followup": 1}


def test_migration_adds_columns_to_existing_database(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    monkeypatch.setenv("ODIFY_DB", str(db))
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE searches (id INTEGER PRIMARY KEY, niche TEXT NOT NULL, location TEXT NOT NULL,
            requested INTEGER NOT NULL, returned INTEGER NOT NULL, options TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE leads (id INTEGER PRIMARY KEY, search_id INTEGER NOT NULL, position INTEGER NOT NULL,
            place_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
            intl_phone TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
            website TEXT NOT NULL DEFAULT '', web_presence TEXT NOT NULL DEFAULT 'none',
            category TEXT NOT NULL DEFAULT '', rating REAL, reviews INTEGER NOT NULL DEFAULT 0,
            maps_url TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'new',
            notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT '2026-09-20 10:00:00');
        INSERT INTO searches (niche, location, requested, returned) VALUES ('b', 'l', 1, 1);
        INSERT INTO leads (search_id, position, name, status) VALUES (1, 0, 'Old contacted lead', 'contacted');
    """)
    conn.commit()
    conn.close()
    store.init_db()
    store.init_db()  # idempotent
    lead = store.get_lead(1)
    assert lead["followups"] == 0 and lead["contacted_at"] == "2026-09-20 10:00:00"  # clock backfilled


# ---- building the digest ----

def test_build_sorts_leads_into_due_hot_and_closed():
    due, fresh, exhausted, hot, new = make_leads(5)
    contact(due["id"], NOW - timedelta(days=4))
    contact(fresh["id"], NOW - timedelta(days=1))
    contact(exhausted["id"], NOW - timedelta(days=4), followups=2)
    contact(hot["id"], NOW - timedelta(days=5))
    token = store.mockup_token(hot["id"])
    store.record_view(token)
    sql("UPDATE mockups SET last_view_at=? WHERE token=?", ts(NOW - timedelta(hours=2)), token)
    # yesterday (Lagos) activity + something from today that must not count
    sql("INSERT INTO lead_events (lead_id, kind, at) VALUES (?, 'replied', ?)", new["id"], ts(NOW - timedelta(hours=20)))
    sql("INSERT INTO lead_events (lead_id, kind, at) VALUES (?, 'won', ?)", new["id"], ts(NOW - timedelta(hours=1)))

    d = digest.build(NOW)
    assert [x["id"] for x in d.due] == [due["id"]]          # hot lead isn't listed twice
    assert [x["id"] for x in d.hot] == [hot["id"]]
    assert [x["id"] for x in d.closed] == [exhausted["id"]]
    assert store.get_lead(exhausted["id"])["status"] == "lost"
    assert d.yesterday.get("replied") == 1 and "won" not in d.yesterday
    text = digest.summary_html(d)
    assert "Tue 29 Sep" in text and "💬 replied 1" in text
    assert "🔥 <b>1 opened your preview</b>" in text and "⏰ <b>1 follow-up due</b>" in text
    assert "Moved 1 to Lost" in text and "Shop 2" in text


def test_empty_digest_suggests_new_search():
    d = digest.build(NOW)
    assert d.empty and "Nothing to chase today" in digest.summary_html(d)


def test_card_shows_their_local_time():
    pl, br = make_leads(2, phones=["+48 601 234 567", "+55 11 91234-5678"])
    for lead in (pl, br):
        contact(lead["id"], NOW - timedelta(days=4))
    pl, br = store.get_lead(pl["id"]), store.get_lead(br["id"])
    assert "10:00 their time, good time to send" in digest.card_html(pl, NOW)       # Warsaw, CEST
    assert "05:00 their time, better wait" in digest.card_html(br, NOW)              # São Paulo
    assert "follow-up 1/2" in digest.card_html(pl, NOW) and "contacted 4d ago" in digest.card_html(pl, NOW)


def test_followup_link_is_localised_and_includes_preview(monkeypatch):
    [lead] = make_leads(1, language="de")
    assert unquote(digest.followup_link(lead)).startswith(
        "https://wa.me/48601000000?text=Hallo Shop 0! Ich wollte kurz nachhaken. Möchten Sie")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "verd-bot.up.railway.app")
    link = unquote(digest.followup_link(lead))
    assert f"https://verd-bot.up.railway.app/m/{store.mockup_token(lead['id'])}" in link


# ---- bot ----

def fake_bot():
    sent = []

    async def send_message(chat_id, text, **kw):
        sent.append((chat_id, text, kw))
    return SimpleNamespace(send_message=send_message), sent


def test_send_digest_sends_summary_then_actionable_cards():
    [lead] = make_leads(1)
    contact(lead["id"], NOW - timedelta(days=4))
    tg, sent = fake_bot()
    asyncio.run(bot.send_digest(tg, OWNER, NOW))
    assert len(sent) == 2 and "Your lead digest" in sent[0][1]
    markup = str(sent[1][2]["reply_markup"])
    assert "Send follow-up" in markup and f"fu:{lead['id']}" in markup and f"st:{lead['id']}:lost" in markup


def test_followed_up_button(monkeypatch):
    [lead] = make_leads(1)
    contact(lead["id"], NOW - timedelta(days=4))
    q = MagicMock(data=f"fu:{lead['id']}", message=SimpleNamespace(chat_id=5))
    q.answer, q.edit_message_text = AsyncMock(), AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER, username="me"), callback_query=q,
                             effective_message=None)
    asyncio.run(bot.on_button(update, SimpleNamespace(bot=None)))
    assert store.get_lead(lead["id"])["followups"] == 1
    assert "(1/2)" in q.edit_message_text.call_args.args[0]
    asyncio.run(bot.on_button(update, SimpleNamespace(bot=None)))
    assert "move them to Lost" in q.edit_message_text.call_args.args[0]


def test_digest_loop_sends_once_per_day(monkeypatch):
    [lead] = make_leads(1)
    contact(lead["id"], NOW - timedelta(days=4))
    tg, sent = fake_bot()

    class Stop(Exception):
        pass

    async def stop_sleep(_seconds):
        raise Stop

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(bot, "datetime", FrozenDatetime)
    monkeypatch.setattr(bot.asyncio, "sleep", stop_sleep)
    app = SimpleNamespace(bot=tg)
    with pytest.raises(Stop):
        asyncio.run(bot.digest_loop(app))
    assert store.get_setting("digest_last_sent") == "2026-09-29" and len(sent) == 2
    with pytest.raises(Stop):
        asyncio.run(bot.digest_loop(app))
    assert len(sent) == 2  # not sent twice the same day
