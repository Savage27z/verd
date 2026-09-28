"""Telegram bot: find businesses without a website, work them as a pipeline."""
import asyncio
import html
import logging
import os
import re
from functools import wraps

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import exports, outreach, places, store

log = logging.getLogger("odify.bot")

DEFAULT_RESULTS = int(os.getenv("DEFAULT_RESULTS", "20"))
MAX_RESULTS = 60  # Google text search never returns more than 60 places
PAGE = 5
STATUS_EMOJI = {"new": "🆕", "contacted": "📨", "replied": "💬", "won": "🏆", "lost": "❌"}

HELP = f"""<b>Odify</b> — find businesses on Google Maps with no website.

<b>Search</b> — just type it:
<code>barbers in Ikeja, Lagos</code>
<code>30 dentists in Austin, TX</code>  (how many, max {MAX_RESULTS}; default {DEFAULT_RESULTS})

Add to the end:
<code>--no-social</code> skip businesses that only have a Facebook/Instagram page
<code>--all</code> include leads you already got from earlier searches

<b>Work your leads</b>
• Tap a status under a lead (Contacted / Replied / Won / Lost)
• <b>Reply</b> to a lead card with text to save a note
• 💬 WhatsApp opens a chat with your outreach message pre-filled

/leads — recent searches
/pipeline — leads by status
/template — view or change your outreach message
/export — every lead you have, as CSV"""


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
                hint = f"\n\nOwner setup: put <code>ALLOWED_USER_IDS={user.id}</code> in .env and restart."
            await update.effective_message.reply_text(
                f"This is a private bot.{hint}", parse_mode=ParseMode.HTML)
        log.warning("Rejected user %s (@%s)", user.id if user else "?", user.username if user else "?")
    return wrapper


# ---- query parsing ----

def parse_query(text: str) -> dict | None:
    """'30 barbers in Ikeja, Lagos --no-social' -> {niche, location, count, include_social, exclude_seen}."""
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


# ---- rendering ----

def e(s) -> str:
    return html.escape(str(s or ""))


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
        lines.append(f'🔗 <a href="{e(lead["website"])}">Social page only</a>')
    if lead.get("notes"):
        lines.append(f"📝 <i>{e(lead['notes'])}</i>")
    lines.append(f"{STATUS_EMOJI[lead['status']]} {lead['status'].capitalize()}")
    return "\n".join(lines)


def lead_keyboard(lead: dict) -> InlineKeyboardMarkup:
    row1 = []
    wa = outreach.whatsapp_link(lead)
    if wa:
        row1.append(InlineKeyboardButton("💬 WhatsApp", url=wa))
    if lead.get("maps_url"):
        row1.append(InlineKeyboardButton("🗺 Maps", url=lead["maps_url"]))
    row1.append(InlineKeyboardButton("👤 Contact", callback_data=f"vc:{lead['id']}"))
    statuses = [
        InlineKeyboardButton(("✓ " if lead["status"] == s else "") + s.capitalize(), callback_data=f"st:{lead['id']}:{s}")
        for s in ("contacted", "replied", "won", "lost")
    ]
    return InlineKeyboardMarkup([row1, statuses[:2], statuses[2:]])


def search_summary(search: dict, leads: list[dict]) -> str:
    phones = sum(1 for lead in leads if lead.get("phone") or lead.get("intl_phone"))
    wa = sum(1 for lead in leads if outreach.whatsapp_number(lead))
    emails = sum(1 for lead in leads if lead.get("email"))
    social = sum(1 for lead in leads if lead.get("web_presence") == "social")
    head = f"<b>{len(leads)} leads</b> — {e(search['niche'])} in {e(search['location'])}"
    if not leads:
        return head + "\nNo businesses without a website found. Try a nearby area or a broader niche."
    return (f"{head}\n📞 {phones} with phone · 💬 {wa} WhatsApp-ready · ✉️ {emails} emails"
            f" · 🔗 {social} social-only")


def search_keyboard(search_id: int, total: int, offset: int = 0) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📄 CSV", callback_data=f"csv:{search_id}"),
             InlineKeyboardButton("👥 All contacts (.vcf)", callback_data=f"vcf:{search_id}")]]
    if offset < total:
        label = "📋 Show leads" if offset == 0 else f"⬇️ Next {min(PAGE, total - offset)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"more:{search_id}:{offset}")])
    return InlineKeyboardMarkup(rows)


async def send_lead(chat_id: int, lead: dict, context: ContextTypes.DEFAULT_TYPE):
    msg = await context.bot.send_message(
        chat_id, lead_card(lead), parse_mode=ParseMode.HTML, reply_markup=lead_keyboard(lead),
        disable_web_page_preview=True,
    )
    store.link_message(chat_id, msg.message_id, lead["id"])


