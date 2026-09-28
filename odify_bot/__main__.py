"""Run the bot: python -m odify_bot"""
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from . import places, store  # noqa: E402
from .bot import allowed_ids, build_app  # noqa: E402


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        level=os.getenv("LOG_LEVEL", "INFO"))
    logging.getLogger("httpx").setLevel(logging.WARNING)  # PTB logs every poll otherwise
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        sys.exit("Missing TELEGRAM_BOT_TOKEN (see .env.example)")
    try:
        source = places.provider()
    except places.PlacesError as e:
        sys.exit(f"{e} (see .env.example)")
    logging.info("Search provider: %s", source)
    if not allowed_ids():
        logging.warning("ALLOWED_USER_IDS is empty — the bot will reply with your user ID so you can set it.")
    store.init_db()
    logging.info("Odify bot running (db: %s). Ctrl+C to stop.", store.db_path())
    build_app(token).run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
