"""Telegram bot: say what you want in plain words; it finds businesses with no website
anywhere in the world and helps you work them as a pipeline."""
import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime, timezone
from functools import wraps

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import digest, exports, llm, mockup, outreach, places, store, web

log = logging.getLogger("odify.bot")

DEFAULT_RESULTS = int(os.getenv("DEFAULT_RESULTS", "20"))        # one city
DEFAULT_WIDE_RESULTS = int(os.getenv("DEFAULT_WIDE_RESULTS", "60"))  # a whole country / region
MAX_RESULTS = 150
PAGE = 5
STATUS_EMOJI = {"new": "🆕", "contacted": "📨", "replied": "💬", "won": "🏆", "lost": "❌"}

HELP = f"""<b>Odify</b> — finds businesses on Google Maps with <b>no website</b>, anywhere in the world.

<b>Just tell me what you want</b>, in any language:
<code>barbers in poland</code>
<code>30 dentists around Kraków</code>
<code>car washes in Lagos, skip the ones with only instagram</code>
<code>show my won leads</code> · <code>export everything</code>

A whole country gets split across its biggest cities, searched in the local language.
Phone numbers get the right country code and your pitch is translated for WhatsApp.
Default {DEFAULT_RESULTS} results for a city, {DEFAULT_WIDE_RESULTS} for a country (max {MAX_RESULTS}).
Leads you already have are skipped unless you ask for them again.

<b>Work your leads</b>
• Tap a status under a lead (Contacted / Replied / Won / Lost)
• <b>Reply</b> to a lead card with text to save a note
• 💬 WhatsApp opens a chat with your pitch pre-filled
• 🎨 Mockup builds them a free one-page website preview to send — you get pinged when they open it

/leads — recent searches
/pipeline — leads by status
/template — view or change your pitch
/digest — today's follow-ups & hot leads (also sent every morning)
/export — every lead, as CSV"""


# ---- access control ----

