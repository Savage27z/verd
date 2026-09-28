# Odify Bot

A personal Telegram bot that finds businesses on Google Maps **with no website**, the people who need one, and helps you work them as leads.

Type `barbers in Ikeja, Lagos` and you get:

- Open businesses with no website, plus those with **only a Facebook/Instagram/Linktree page**
- Phone, international number, address, rating and review count, and a best-effort email
- A **lead score** (0–100): established businesses you can actually reach come first
- A card per lead with **💬 WhatsApp** (your pitch pre-filled), **🗺 Maps**, **👤 save contact**, and status buttons
- CSV and `.vcf` (all contacts) export
- Leads you already have are skipped on later searches, so you never pay twice for the same lead

Built on the lead engine from [callmidavid/odify](https://github.com/callmidavid/odify), with the pricing, accounts and web app removed.

## Setup (5 minutes)

1. **Bot token**: message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. **Search key**: pick one.
   - **Serper (easiest)**: sign up at [serper.dev](https://serper.dev) and copy the API key from the dashboard. You don't need a card or a Google Cloud account. Put it in `SERPER_API_KEY`.
   - **Google**: in [Google Cloud Console](https://console.cloud.google.com/apis/library), enable **Places API (New)** (this needs billing turned on), then create an API key restricted to that API. Put it in `GOOGLE_PLACES_API_KEY`.
3. **Configure**:
   ```bash
   cp .env.example .env    # fill in TELEGRAM_BOT_TOKEN, your search key, DEFAULT_COUNTRY_CODE
   python -m venv venv
   venv/Scripts/pip install -r requirements.txt     # Windows
   # source venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
   ```
4. **Run** `python -m odify_bot` and message your bot. The first time, it replies with your Telegram user ID. Put that in `ALLOWED_USER_IDS` in `.env` and restart. Nobody else can use the bot.

## Usage

| You send | It does |
|---|---|
| `barbers in Ikeja, Lagos` | Search (default 20 results) |
| `40 dentists in Austin, TX` | Search for up to 40 (max 60, Google's limit) |
| `… --no-social` | Skip businesses that only have a social page |
| `… --all` | Include leads you already got before |
| Tap **Contacted / Replied / Won / Lost** | Moves the lead through your pipeline |
| *Reply* to a lead card with text | Saves it as a note on that lead |
| `/leads` | Recent searches (re-open, export) |
| `/pipeline` | Counts by status; tap to list leads |
| `/template` | View or change the WhatsApp/email pitch (`{name}`, `{category}`, `{rating}`, `{reviews}`, `{rating_line}`) |
| `/export` | Every lead, with status and notes, as CSV |

## Hosting

The bot uses long polling, so it needs no domain, webhook or open port.

- **Your PC**: just leave `python -m odify_bot` running.
- **Railway**: create a service from this repo (it uses the Dockerfile). Set the variables from `.env.example`, **add a volume mounted at `/data`** (otherwise your leads are wiped on every deploy) and keep it at 1 replica. It needs no domain or port.
- **Anywhere else with Docker**: mount a volume at `/data` so the SQLite database survives redeploys:
  ```bash
  docker build -t odify-bot .
  docker run -d --env-file .env -v odify-data:/data --restart unless-stopped odify-bot
  ```

## Costs

Either provider makes **at most 3 requests per search**, whatever the result count.

- **Serper**: each request uses Serper credits (see your serper.dev dashboard). New accounts get free credits.
- **Google**: bills Places API (New) Text Search and includes a monthly free allowance. Set a budget alert in Cloud billing. If your key only has the *old* Places API enabled, the bot falls back to it automatically, and that costs much more (one Details call per business). Set `PLACES_API_MODE=new` to forbid the fallback.

**WhatsApp buttons** need an international number. Google usually provides one, but Serper often returns local numbers like `0803…`. Set `DEFAULT_COUNTRY_CODE` (e.g. `234` for Nigeria) and the bot converts them.

## Development

```bash
venv/Scripts/python -m pytest tests -q
```

Code map:
- `odify_bot/places.py`: Serper / Google search, website/social classification, lead scoring
- `odify_bot/emails.py`: email discovery
- `odify_bot/exports.py`: CSV and vCard
- `odify_bot/store.py`: SQLite
- `odify_bot/bot.py`: Telegram handlers
