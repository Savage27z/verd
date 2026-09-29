"""Best-effort email discovery for businesses that have no website."""
import os
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

JUNK_EMAIL_HINTS = (
    "noreply", "no-reply", "donotreply", "example.", ".invalid", "test@", "sentry", "lorem",
    "wixpress", "@domain.", "@email.", "yourname", "your@", "name@", "user@", "privacy@",
    "abuse@", "webmaster@", "postmaster@", "@sentry", "@godaddy", "@2x.",
)
ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")

# Public link-in-bio pages usually list contact emails and don't need a login to read.
LINK_PAGE_HOSTS = ("linktr.ee", "bio.link", "beacons.ai", "taplink.cc", "linkin.bio")

# Directory/aggregator sites list *many* businesses; an email scraped there is only
# trusted if it looks related to the business name.
DIRECTORY_HINTS = ("yelp.", "yellowpages", "vconnect", "businesslist", "finelib", "cybo.", "tripadvisor", "foursquare")


def extract_emails(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for e in EMAIL_REGEX.findall(text):
        seen.setdefault(e.strip(".").lower(), None)
    return list(seen)


def scrape_page(url: str, timeout: int = 8) -> list[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        mailtos = [a["href"][7:].split("?")[0] for a in soup.select('a[href^="mailto:"]')]
        return extract_emails(" ".join(mailtos) + " " + soup.get_text(separator=" ", strip=True))
    except Exception:
        return []


# Engines that answer from datacenter IPs (Railway etc.). ddgs' default "auto" also hits
# Google/Brave/Startpage/Mojeek, which block cloud IPs, plus Wikipedia-style engines that
# never have contact pages — ~10 wasted requests per lead.
DEFAULT_BACKENDS = "yahoo,bing"


def web_search(query: str, max_results: int = 10) -> list[str]:
    urls: list[str] = []
    try:
        from ddgs import DDGS
        with DDGS(timeout=8) as ddgs:
            backend = os.getenv("EMAIL_SEARCH_BACKENDS", DEFAULT_BACKENDS)
            for r in ddgs.text(query, max_results=max_results, backend=backend):
                urls.append(r["href"])
    except Exception:
        pass
    if urls:
        return urls
    try:
        resp = requests.get("https://html.duckduckgo.com/html/", params={"q": query}, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if href.startswith("http"):
                urls.append(href)
                if len(urls) >= max_results:
                    break
    except Exception:
        pass
    return urls


def is_plausible(email: str) -> bool:
    low = email.lower()
    if len(low) > 80 or low.endswith(ASSET_SUFFIXES):
        return False
    return not any(h in low for h in JUNK_EMAIL_HINTS)


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 4}


def pick_best_email(emails: list[str], name: str = "", strict: bool = False) -> str:
    """Prefer an address that shares a word with the business name; with `strict`
    (directory pages listing many businesses) only accept such a match."""
    plausible = [e for e in emails if is_plausible(e)]
    tokens = _name_tokens(name)
    for e in plausible:
        compact = re.sub(r"[^a-z0-9]", "", e)
        if any(t in compact for t in tokens):
            return e
    if strict:
        return ""
    return plausible[0] if plausible else ""


def find_business_email(name: str, location: str, max_pages: int = 2, website: str = "") -> str:
    """Try the business's public link page (if any), then DDG the name and scrape top hits."""
    if not name:
        return ""
    host = urlparse(website).netloc.lower().removeprefix("www.") if website else ""
    if host in LINK_PAGE_HOSTS:
        best = pick_best_email(scrape_page(website), name)
        if best:
            return best
    urls = web_search(f'"{name}" {location} email contact', max_results=max_pages)
    for url in urls:
        strict = any(h in url.lower() for h in DIRECTORY_HINTS)
        best = pick_best_email(scrape_page(url), name, strict=strict)
        if best:
            return best
    return ""