def allowed_ids() -> set[int]:
    return {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USER_IDS", ""))}


def restricted(handler):
    """Personal bot: only ALLOWED_USER_IDS may use it. Strangers are told nothing useful."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user and user.id in allowed_ids():
            return await handler(update, context)
        if update.callback_query:
            await update.callback_query.answer("Not authorised", show_alert=True)
        elif update.effective_message:
            hint = ""
            if not allowed_ids():
                hint = f"\n\nOwner setup: set <code>ALLOWED_USER_IDS={user.id}</code> and restart."
            await update.effective_message.reply_text(
                f"This is a private bot.{hint}", parse_mode=ParseMode.HTML)
        log.warning("Rejected user %s (@%s)", user.id if user else "?", user.username if user else "?")
    return wrapper


# ---- understanding messages ----

def parse_query(text: str) -> dict | None:
    """No-LLM fallback: '30 barbers in Kraków --no-social' -> search params."""
    flags = re.compile(r"(?:^|\s)--?(no-?social|all|include-seen)(?=\s|$)", re.IGNORECASE)
    found = {f.lower().replace("nosocial", "no-social") for f in flags.findall(text)}
    include_social = "no-social" not in found
    exclude_seen = not found & {"all", "include-seen"}
    text = flags.sub(" ", text).strip()
    count = DEFAULT_RESULTS
    m = re.match(r"^(\d{1,3})\s+(.*)$", text)
    if m:
        count, text = int(m.group(1)), m.group(2)
    parts = re.split(r"\s+in\s+", text, flags=re.IGNORECASE)
    if len(parts) < 2:
        return None
    niche, location = " in ".join(parts[:-1]).strip(" ,"), parts[-1].strip(" ,")
    if not niche or not location:
        return None
    return {"niche": niche[:120], "location": location[:120], "count": max(1, min(MAX_RESULTS, count)),
            "include_social": include_social, "exclude_seen": exclude_seen}


def plan_from_query(q: dict) -> dict:
    return {
        "intent": "search", "reply": "", "status": "", "template_text": "",
        "niche": q["niche"], "location_label": q["location"], "country_iso": "", "language": "",
        "count": q["count"], "include_social": q["include_social"], "include_seen": not q["exclude_seen"],
        "queries": [f"{q['niche']} in {q['location']}"],
    }


async def understand(text: str) -> dict | None:
    """LLM plan when configured (falls back to the simple parser if the LLM fails)."""
    if llm.enabled():
        try:
            return await llm.plan_message(text)
        except llm.LLMError as err:
            log.warning("LLM planning failed, using simple parser: %s", err)
    q = parse_query(text)
    return plan_from_query(q) if q else None


# ---- rendering ----

def e(s) -> str:
    return html.escape(str(s or ""))


def pitch_for(lead: dict) -> str:
    """The pitch saved with the lead's search (translated), else the current template."""
    search = store.get_search(lead["search_id"]) if lead.get("search_id") else None
    return (search or {}).get("options", {}).get("pitch") or outreach.get_template()


def lead_card(lead: dict) -> str:
    lines = [f"<b>{lead['score']}</b> · <b>{e(lead['name']) or '—'}</b>"]
    meta = [e(lead["category"])] if lead.get("category") else []
    if lead.get("rating") is not None:
        meta.append(f"⭐ {lead['rating']} ({lead['reviews']})")
    if meta:
        lines.append(" · ".join(meta))
    phone = lead.get("intl_phone") or lead.get("phone")
    if phone:
        lines.append(f"📞 {e(phone)}")
    if lead.get("email"):
        lines.append(f"✉️ {e(lead['email'])}")
    if lead.get("address"):
        lines.append(f"📍 {e(lead['address'])}")
    if lead.get("web_presence") == "social" and lead.get("website"):
        lines.append(f'🔗 <a href="{e(lead["website"])}">Only a social/booking page</a>')
    if lead.get("notes"):
        lines.append(f"📝 <i>{e(lead['notes'])}</i>")
    views = store.mockup_views(lead["id"]) if lead.get("id") else 0
    if views:
        lines.append(f"👀 Opened their website preview {views}×")
    lines.append(f"{STATUS_EMOJI[lead['status']]} {lead['status'].capitalize()}")
    return "\n".join(lines)


def lead_keyboard(lead: dict, pitch: str | None = None) -> InlineKeyboardMarkup:
    row1, row2 = [], []
    wa = outreach.whatsapp_link(lead, pitch or pitch_for(lead))
    if wa:
        row1.append(InlineKeyboardButton("💬 WhatsApp", url=wa))
    row1.append(InlineKeyboardButton("🎨 Mockup", callback_data=f"mk:{lead['id']}"))
    if lead.get("maps_url"):
        row2.append(InlineKeyboardButton("🗺 Maps", url=lead["maps_url"]))
    row2.append(InlineKeyboardButton("👤 Contact", callback_data=f"vc:{lead['id']}"))
    statuses = [
        InlineKeyboardButton(("✓ " if lead["status"] == s else "") + s.capitalize(), callback_data=f"st:{lead['id']}:{s}")
        for s in ("contacted", "replied", "won", "lost")
    ]
    return InlineKeyboardMarkup([row1, row2, statuses[:2], statuses[2:]])


async def send_mockup(chat_id: int, lead: dict, context: ContextTypes.DEFAULT_TYPE):
    url = web.preview_url(store.mockup_token(lead["id"]))
    lang = outreach.lead_language(lead)
    buttons = [[InlineKeyboardButton("🌐 Open preview", url=url)]]
    send = mockup.send_link(lead, url, lang)
    if send:
        buttons.append([InlineKeyboardButton("💬 Send preview on WhatsApp", url=send)])
    await context.bot.send_message(
        chat_id,
        f"🎨 <b>Website preview for {e(lead['name'])}</b>\n{e(url)}\n\n"
        "Ask first (💬 WhatsApp on the lead card); once they say yes, tap <b>Send preview</b>. "
        "I'll tell you when they open it.",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True,
    )


def search_summary(search: dict, leads: list[dict]) -> str:
    phones = sum(1 for lead in leads if lead.get("phone") or lead.get("intl_phone"))
    wa = sum(1 for lead in leads if outreach.whatsapp_number(lead))
    emails = sum(1 for lead in leads if lead.get("email"))
    social = sum(1 for lead in leads if lead.get("web_presence") == "social")
    head = f"<b>{len(leads)} leads</b> — {e(search['niche'])} in {e(search['location'])}"
    if not leads:
        return head + "\nNo businesses without a website found. Try a nearby area or a broader niche."
    lines = [head, f"📞 {phones} with phone · 💬 {wa} WhatsApp-ready · ✉️ {emails} emails · 🔗 {social} social-only"]
    queries = search.get("options", {}).get("queries") or []
    if len(queries) > 1:
        lines.append(f"🗺 Searched {len(queries)} areas: " + ", ".join(e(q) for q in queries))
    return "\n".join(lines)


def search_keyboard(search_id: int, total: int, offset: int = 0) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📄 CSV", callback_data=f"csv:{search_id}"),
             InlineKeyboardButton("👥 All contacts (.vcf)", callback_data=f"vcf:{search_id}")]]
    if offset < total:
        label = "📋 Show leads" if offset == 0 else f"⬇️ Next {min(PAGE, total - offset)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"more:{search_id}:{offset}")])
    return InlineKeyboardMarkup(rows)


