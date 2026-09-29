import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import fake_leads
from odify_bot import bot, llm, outreach, store

OWNER = 111


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", str(OWNER))
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")


def plan(**over):
    base = {"intent": "search", "reply": "", "status": "", "template_text": "", "niche": "barbers",
            "location_label": "Poland", "country_iso": "PL", "language": "pl", "count": 0,
            "include_social": True, "include_seen": False,
            "queries": ["fryzjer męski Warszawa", "fryzjer męski Kraków", "fryzjer męski Łódź"]}
    base.update(over)
    return base


class FakeLLM:
    """Stands in for AsyncOpenAI: returns queued replies, records requests."""

    def __init__(self, *contents):
        self.contents = list(contents)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.requests.append(kw)
        content = self.contents.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def use_llm(monkeypatch, *contents) -> FakeLLM:
    fake = FakeLLM(*contents)
    monkeypatch.setattr(llm, "client", lambda: fake)
    return fake


# ---- plan parsing ----

def test_plan_message_requests_strict_schema_and_normalises(monkeypatch):
    raw = plan(country_iso="pl", language="PL", count=-5, queries=["a", "A", " ", "b"] + [f"c{i}" for i in range(10)])
    fake = use_llm(monkeypatch, json.dumps(raw))
    p = asyncio.run(llm.plan_message("barbers in poland"))
    req = fake.requests[0]
    assert req["model"] == llm.DEFAULT_MODEL
    assert req["response_format"]["json_schema"]["strict"] is True
    assert req["extra_body"] == {"provider": {"require_parameters": True}}
    assert p["country_iso"] == "PL" and p["language"] == "pl" and p["count"] == 0
    assert p["queries"][:2] == ["a", "b"] and len(p["queries"]) == llm.MAX_QUERIES  # deduped + capped


def test_model_is_configurable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-pro")
    fake = use_llm(monkeypatch, json.dumps(plan()))
    asyncio.run(llm.plan_message("x"))
    assert fake.requests[0]["model"] == "deepseek/deepseek-v4-pro"


def test_plan_tolerates_code_fences_and_rejects_garbage(monkeypatch):
    use_llm(monkeypatch, "```json\n" + json.dumps(plan()) + "\n```", "", "not json")
    assert asyncio.run(llm.plan_message("x"))["intent"] == "search"
    for _ in range(2):
        with pytest.raises(llm.LLMError):
            asyncio.run(llm.plan_message("x"))


def test_search_without_queries_becomes_single_query_or_chat():
    assert llm.normalise_plan(plan(queries=[]))["queries"] == ["barbers in Poland"]
    p = llm.normalise_plan(plan(queries=[], niche="", location_label=""))
    assert p["intent"] == "chat" and p["reply"]


def test_localise_pitch_keeps_placeholders_or_falls_back(monkeypatch):
    fake = use_llm(monkeypatch,
                   '"Cześć {name}! Nie macie strony."',              # plain text; stray quotes stripped
                   "", "Hallo {name}! Keine Website.",                # empty (DeepSeek quirk) → retried
                   "Hola! Sin web.", "Hola! Sin web.")                # lost {name} twice → original
    assert asyncio.run(llm.localise_pitch("Hi {name}! No site.", "pl")) == "Cześć {name}! Nie macie strony."
    assert "response_format" not in fake.requests[0]
    assert asyncio.run(llm.localise_pitch("Hi {name}! No site.", "de")) == "Hallo {name}! Keine Website."
    assert asyncio.run(llm.localise_pitch("Hi {name}! No site.", "es")) == "Hi {name}! No site."
    assert asyncio.run(llm.localise_pitch("Hi {name}!", "en")) == "Hi {name}!"
    assert len(fake.requests) == 5


# ---- bot routing ----

def make_update(text):
    status = MagicMock()
    status.edit_text = AsyncMock()
    msg = MagicMock(text=text, chat_id=5, reply_to_message=None)
    msg.reply_text = AsyncMock(return_value=status)
    return SimpleNamespace(effective_user=SimpleNamespace(id=OWNER, username="me"), effective_message=msg,
                           effective_chat=SimpleNamespace(id=5), callback_query=None), msg, status


def make_context():
    sent = []

    async def send_message(chat_id, text, **kw):
        sent.append(("msg", text, kw))
        return SimpleNamespace(message_id=200 + len(sent))

    async def send_document(chat_id, document, filename=None, **kw):
        sent.append(("doc", filename, document))

    return SimpleNamespace(bot=SimpleNamespace(send_message=send_message, send_document=send_document), args=[]), sent


# Any country: the LLM supplies country + language + cities; phone numbers and the
# WhatsApp pitch follow. (Local numbers here are made international by `phonenumbers`.)
COUNTRIES = [
    ("barbers in poland", plan(), "601 234 567", "48601234567", "Cześć {name}!", "Cze%C5%9B%C4%87"),
    ("plumbers in brazil", plan(niche="plumbers", location_label="Brazil", country_iso="BR", language="pt",
                                queries=["encanador São Paulo", "encanador Rio de Janeiro"]),
     "(11) 91234-5678", "5511912345678", "Olá {name}!", "Ol%C3%A1"),
    ("salons in kenya", plan(niche="salons", location_label="Kenya", country_iso="KE", language="sw",
                             queries=["salon Nairobi", "salon Mombasa"]),
     "0712 345678", "254712345678", "Habari {name}!", "Habari"),
    ("dentists in germany", plan(niche="dentists", location_label="Germany", country_iso="DE", language="de",
                                 queries=["Zahnarzt Berlin", "Zahnarzt Hamburg", "Zahnarzt München"]),
     "030 1234567", "49301234567", "Hallo {name}!", "Hallo"),
]


