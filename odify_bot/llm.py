"""DeepSeek via OpenRouter: turn any message into an action plan, and localise the pitch.

Everything here is optional — without OPENROUTER_API_KEY the bot falls back to the
plain '<niche> in <location>' parser.
"""
import json
import logging
import os
import re

from openai import AsyncOpenAI, OpenAIError

log = logging.getLogger("odify.llm")

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
BASE_URL = "https://openrouter.ai/api/v1"
MAX_QUERIES = 8

INTENTS = ["search", "pipeline", "recent", "export", "template", "help", "chat"]
STATUSES = ["", "new", "contacted", "replied", "won", "lost"]

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "reply", "status", "template_text", "niche", "location_label", "country_iso",
                 "language", "count", "include_social", "include_seen", "queries"],
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "reply": {"type": "string"},
        "status": {"type": "string", "enum": STATUSES},
        "template_text": {"type": "string"},
        "niche": {"type": "string"},
        "location_label": {"type": "string"},
        "country_iso": {"type": "string"},
        "language": {"type": "string"},
        "count": {"type": "integer"},
        "include_social": {"type": "boolean"},
        "include_seen": {"type": "boolean"},
        "queries": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM_PROMPT = f"""You are the brain of a Telegram bot that finds local businesses on Google Maps that have NO website, so the user (a web designer) can pitch them. Read the user's message (any language) and return one JSON object matching the schema.

Intents:
- "search": the user wants businesses found. Examples: "barbers in poland", "30 dentists around Kraków", "find me car washes in Lagos that only have instagram".
- "pipeline": they ask about their leads by status ("show my won leads", "who did I contact"). Put the status in "status" ("" = overview of all statuses).
- "recent": they want their recent searches.
- "export": they want all their leads as a file/CSV.
- "template": they want to see or change their outreach/WhatsApp pitch. If they give new text, put it in "template_text" and keep any {{name}} {{category}} {{rating}} {{reviews}} placeholders they used; otherwise "".
- "help": how to use the bot.
- "chat": anything else. Answer briefly in "reply", in the user's language, steering back to what the bot does.

For "search" fill:
- "niche": the kind of business in English, singular or plural as natural ("barbers").
- "location_label": the place in plain words ("Poland", "Kraków, Poland").
- "country_iso": ISO 3166-1 alpha-2 code of the country searched ("PL"). "" only if truly unknown.
- "language": ISO 639-1 code of the main local language there ("pl").
- "count": how many results they asked for; 0 if they didn't say.
- "include_social": false only if they want to exclude businesses that have just a Facebook/Instagram page; else true.
- "include_seen": true only if they explicitly want leads they've already received again; else false.
- "queries": Google Maps search strings written the way a local would type them, in the local language, each naming a specific place. If the location is a single city, town or district, give 1-2 queries (local-language term, plus the English term if locals commonly use it, e.g. "barber Warszawa"). If it is a whole country, state or large region, pick its largest cities (up to {MAX_QUERIES}) and give one query per city, e.g. ["fryzjer męski Warszawa", "fryzjer męski Kraków", ...]. Never exceed {MAX_QUERIES} queries.

For non-search intents set niche/location_label/country_iso/language to "", count to 0, include_social true, include_seen false, queries []. Always set "reply" ("" is fine for search).
Output only the JSON object."""


class LLMError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.getenv("OPENROUTER_API_KEY"))


def model() -> str:
    return os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)


_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENROUTER_API_KEY"), base_url=BASE_URL, timeout=45, max_retries=2)
    return _client


def _json_from(text: str | None) -> dict:
    if not text or not text.strip():
        raise LLMError("empty response")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())  # tolerate fenced output
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMError("expected a JSON object")
    return data


def normalise_plan(raw: dict) -> dict:
    """Defensive clean-up — the model is instructed well, but never trusted blindly."""
    intent = raw.get("intent") if raw.get("intent") in INTENTS else "chat"
    queries = [q.strip()[:120] for q in raw.get("queries") or [] if isinstance(q, str) and q.strip()]
    seen: set[str] = set()
    queries = [q for q in queries if not (q.lower() in seen or seen.add(q.lower()))][:MAX_QUERIES]
    iso = str(raw.get("country_iso") or "").strip().upper()
    lang = str(raw.get("language") or "").strip().lower()
    try:
        count = int(raw.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    plan = {
        "intent": intent,
        "reply": str(raw.get("reply") or "")[:1500],
        "status": raw.get("status") if raw.get("status") in STATUSES else "",
        "template_text": str(raw.get("template_text") or "")[:1500],
        "niche": str(raw.get("niche") or "").strip()[:120],
        "location_label": str(raw.get("location_label") or "").strip()[:120],
        "country_iso": iso if re.fullmatch(r"[A-Z]{2}", iso) else "",
        "language": lang if re.fullmatch(r"[a-z]{2}", lang) else "",
        "count": max(0, count),
        "include_social": raw.get("include_social") is not False,
        "include_seen": raw.get("include_seen") is True,
        "queries": queries,
    }
    if intent == "search" and not plan["queries"]:
        if plan["niche"] and plan["location_label"]:
            plan["queries"] = [f"{plan['niche']} in {plan['location_label']}"]
        else:
            plan["intent"] = "chat"
            plan["reply"] = plan["reply"] or "Tell me what kind of business and where, e.g. “barbers in Poland”."
    return plan


async def plan_message(text: str) -> dict:
    try:
        resp = await client().chat.completions.create(
            model=model(),
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text[:2000]}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "bot_action", "strict": True, "schema": PLAN_SCHEMA}},
            extra_body={"provider": {"require_parameters": True}},  # only providers that honour the schema
            max_tokens=1500,
            temperature=0.2,
        )
    except OpenAIError as e:
        raise LLMError(str(e)) from e
    if not resp.choices:
        raise LLMError("no choices returned")
    return normalise_plan(_json_from(resp.choices[0].message.content))


async def localise_pitch(template: str, language: str) -> str:
    """Translate the pitch into `language`, keeping {placeholders}. Returns the original on failure."""
    if not language or language == "en":
        return template
    placeholders = sorted(set(re.findall(r"\{[a-z_]+\}", template)))
    messages = [
        {"role": "system", "content": (
            "Translate the user's WhatsApp sales message into the language with ISO 639-1 code "
            f"'{language}'. Keep it friendly, natural and short, as a local would write it. Keep these "
            f"placeholders exactly as written, untranslated: {' '.join(placeholders) or '(none)'}. "
            "Reply with only the translated message — no quotes, notes or explanations.")},
        {"role": "user", "content": template},
    ]
    # Plain text, not JSON mode: DeepSeek's JSON mode occasionally returns empty content.
    problem = ""
    for _attempt in range(2):
        try:
            resp = await client().chat.completions.create(
                model=model(), messages=messages, max_tokens=800, temperature=0.3)
        except OpenAIError as e:
            problem = str(e)
            continue
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip().strip('"“”«»').strip()
        if not text:
            problem = "empty response"
        elif any(p not in text for p in placeholders):
            problem = "placeholder lost"  # never ship a pitch that lost the business name
        else:
            return text
    log.warning("Pitch translation to %s failed (%s); using the original", language, problem)
    return template