async def send_lead(chat_id: int, lead: dict, context: ContextTypes.DEFAULT_TYPE, pitch: str | None = None):
    msg = await context.bot.send_message(
        chat_id, lead_card(lead), parse_mode=ParseMode.HTML, reply_markup=lead_keyboard(lead, pitch),
        disable_web_page_preview=True,
    )
    store.link_message(chat_id, msg.message_id, lead["id"])


async def send_page(chat_id: int, search_id: int, offset: int, context: ContextTypes.DEFAULT_TYPE):
    leads = store.get_leads(search_id)
    pitch = pitch_for(leads[0]) if leads else None
    for lead in leads[offset:offset + PAGE]:
        await send_lead(chat_id, lead, context, pitch)
    nxt = offset + PAGE
    if nxt < len(leads):
        await context.bot.send_message(
            chat_id, f"Showing {nxt} of {len(leads)}", reply_markup=search_keyboard(search_id, len(leads), nxt))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "leads"


# ---- handlers ----

@restricted
async def cmd_start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


@restricted
async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await route(update, context, " ".join(context.args or []))


@restricted
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    reply_to = msg.reply_to_message
    if reply_to:
        lead_id = store.lead_for_message(msg.chat_id, reply_to.message_id)
        if lead_id:
            lead = store.set_notes(lead_id, msg.text.strip())
            await msg.reply_text("📝 Note saved")
            try:
                await reply_to.edit_text(lead_card(lead), parse_mode=ParseMode.HTML,
                                         reply_markup=lead_keyboard(lead), disable_web_page_preview=True)
            except Exception:
                pass  # card too old to edit — the note is saved regardless
            return
    await route(update, context, msg.text)


