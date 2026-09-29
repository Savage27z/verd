"""Daily digest: follow-ups due, hot leads, yesterday's numbers. No LLM, no search credits."""
import html
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import mockup, outreach, store, web

MAX_FOLLOWUPS = 2
HOT_WINDOW = timedelta(days=1)


def settings() -> tuple[ZoneInfo, time, int]:
    """(timezone, local send time, days to wait before following up)."""
    try:
        tz = ZoneInfo(os.getenv("DIGEST_TZ", "UTC").strip() or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", os.getenv("DIGEST_TIME", "09:00").strip())
    at = time(int(m.group(1)) % 24, int(m.group(2)) % 60) if m else time(9, 0)
    return tz, at, max(1, int(os.getenv("FOLLOWUP_DAYS", "3") or 3))


def sql_utc(dt: datetime) -> str:
    """Matches SQLite's datetime('now') format, so text comparison works."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def next_run(now: datetime, tz: ZoneInfo, at: time) -> datetime:
    local = now.astimezone(tz)
    run = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return run if run > local else run + timedelta(days=1)


def due_now(now: datetime, tz: ZoneInfo, at: time, last_sent: str | None) -> bool:
    """Past today's send time and not sent yet today — also catches up after a restart at 9:00."""
    local = now.astimezone(tz)
    return local.time() >= at and last_sent != local.date().isoformat()


def timing_hint(lead: dict, now: datetime) -> str:
    local = outreach.lead_local_time(lead, now)
    if local is None:
        return ""
    if 9 <= local.hour < 19:
        return f"🕘 {local:%H:%M} their time, good time to send"
    return f"🌙 {local:%H:%M} their time, better wait until ~9:00"


def days_since(sql_ts: str | None, now: datetime) -> int:
    if not sql_ts:
        return 0
    then = datetime.strptime(sql_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return max(0, (now - then).days)


def followup_link(lead: dict) -> str | None:
    """wa.me link with a follow-up in their language, including their preview if we can host one."""
    number = outreach.whatsapp_number(lead)
    if not number:
        return None
    url = web.preview_url(store.mockup_token(lead["id"])) if web.public_base_url() else ""
    t = mockup.strings(outreach.lead_language(lead))
    text = (t["followup_link"] if url else t["followup"]).format(name=lead.get("name") or "", url=url)
    return f"https://wa.me/{number}?text={quote(text)}"


@dataclass
class Digest:
    day: date
    due: list[dict] = field(default_factory=list)
    hot: list[dict] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)
    yesterday: dict[str, int] = field(default_factory=dict)
    pipeline: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.due or self.hot or self.closed)


def build(now: datetime | None = None) -> Digest:
    now = now or datetime.now(timezone.utc)
    tz, _, days = settings()
    cutoff = sql_utc(now - timedelta(days=days))
    # No reply 3 days after the last follow-up: stop chasing, keep the pipeline honest.
    closed = []
    for lead in store.exhausted_leads(cutoff, MAX_FOLLOWUPS):
        store.set_status(lead["id"], "lost", auto=True)
        closed.append(lead)
    hot = store.hot_leads(sql_utc(now - HOT_WINDOW))
    hot_ids = {lead["id"] for lead in hot}
    due = [lead for lead in store.followups_due(cutoff, MAX_FOLLOWUPS) if lead["id"] not in hot_ids]
    today = now.astimezone(tz).date()
    start = datetime.combine(today - timedelta(days=1), time(0), tz)
    yesterday = store.event_counts(sql_utc(start), sql_utc(start + timedelta(days=1)))
    return Digest(today, due=due, hot=hot, closed=closed, yesterday=yesterday, pipeline=store.pipeline_counts())


def summary_html(d: Digest) -> str:
    e = _escape
    y = d.yesterday
    lines = [f"☀️ <b>Your lead digest · {d.day:%a %d %b}</b>", ""]
    acts = [(y.get("contacted", 0), "📨 contacted"), (y.get("followup", 0), "🔁 followed up"),
            (y.get("replied", 0), "💬 replied"), (y.get("won", 0), "🏆 won")]
    done = " · ".join(f"{label} {n}" for n, label in acts if n)
    lines.append(f"<b>Yesterday:</b> {done or 'no activity'}")
    p = d.pipeline
    lines.append(f"<b>Pipeline:</b> 📨 {p.get('contacted', 0)} waiting · 💬 {p.get('replied', 0)} talking · "
                 f"🏆 {p.get('won', 0)} won · 🆕 {p.get('new', 0)} not contacted yet")
    lines.append("")
    if d.hot:
        lines.append(f"🔥 <b>{len(d.hot)} opened your preview</b> in the last day. Follow up while it's fresh!")
    if d.due:
        lines.append(f"⏰ <b>{len(d.due)} follow-up{'s' if len(d.due) != 1 else ''} due</b> (no reply yet)")
    if d.closed:
        names = ", ".join(e(lead["name"]) for lead in d.closed[:5]) + ("…" if len(d.closed) > 5 else "")
        lines.append(f"🗑 Moved {len(d.closed)} to Lost after {MAX_FOLLOWUPS} follow-ups with no reply: {names}")
    if d.empty:
        lines.append("Nothing to chase today. Go find fresh leads, e.g. <i>barbers in Lagos</i>.")
    return "\n".join(lines).rstrip()


def card_html(lead: dict, now: datetime, hot: bool = False) -> str:
    e = _escape
    n = int(lead.get("followups") or 0)
    head = (f"🔥 <b>{e(lead['name'])}</b> opened your preview {lead.get('preview_views', 1)}×" if hot
            else f"⏰ <b>{e(lead['name'])}</b> · follow-up {n + 1}/{MAX_FOLLOWUPS}")
    lines = [head]
    meta = [f"contacted {days_since(lead.get('contacted_at'), now)}d ago"]
    if lead.get("category"):
        meta.append(e(lead["category"]))
    lines.append(" · ".join(meta))
    phone = lead.get("intl_phone") or lead.get("phone")
    if phone:
        lines.append(f"📞 {e(phone)}")
    if lead.get("notes"):
        lines.append(f"📝 <i>{e(lead['notes'])}</i>")
    hint = timing_hint(lead, now)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def _escape(s) -> str:
    return html.escape(str(s or ""))