@pytest.mark.parametrize("text,p,local_phone,wa_digits,pitch,pitch_in_url", COUNTRIES,
                         ids=[c[0] for c in COUNTRIES])
def test_any_country_search(monkeypatch, text, p, local_phone, wa_digits, pitch, pitch_in_url):
    use_llm(monkeypatch, json.dumps(p), pitch)
    calls = {}

    def fake_search(queries, n, include_social, exclude_place_ids, region, language, email_location, progress):
        calls.update(queries=queries, n=n, region=region, language=language, where=email_location)
        # exercise the real phone conversion for that country
        lead = {**fake_leads(1)[0], "phone": local_phone, "intl_phone": ""}
        lead["intl_phone"] = bot.places.to_international(local_phone, region)
        return [lead]

    monkeypatch.setattr(bot.places, "search_queries", fake_search)
    update, msg, status = make_update(text)
    ctx, sent = make_context()
    asyncio.run(bot.on_text(update, ctx))

    assert calls["queries"] == p["queries"]
    assert calls["n"] == bot.DEFAULT_WIDE_RESULTS  # whole country → wide default
    assert (calls["region"], calls["language"], calls["where"]) == (p["country_iso"], p["language"],
                                                                    p["location_label"])
    summary = status.edit_text.call_args.args[0]
    assert f"Searched {len(p['queries'])} areas" in summary and "pitch translated" in summary
    card_markup = str(sent[0][2]["reply_markup"])
    assert f"wa.me/{wa_digits}?text={pitch_in_url}" in card_markup
    assert store.list_searches(1)[0]["options"]["pitch"] == pitch


def test_pitch_translation_is_cached_per_language(monkeypatch):
    fake = use_llm(monkeypatch, "Cześć {name}!", "Olá {name}!", "Hej {name}, nowy tekst!")
    assert asyncio.run(outreach.template_for_language("pl")) == "Cześć {name}!"
    assert asyncio.run(outreach.template_for_language("pl")) == "Cześć {name}!"   # cached: no call
    assert asyncio.run(outreach.template_for_language("pt")) == "Olá {name}!"
    assert len(fake.requests) == 2
    outreach.set_template("Hi {name}, new text!")                                  # edited pitch → re-translate
    assert asyncio.run(outreach.template_for_language("pl")) == "Hej {name}, nowy tekst!"
    assert len(fake.requests) == 3
    assert asyncio.run(outreach.template_for_language("en")) == "Hi {name}, new text!"
    assert len(fake.requests) == 3


def test_english_speaking_country_keeps_pitch(monkeypatch):
    fake = use_llm(monkeypatch, json.dumps(plan(location_label="Austin, TX", country_iso="US", language="en",
                                                queries=["barber Austin TX"])))
    monkeypatch.setattr(bot.places, "search_queries", lambda queries, n, **kw: [])
    update, _, status = make_update("barbers in austin")
    asyncio.run(bot.on_text(update, make_context()[0]))
    assert len(fake.requests) == 1  # no translation call
    assert "pitch translated" not in status.edit_text.call_args.args[0]


def test_llm_failure_falls_back_to_simple_parser(monkeypatch):
    use_llm(monkeypatch, "")  # empty content → LLMError
    seen = {}

    def fake_search(queries, n, **kw):
        seen["q"] = queries
        return []

    monkeypatch.setattr(bot.places, "search_queries", fake_search)
    update, msg, status = make_update("barbers in Ikeja")
    asyncio.run(bot.on_text(update, make_context()[0]))
    assert seen["q"] == ["barbers in Ikeja"]


@pytest.mark.parametrize("p,expect", [
    (plan(intent="chat", reply="I find businesses without websites!"), "I find businesses without websites!"),
    (plan(intent="help"), "Just tell me what you want"),
    (plan(intent="template", template_text="Hej {name}!"), "Pitch saved"),
])
def test_non_search_intents(monkeypatch, p, expect):
    use_llm(monkeypatch, json.dumps(p))
    update, msg, _ = make_update("whatever")
    asyncio.run(bot.on_text(update, make_context()[0]))
    assert expect in msg.reply_text.call_args.args[0]
    if p["intent"] == "template":
        assert outreach.get_template() == "Hej {name}!"


def test_pipeline_intent_lists_leads_by_status(monkeypatch):
    sid = store.save_search("b", "l", 2, {}, fake_leads(2))
    store.set_status(store.get_leads(sid)[1]["id"], "won")
    use_llm(monkeypatch, json.dumps(plan(intent="pipeline", status="won")))
    update, _, _ = make_update("show my won leads")
    ctx, sent = make_context()
    asyncio.run(bot.on_text(update, ctx))
    assert len(sent) == 1 and "Biz 1" in sent[0][1]