async def route(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    msg = update.effective_message
    if not text.strip():
        await msg.reply_text("Tell me what and where, e.g. <code>barbers in Poland</code>", parse_mode=ParseMode.HTML)
        return
    plan = await understand(text)
    if plan is None:
        hint = "" if llm.enabled() else "\n(Set OPENROUTER_API_KEY to let me understand free-form messages.)"
        await msg.reply_text(
            f"Tell me what and where, e.g. <code>barbers in Poland</code>{e(hint)}", parse_mode=ParseMode.HTML)
        return
    intent = plan["intent"]
    if intent == "search":
        await run_search(update, context, plan)
    elif intent == "pipeline":
        if plan["status"]:
            leads = store.leads_by_status(plan["status"], limit=10)
            if not leads:
                await msg.reply_text(f"No {plan['status']} leads yet.")
            for lead in leads:
                await send_lead(msg.chat_id, lead, context)
        else:
            await show_pipeline(update)
    elif intent == "recent":
        await show_recent(update)
    elif intent == "export":
        await send_export(update, context)
    elif intent == "template":
        if plan["template_text"]:
            outreach.set_template(plan["template_text"])
            await msg.reply_text(f"✅ Pitch saved:\n\n{plan['template_text']}")
        else:
            await show_template(update)
    elif intent == "help":
        await msg.reply_text(HELP, parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text(plan["reply"] or "I find businesses without websites — try “barbers in Poland”.")


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: dict):
    msg = update.effective_message
    queries = plan["queries"]
    count = plan["count"] or (DEFAULT_WIDE_RESULTS if len(queries) > 1 else DEFAULT_RESULTS)
    count = max(1, min(MAX_RESULTS, count))
    where = plan["location_label"] or ", ".join(queries)
    areas = f", {len(queries)} areas" if len(queries) > 1 else ""
    status = await msg.reply_text(
        f"🔎 Looking for <b>{e(plan['niche'])}</b> in <b>{e(where)}</b> (up to {count}{areas})…",
        parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()

    def progress(text: str):
        asyncio.run_coroutine_threadsafe(
            status.edit_text(f"🔎 {e(plan['niche'])} in {e(where)}\n{e(text)}", parse_mode=ParseMode.HTML), loop)

    seen = set() if plan["include_seen"] else store.seen_place_ids()
    started = time.monotonic()
    search_job = asyncio.to_thread(
        places.search_queries, queries, count,
        include_social=plan["include_social"], exclude_place_ids=seen, region=plan["country_iso"],
        language=plan["language"], email_location=where, progress=progress,
    )
    pitch_job = outreach.template_for_language(plan["language"])
    try:
        leads, pitch = await asyncio.gather(search_job, pitch_job)
    except places.PlacesError as err:
        log.warning("Search for %r in %r failed: %s", plan["niche"], where, err)
        await status.edit_text(f"⚠️ Search error:\n<code>{e(err)}</code>", parse_mode=ParseMode.HTML)
        return
    except Exception as err:
        log.exception("Search failed")
        await status.edit_text(f"⚠️ Search failed: {e(err)}", parse_mode=ParseMode.HTML)
        return

    options = {"include_social": plan["include_social"], "exclude_seen": not plan["include_seen"],
               "queries": queries, "country": plan["country_iso"], "language": plan["language"]}
    if pitch and pitch != outreach.get_template():
        options["pitch"] = pitch
    search_id = store.save_search(plan["niche"], where, count, options, leads)
    search = store.get_search(search_id)
    saved = store.get_leads(search_id)
    log.info("Search %d: %r in %r (%s, %s) — %d queries, %d leads | phone %d, WhatsApp %d, email %d, "
             "social/booking %d | skipped-known %d | pitch %s | %.0fs",
             search_id, plan["niche"], where, plan["country_iso"] or "?", plan["language"] or "?", len(queries),
             len(saved), sum(1 for x in saved if x["phone"] or x["intl_phone"]),
             sum(1 for x in saved if outreach.whatsapp_number(x)), sum(1 for x in saved if x["email"]),
             sum(1 for x in saved if x["web_presence"] == "social"), len(seen),
             "translated" if "pitch" in options else "default", time.monotonic() - started)
    note = ""
    if seen:
        note += "\n<i>Skipped leads you already have — say “include ones I already have” to get them too.</i>"
    if "pitch" in options:
        note += f"\n<i>WhatsApp pitch translated ({e(plan['language'])}).</i>"
    await status.edit_text(search_summary(search, saved) + note, parse_mode=ParseMode.HTML,
                           reply_markup=search_keyboard(search_id, len(saved)) if saved else None)
    if saved:
        await send_page(msg.chat_id, search_id, 0, context)


async def show_recent(update: Update):
    searches = store.list_searches(10)
    if not searches:
        await update.effective_message.reply_text("No searches yet — try: barbers in Poland")
        return
    rows = [[InlineKeyboardButton(f"{s['niche']} · {s['location']} ({s['returned']})", callback_data=f"open:{s['id']}")]
            for s in searches]
    await update.effective_message.reply_text("Recent searches:", reply_markup=InlineKeyboardMarkup(rows))


async def show_pipeline(update: Update):
    counts = store.pipeline_counts()
    text = "<b>Pipeline</b>\n" + "\n".join(f"{STATUS_EMOJI[s]} {s.capitalize()}: {n}" for s, n in counts.items())
    rows = [[InlineKeyboardButton(f"{STATUS_EMOJI[s]} {s.capitalize()} ({n})", callback_data=f"pl:{s}")]
            for s, n in counts.items() if n and s != "new"]
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                              reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def show_template(update: Update):
    await update.effective_message.reply_text(
        f"<b>Your pitch</b> (translated automatically for each country)\n\n{e(outreach.get_template())}\n\n"
        f"Change it: <code>/template Hi {{name}}, …</code> — or just tell me the new message.\n"
        f"Reset: <code>/template reset</code>\n"
        f"Variables: {' '.join(f'<code>{v}</code>' for v in outreach.TEMPLATE_VARS)}",
        parse_mode=ParseMode.HTML)


async def send_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leads = store.all_leads()
    if not leads:
        await update.effective_message.reply_text("Nothing to export yet.")
        return
    await context.bot.send_document(update.effective_chat.id, exports.generate_csv(leads).encode("utf-8-sig"),
                                    filename="odify-all-leads.csv", caption=f"{len(leads)} leads")


@restricted
async def cmd_leads(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await show_recent(update)


@restricted
async def cmd_pipeline(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await show_pipeline(update)


@restricted
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_export(update, context)


# ---- daily digest ----

def digest_keyboard(lead: dict) -> InlineKeyboardMarkup:
    row1 = []
    link = digest.followup_link(lead)
    if link:
        row1.append(InlineKeyboardButton("💬 Send follow-up", url=link))
    row1.append(InlineKeyboardButton("✅ Followed up", callback_data=f"fu:{lead['id']}"))
    return InlineKeyboardMarkup([row1, [
        InlineKeyboardButton("💬 They replied", callback_data=f"st:{lead['id']}:replied"),
        InlineKeyboardButton("❌ Lost", callback_data=f"st:{lead['id']}:lost"),
    ]])


async def send_digest(tg_bot, chat_id: int, now: datetime | None = None) -> digest.Digest:
    now = now or datetime.now(timezone.utc)
    d = digest.build(now)
    await tg_bot.send_message(chat_id, digest.summary_html(d), parse_mode=ParseMode.HTML)
    for lead, hot in [(lead, True) for lead in d.hot[:10]] + [(lead, False) for lead in d.due[:10]]:
        await tg_bot.send_message(chat_id, digest.card_html(lead, now, hot=hot), parse_mode=ParseMode.HTML,
                                  reply_markup=digest_keyboard(lead), disable_web_page_preview=True)
    if len(d.due) > 10:
        await tg_bot.send_message(chat_id, f"…and {len(d.due) - 10} more follow-ups due. Clear these first!")
    return d


async def digest_loop(app: Application):
    """Sends the digest daily at DIGEST_TIME in DIGEST_TZ (catching up after a restart)."""
    while True:
        try:
            tz, at, _ = digest.settings()
            now = datetime.now(timezone.utc)
            if digest.due_now(now, tz, at, store.get_setting("digest_last_sent")):
                store.set_setting("digest_last_sent", now.astimezone(tz).date().isoformat())
                for uid in allowed_ids():
                    await send_digest(app.bot, uid, now)
                log.info("Daily digest sent")
            wait = (digest.next_run(datetime.now(timezone.utc), tz, at) - datetime.now(timezone.utc)).total_seconds()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Daily digest failed")
            wait = 600
        await asyncio.sleep(max(30.0, min(wait, 3600.0)))  # re-check hourly: survives clock/setting changes


@restricted
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_digest(context.bot, update.effective_chat.id)


@restricted
async def cmd_template(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    new = msg.text.partition(" ")[2].strip()
    if new.lower() == "reset":
        outreach.set_template(None)
        await msg.reply_text("Pitch reset to default.")
    elif new:
        outreach.set_template(new)
        await msg.reply_text("✅ Pitch saved.")
    else:
        await show_template(update)


@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kind, _, rest = (query.data or "").partition(":")
    chat_id = query.message.chat_id

    if kind == "st":
        lead_id, status = rest.split(":")
        lead = store.set_status(int(lead_id), status)
        if not lead:
            await query.answer("Lead not found")
            return
        await query.answer(f"Marked {status}")
        await query.edit_message_text(lead_card(lead), parse_mode=ParseMode.HTML,
                                      reply_markup=lead_keyboard(lead), disable_web_page_preview=True)
    elif kind == "vc":
        lead = store.get_lead(int(rest))
        await query.answer()
        if lead:
            await context.bot.send_document(chat_id, exports.generate_vcard(lead).encode(),
                                            filename=f"{slugify(lead['name'])}.vcf")
    elif kind == "fu":
        lead = store.mark_followed_up(int(rest))
        if not lead or lead["status"] != "contacted":
            await query.answer("This lead isn't waiting on a reply anymore")
            return
        _, _, days = digest.settings()
        await query.answer("Follow-up logged")
        last = lead["followups"] >= digest.MAX_FOLLOWUPS
        await query.edit_message_text(
            f"✅ Followed up with <b>{e(lead['name'])}</b> ({lead['followups']}/{digest.MAX_FOLLOWUPS}). "
            + (f"If there's no reply in {days} days I'll move them to Lost." if last
               else f"I'll remind you again in {days} days if they don't reply."),
            parse_mode=ParseMode.HTML)
    elif kind == "mk":
        lead = store.get_lead(int(rest))
        if not lead:
            await query.answer("Lead not found")
        elif not web.public_base_url():
            await query.answer("Previews need a public URL: generate a domain for this service on Railway "
                               "(or set PUBLIC_URL), then try again.", show_alert=True)
        else:
            await query.answer()
            await send_mockup(chat_id, lead, context)
    elif kind in ("csv", "vcf"):
        search = store.get_search(int(rest))
        await query.answer()
        if not search:
            return
        leads = store.get_leads(search["id"])
        name = f"odify-{slugify(search['niche'] + '-' + search['location'])}"
        if kind == "csv":
            await context.bot.send_document(chat_id, exports.generate_csv(leads).encode("utf-8-sig"),
                                            filename=f"{name}.csv")
        else:
            await context.bot.send_document(chat_id, exports.generate_all_vcards(leads).encode(),
                                            filename=f"{name}.vcf")
    elif kind == "more":
        search_id, offset = map(int, rest.split(":"))
        await query.answer()
        await query.edit_message_reply_markup(None)
        await send_page(chat_id, search_id, offset, context)
    elif kind == "open":
        search = store.get_search(int(rest))
        await query.answer()
        if search:
            leads = store.get_leads(search["id"])
            await context.bot.send_message(chat_id, search_summary(search, leads), parse_mode=ParseMode.HTML,
                                           reply_markup=search_keyboard(search["id"], len(leads)))
    elif kind == "pl":
        await query.answer()
        for lead in store.leads_by_status(rest, limit=10):
            await send_lead(chat_id, lead, context)
    else:
        await query.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


async def notify_view(app: Application, lead: dict, views: int):
    """A lead opened their preview. Only worth a ping once you've actually contacted them —
    before that, the opens are you checking the page."""
    log.info("Preview opened: lead %s %r (view %d, status %s)", lead["id"], lead["name"], views, lead["status"])
    if lead["status"] == "new":
        return
    for uid in allowed_ids():
        await app.bot.send_message(
            uid, f"👀 <b>{e(lead['name'])}</b> just opened their website preview (view {views}). "
                 "Good moment to follow up!",
            parse_mode=ParseMode.HTML, reply_markup=lead_keyboard(lead), disable_web_page_preview=True)


async def _post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("leads", "Recent searches"),
        BotCommand("pipeline", "Leads by status"),
        BotCommand("digest", "Today's follow-ups & hot leads"),
        BotCommand("template", "View/change your pitch"),
        BotCommand("export", "All leads as CSV"),
        BotCommand("help", "How to use"),
    ])
    loop = asyncio.get_running_loop()

    def on_view(lead: dict, views: int):  # called from the web server thread
        asyncio.run_coroutine_threadsafe(notify_view(app, lead, views), loop)

    try:
        app.bot_data["web"] = web.start(on_view)
    except OSError as err:
        log.error("Preview server could not start: %s", err)
    tz, at, days = digest.settings()
    app.bot_data["digest_task"] = asyncio.create_task(digest_loop(app))
    log.info("Daily digest at %s %s (follow-ups after %d days)", at.strftime("%H:%M"), tz.key, days)


async def _post_shutdown(app: Application):
    task = app.bot_data.get("digest_task")
    if task:
        task.cancel()


def build_app(token: str) -> Application:
    app = (Application.builder().token(token).concurrent_updates(True)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("leads", cmd_leads))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CommandHandler("template", cmd_template))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