async def send_page(chat_id: int, search_id: int, offset: int, context: ContextTypes.DEFAULT_TYPE):
    leads = store.get_leads(search_id)
    for lead in leads[offset:offset + PAGE]:
        await send_lead(chat_id, lead, context)
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
    await run_search(update, context, " ".join(context.args or []))


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
    await run_search(update, context, msg.text)


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    msg = update.effective_message
    q = parse_query(text)
    if not q:
        await msg.reply_text(
            "Tell me what and where, e.g. <code>barbers in Ikeja, Lagos</code>  (/help for more)",
            parse_mode=ParseMode.HTML)
        return
    status = await msg.reply_text(
        f"🔎 Looking for <b>{e(q['niche'])}</b> in <b>{e(q['location'])}</b> (up to {q['count']})…",
        parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()

    def progress(text: str):
        asyncio.run_coroutine_threadsafe(
            status.edit_text(f"🔎 {e(q['niche'])} in {e(q['location'])}\n{e(text)}", parse_mode=ParseMode.HTML),
            loop)

    try:
        seen = store.seen_place_ids() if q["exclude_seen"] else set()
        leads = await asyncio.to_thread(
            places.search_without_website, q["niche"], q["location"], q["count"],
            include_social=q["include_social"], exclude_place_ids=seen, progress=progress,
        )
    except places.PlacesError as err:
        await status.edit_text(f"⚠️ Google Places error:\n<code>{e(err)}</code>", parse_mode=ParseMode.HTML)
        return
    except Exception as err:
        log.exception("Search failed")
        await status.edit_text(f"⚠️ Search failed: {e(err)}", parse_mode=ParseMode.HTML)
        return

    options = {"include_social": q["include_social"], "exclude_seen": q["exclude_seen"]}
    search_id = store.save_search(q["niche"], q["location"], q["count"], options, leads)
    search = store.get_search(search_id)
    saved = store.get_leads(search_id)
    note = ""
    if q["exclude_seen"] and seen:
        note = "\n<i>Skipped leads you already have — add --all to include them.</i>"
    await status.edit_text(search_summary(search, saved) + note, parse_mode=ParseMode.HTML,
                           reply_markup=search_keyboard(search_id, len(saved)) if saved else None)
    if saved:
        await send_page(msg.chat_id, search_id, 0, context)


@restricted
async def cmd_leads(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    searches = store.list_searches(10)
    if not searches:
        await update.effective_message.reply_text("No searches yet — type something like: barbers in Ikeja")
        return
    rows = [[InlineKeyboardButton(f"{s['niche']} · {s['location']} ({s['returned']})", callback_data=f"open:{s['id']}")]
            for s in searches]
    await update.effective_message.reply_text("Recent searches:", reply_markup=InlineKeyboardMarkup(rows))


@restricted
async def cmd_pipeline(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    counts = store.pipeline_counts()
    text = "<b>Pipeline</b>\n" + "\n".join(f"{STATUS_EMOJI[s]} {s.capitalize()}: {n}" for s, n in counts.items())
    rows = [[InlineKeyboardButton(f"{STATUS_EMOJI[s]} {s.capitalize()} ({n})", callback_data=f"pl:{s}")]
            for s, n in counts.items() if n and s != "new"]
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                              reply_markup=InlineKeyboardMarkup(rows) if rows else None)


@restricted
async def cmd_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    new = msg.text.partition(" ")[2].strip()
    if new.lower() == "reset":
        outreach.set_template(None)
        await msg.reply_text("Template reset to default.")
        return
    if new:
        outreach.set_template(new)
        await msg.reply_text("✅ Template saved.")
        return
    await msg.reply_text(
        f"<b>Your outreach message</b>\n\n{e(outreach.get_template())}\n\n"
        f"Change it: <code>/template Hi {{name}}, …</code>\nReset: <code>/template reset</code>\n"
        f"Variables: {' '.join(f'<code>{v}</code>' for v in outreach.TEMPLATE_VARS)}",
        parse_mode=ParseMode.HTML)


@restricted
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leads = store.all_leads()
    if not leads:
        await update.effective_message.reply_text("Nothing to export yet.")
        return
    await context.bot.send_document(update.effective_chat.id, exports.generate_csv(leads).encode("utf-8-sig"),
                                    filename="odify-all-leads.csv", caption=f"{len(leads)} leads")


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
    elif kind in ("csv", "vcf"):
        search = store.get_search(int(rest))
        await query.answer()
        if not search:
            return
        leads = store.get_leads(search["id"])
        if kind == "csv":
            data, name = exports.generate_csv(leads).encode("utf-8-sig"), f"odify-{slugify(search['niche'] + '-' + search['location'])}.csv"
        else:
            data, name = exports.generate_all_vcards(leads).encode(), f"odify-{slugify(search['niche'] + '-' + search['location'])}.vcf"
        await context.bot.send_document(chat_id, data, filename=name)
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


async def _post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("leads", "Recent searches"),
        BotCommand("pipeline", "Leads by status"),
        BotCommand("template", "View/change outreach message"),
        BotCommand("export", "All leads as CSV"),
        BotCommand("help", "How to use"),
    ])


def build_app(token: str) -> Application:
    app = (Application.builder().token(token).concurrent_updates(True).post_init(_post_init).build())
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("leads", cmd_leads))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CommandHandler("template", cmd_template))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
