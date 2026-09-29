# Odify Bot

A personal Telegram bot that finds businesses on Google Maps **with no website**, the people who need one, **in any country**, and helps you work them as leads.

Just text it like a person:

> barbers in poland
> 30 dentists around São Paulo
> car washes in Lagos, skip the ones with only instagram
> show my won leads

It then does the following:

- **Understands the message** in any language, using DeepSeek via OpenRouter.
- **Covers whole countries.** "barbers in poland" becomes searches across Poland's biggest cities, written the way locals type them ("fryzjer męski Warszawa", "fryzjer męski Kraków", …). The results are merged and deduplicated.
- Returns open businesses with **no website**, plus those with **only a Facebook/Instagram/Linktree page**. You get phone, address, rating and review count, and a best-effort email for each.
- **Fixes phone numbers for that country** (+48, +55, +254, …) so the **💬 WhatsApp** button works, and **translates your pitch** into the local language.
- Ranks leads by a **lead score** (0–100): established businesses you can actually reach come first.
- Gives each lead a card with **WhatsApp**, **Maps**, **save contact**, and **Contacted / Replied / Won / Lost** buttons. Reply to a card with text to save a note on it.
- Exports each search, or everything, as **CSV** and **.vcf**.
- **Skips leads you already have** on later searches, so you never pay twice for the same lead.

Built on the lead engine from [callmidavid/odify](https://github.com/callmidavid/odify), with the pricing, accounts and web app removed.

## Website previews (🎨 Mockup)

Tap **🎨 Mockup** on any lead to get a one-page website for that business at `https://<your-domain>/m/<random-code>`. It includes their name, rating and reviews, call/WhatsApp/booking buttons, a map and contact details, with its fixed text in the lead's language (en, pl, de, es, pt, fr, it, nl; others fall back to English). The page is built from the lead's data with **no LLM**, and the bot serves it itself.

- **Two-step pitch:** first ask whether they'd like to see it (💬 WhatsApp on the card). When they say yes, tap **Send preview on WhatsApp**.
- **Open alerts:** once a lead is marked Contacted, the bot pings you when they open their preview (at most every 30 min). WhatsApp and Telegram link-preview bots don't count as opens.
- **Privacy:** preview links can't be guessed, and pages are `noindex` so search engines don't list them.
- **What it shows:** their Google photo as the header, opening hours with today highlighted and a live "Open now" badge (in their timezone), a reviews section, and a "Book online" button using their Google booking link. Leads saved before this update have no photo or hours, so their previews fall back to the colour-and-icon header until you search that area again.
- **Setup:** give the service a public domain (Railway → service → Settings → Networking → Generate Domain, port 8080). Set `MOCKUP_BRAND` to your studio name, and optionally `MOCKUP_BRAND_URL` to link it (e.g. your WhatsApp).

## Daily digest

Every morning at `DIGEST_TIME` in `DIGEST_TZ`, the bot sends you one message. If it was down at that time, it sends as soon as it's back, once per day. `/digest` sends one whenever you like. It uses no LLM and no search credits.

- **🔥 Hot:** leads you've contacted who opened their website preview in the last day.
- **⏰ Follow-ups due:** leads marked Contacted `FOLLOWUP_DAYS` (default 3) or more days ago with no reply. Each card has:
  - a ready follow-up message in the lead's language, including their preview link
  - their local time, worked out from the phone number, so you don't message someone at 5am
  - **✅ Followed up**, **💬 They replied** and **❌ Lost** buttons
- **Auto-close:** after 2 follow-ups with no reply, the lead moves to Lost.
- **Yesterday's numbers:** contacted, followed up, replied and won, plus your pipeline totals.

## Setup

You need three keys:

| Key | Where | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` | |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) | Uses `deepseek/deepseek-v4.1-flash` by default. Change it with `OPENROUTER_MODEL`. |
| `SERPER_API_KEY` | [serper.dev](https://serper.dev) → dashboard | No card or Google Cloud account needed. You can use `GOOGLE_PLACES_API_KEY` (Places API (New)) instead. |

```bash
cp .env.example .env    # fill in the keys
python -m venv venv
venv/Scripts/pip install -r requirements.txt     # Windows
# source venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
python -m odify_bot
```

The first time you message the bot, it replies with your Telegram user ID. Set that as `ALLOWED_USER_IDS` and restart. Nobody else can use the bot.

Without `OPENROUTER_API_KEY` the bot still works, but it only understands `<niche> in <location>` messages and searches that exact phrase.

## Usage

| You say | It does |
|---|---|
| `barbers in poland` | A country-wide search across its biggest cities (default 60 leads) |
| `barbers in Kraków` / `40 dentists in Austin` | A city search (default 20; max 150) |
| “…skip the ones with only instagram” | Leaves out social-only businesses |
| “…include ones I already have” | Doesn't skip leads from earlier searches |
| `show my won leads` / `/pipeline` | Your leads by status |
| `my recent searches` / `/leads` | Re-open a search, export it |
| `export everything` / `/export` | Every lead, with status and notes, as CSV |
| `change my pitch to: Hi {name}, …` / `/template` | Your WhatsApp/email pitch (`{name}`, `{category}`, `{rating}`, `{reviews}`), translated per country automatically |

## Hosting on Railway

1. Deploy this repo. It uses the Dockerfile.
2. Set the variables from `.env.example`.
3. **Add a volume mounted at `/data`.** Without it, your leads are wiped on every deploy.
4. Keep it at 1 replica. It needs no domain or port, since it uses long polling.

For any other Docker host: `docker run -d --env-file .env -v odify-data:/data --restart unless-stopped $(docker build -q .)`

## Costs

- **Search:** a city makes up to 3 requests, and a country about 1 request per city (up to 8 cities). Serper charges credits per request, and Google bills Places Text Search, which has a monthly free allowance. With Google, if your key only has the *old* Places API, the bot falls back to it automatically, and that costs much more (one Details call per business). Set `PLACES_API_MODE=new` to forbid the fallback.
- **LLM:** one small DeepSeek call to understand each message, plus one to translate the pitch for non-English countries. That's a tiny fraction of a cent per search.

## Development

```bash
venv/Scripts/python -m pytest tests -q
```

- `odify_bot/llm.py`: DeepSeek/OpenRouter message → action plan (strict JSON schema), pitch translation
- `odify_bot/places.py`: Serper/Google search, multi-city fan-out, website/social classification, phone normalisation, lead scoring
- `odify_bot/emails.py`: email discovery
- `odify_bot/exports.py`: CSV and vCard
- `odify_bot/store.py`: SQLite
- `odify_bot/bot.py`: Telegram handlers and routing
