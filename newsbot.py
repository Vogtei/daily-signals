#!/usr/bin/env python3
"""
Medical & Biotech Daily Digest Bot
Sources: bioRxiv, medRxiv, arXiv, Journal RSS, Newsletters
"""
from __future__ import annotations

import asyncio
import calendar
import datetime
import json
import logging
import os
import pathlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import anthropic
import feedparser
import httpx
from dotenv import load_dotenv
from telegram import Bot

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LAST_RUN_FILE = pathlib.Path(__file__).parent / "last_run.json"
SEEN_ARTICLES_FILE = pathlib.Path(__file__).parent / "seen_articles.json"

BIORXIV_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "drug discovery", "protein", "genomics", "immunotherapy",
    "crispr", "synthetic biology", "bioinformatics", "biotech",
]

MEDRXIV_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "diagnosis", "clinical trial", "cancer", "precision medicine",
    "imaging", "immunotherapy", "drug therapy", "diagnostic",
]

ARXIV_QUERIES = [
    "https://export.arxiv.org/api/query?search_query=all:machine+learning+drug+discovery&max_results=5&sortBy=submittedDate&sortOrder=descending",
    "https://export.arxiv.org/api/query?search_query=all:protein+structure+prediction&max_results=5&sortBy=submittedDate&sortOrder=descending",
    "https://export.arxiv.org/api/query?search_query=all:genomic+analysis+deep+learning&max_results=5&sortBy=submittedDate&sortOrder=descending",
    "https://export.arxiv.org/api/query?search_query=all:medical+imaging+deep+learning&max_results=5&sortBy=submittedDate&sortOrder=descending",
    "https://export.arxiv.org/api/query?search_query=all:computational+biology&max_results=5&sortBy=submittedDate&sortOrder=descending",
    "https://export.arxiv.org/api/query?search_query=all:molecular+dynamics+AI&max_results=5&sortBy=submittedDate&sortOrder=descending",
    "https://export.arxiv.org/api/query?search_query=all:bioinformatics+machine+learning&max_results=5&sortBy=submittedDate&sortOrder=descending",
]

JOURNAL_FEEDS = [
    "https://www.nature.com/nbt.rss",
    "https://www.thelancet.com/action/showFeed?jc=lancet&type=etoc&feed=rss",
    "https://www.cell.com/cell/rss",
    "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=stm",
    "https://jamanetwork.com/rss/site_3/67.xml",
    "https://importai.substack.com/feed",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    url: str
    published: datetime.datetime
    source: str  # "arxiv" | "biorxiv" | "medrxiv" | "journal"


# ---------------------------------------------------------------------------
# Timestamp tracking (24h fetch window)
# ---------------------------------------------------------------------------

def load_last_run() -> datetime.datetime:
    """Return last-run timestamp. Defaults to 24h ago on first run."""
    if LAST_RUN_FILE.exists():
        try:
            data = json.loads(LAST_RUN_FILE.read_text())
            return datetime.datetime.fromisoformat(data["last_run"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Could not parse last_run.json (%s), defaulting to 24h ago", exc)
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)


def save_last_run() -> None:
    """Persist current UTC time to last_run.json."""
    LAST_RUN_FILE.write_text(
        json.dumps({"last_run": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    )


# ---------------------------------------------------------------------------
# 7-day seen articles deduplication
# ---------------------------------------------------------------------------

def load_seen_articles() -> dict[str, str]:
    """Load {url: iso_timestamp} of articles sent in the last 7 days."""
    if not SEEN_ARTICLES_FILE.exists():
        return {}
    try:
        return json.loads(SEEN_ARTICLES_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def save_seen_articles(seen: dict[str, str], new_urls: list[str]) -> None:
    """Add new URLs and purge entries older than 7 days."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=7)
    for url in new_urls:
        seen[url] = now.isoformat()
    seen = {
        url: ts for url, ts in seen.items()
        if datetime.datetime.fromisoformat(ts) > cutoff
    }
    SEEN_ARTICLES_FILE.write_text(json.dumps(seen, indent=2))


# ---------------------------------------------------------------------------
# Fetching — Journal & Newsletter RSS
# ---------------------------------------------------------------------------

def fetch_journals(since: datetime.datetime) -> list[Article]:
    """Parse journal and newsletter RSS feeds."""
    articles = []
    for feed_url in JOURNAL_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                pub = _parse_feedparser_date(entry)
                if pub is None or pub <= since:
                    continue
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()
                if not title or not url:
                    continue
                articles.append(Article(title=title, url=url, published=pub, source="journal"))
        except Exception as exc:
            logger.warning("Failed to fetch journal feed %s: %s", feed_url, exc)
    logger.info("Journals/Newsletters: %d articles", len(articles))
    return articles


def _parse_feedparser_date(entry) -> datetime.datetime | None:
    """Convert feedparser's published_parsed (struct_time, UTC) to aware datetime."""
    parsed = entry.get("published_parsed")
    if parsed is None:
        return None
    try:
        ts = calendar.timegm(parsed)
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fetching — bioRxiv & medRxiv via official API
# ---------------------------------------------------------------------------

def _fetch_rxiv(server: str, keywords: list[str], since: datetime.datetime) -> list[Article]:
    """
    Fetch preprints from bioRxiv/medRxiv JSON API.
    Always looks back at least 3 days to ensure results.
    """
    lookback = max(since, datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3))
    start = lookback.strftime("%Y-%m-%d")
    end = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    url = f"https://api.biorxiv.org/details/{server}/{start}/{end}/0"

    articles = []
    seen: set[str] = set()

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("%s API fetch failed: %s", server, exc)
        return []

    for paper in data.get("collection", []):
        title = paper.get("title", "").strip()
        doi = paper.get("doi", "").strip()
        date_str = paper.get("date", "")
        category = paper.get("category", "").lower()
        abstract = paper.get("abstract", "").lower()

        if not title or not doi:
            continue

        text = f"{title.lower()} {category} {abstract[:300]}"
        if not any(kw in text for kw in keywords):
            continue

        paper_url = f"https://www.{server}.org/content/{doi}"
        if paper_url in seen:
            continue
        seen.add(paper_url)

        try:
            pub = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            pub = datetime.datetime.now(datetime.timezone.utc)

        articles.append(Article(title=title, url=paper_url, published=pub, source=server))

    return articles


def fetch_biorxiv(since: datetime.datetime) -> list[Article]:
    articles = _fetch_rxiv("biorxiv", BIORXIV_KEYWORDS, since)
    logger.info("bioRxiv: %d papers", len(articles))
    return articles


def fetch_medrxiv(since: datetime.datetime) -> list[Article]:
    articles = _fetch_rxiv("medrxiv", MEDRXIV_KEYWORDS, since)
    logger.info("medRxiv: %d papers", len(articles))
    return articles


# ---------------------------------------------------------------------------
# Fetching — ArXiv
# ---------------------------------------------------------------------------

def fetch_arxiv(since: datetime.datetime) -> list[Article]:
    """Fetch all ArXiv queries and return recent papers."""
    articles = []
    with httpx.Client(timeout=15) as client:
        for query_url in ARXIV_QUERIES:
            try:
                resp = client.get(query_url)
                resp.raise_for_status()
                new = _parse_arxiv_xml(resp.text, since)
                logger.info("ArXiv query %s: %d results", query_url.split("search_query=")[1][:40], len(new))
                articles.extend(new)
            except httpx.HTTPError as exc:
                logger.warning("ArXiv fetch failed for %s: %s", query_url, exc)
    return articles


def _parse_arxiv_xml(xml_text: str, since: datetime.datetime) -> list[Article]:
    """Parse ArXiv Atom 1.0 XML."""
    NS = {"atom": "http://www.w3.org/2005/Atom"}
    articles = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("ArXiv XML parse error: %s", exc)
        return []

    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        id_el = entry.find("atom:id", NS)
        pub_el = entry.find("atom:published", NS)

        if title_el is None or id_el is None or pub_el is None:
            continue

        title = " ".join(title_el.text.split())
        url = id_el.text.strip()

        try:
            pub = datetime.datetime.fromisoformat(pub_el.text.strip().replace("Z", "+00:00"))
        except ValueError:
            continue

        if pub > since:
            articles.append(Article(title=title, url=url, published=pub, source="arxiv"))

    return articles


# ---------------------------------------------------------------------------
# Aggregation & deduplication
# ---------------------------------------------------------------------------

def aggregate_articles(
    arxiv: list[Article],
    biorxiv: list[Article],
    medrxiv: list[Article],
    journals: list[Article],
    seen_articles: dict[str, str],
    max_articles: int = 60,
) -> list[Article]:
    """Merge, deduplicate by URL, skip 7-day seen articles, cap at max_articles."""
    seen_urls: set[str] = set()
    deduped: list[Article] = []

    all_articles = (
        sorted(biorxiv, key=lambda a: a.published, reverse=True)
        + sorted(medrxiv, key=lambda a: a.published, reverse=True)
        + sorted(arxiv, key=lambda a: a.published, reverse=True)
        + sorted(journals, key=lambda a: a.published, reverse=True)
    )

    for article in all_articles:
        normalized = article.url.rstrip("/").lower()
        if normalized in seen_urls:
            continue
        if article.url in seen_articles:
            continue
        seen_urls.add(normalized)
        deduped.append(article)
        if len(deduped) >= max_articles:
            break

    return deduped


# ---------------------------------------------------------------------------
# Claude summarization
# ---------------------------------------------------------------------------

def summarize_with_claude(articles: list[Article]) -> str:
    """Send article list to Claude and return German digest. Empty string on failure."""
    if not articles:
        return ""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    article_lines = []
    for i, a in enumerate(articles, 1):
        tag_map = {"arxiv": "[arXiv]", "biorxiv": "[bioRxiv]", "medrxiv": "[medRxiv]", "journal": "[Journal]"}
        tag = tag_map.get(a.source, "[News]")
        article_lines.append(f"{i}. {tag} {a.title}\n   URL: {a.url}")
    articles_block = "\n".join(article_lines)

    system_prompt = (
        "Du bist ein Medical & Biotech Digest Kurator für Leon. Schreibe ausschließlich auf Deutsch.\n"
        "Übersetze alle englischen Titel ins Deutsche. Verwende KEIN ## oder --- Markdown. Nur *fett* erlaubt.\n\n"

        "ABSCHNITT 1 — MIND-BLOWING FACT\n"
        "Format:\n"
        "🤯 *MIND-BLOWING FACT DES TAGES*\n"
        "[1 WOW-Satz aus den besten News, kurz & inspirierend, komplett ohne Fachbegriffe]\n"
        "[Quellname](URL)\n\n"
        "═══════════════════════════════════════\n\n"

        "ABSCHNITT 2 — HIGHLIGHTS DES TAGES\n"
        "Wähle 3-5 der wichtigsten Paper/News. Format pro Eintrag:\n"
        "🏆 *[Übersetzter Titel]* [Schwierigkeits-Emoji]\n"
        "[1 Satz: Was wurde erreicht?]\n\n"
        "🔹 *Für Anfänger erklärt:*\n"
        "[2-3 Sätze komplett ohne Fachbegriffe, wie für einen 15-Jährigen]\n\n"
        "🔹 *Key Concepts:*\n"
        "- *[Begriff 1]:* [1 Satz einfach erklärt]\n"
        "- *[Begriff 2]:* [1 Satz einfach erklärt]\n\n"
        "🔹 *Warum relevant für dich:*\n"
        "[Praktische Anwendung im echten Leben, 1-2 Sätze]\n\n"
        "[Quellname](URL)\n\n"
        "---\n\n"
        "Schwierigkeits-Emojis: 🟢 Anfänger | 🟡 Fortgeschritten | 🔴 Expert\n\n"
        "═══════════════════════════════════════\n\n"

        "ABSCHNITT 3 — AUCH INTERESSANT\n"
        "Format:\n"
        "💡 *AUCH INTERESSANT (Worth Watching)*\n\n"
        "*🧬 Medizin & Biotech Papers*\n"
        "1️⃣ [Titel] – [Quellname](URL)\n"
        "2️⃣ [Titel] – [Quellname](URL)\n\n"
        "*📊 AI Research (Computational Bio)*\n"
        "1️⃣ [Titel] – [arXiv](URL)\n"
        "2️⃣ [Titel] – [arXiv](URL)\n\n"
        "Nummerierung pro Kategorie neu beginnen. Keine Bullet-Points (•).\n\n"
        "═══════════════════════════════════════\n\n"

        "ABSCHNITT 4 — HEUTE ZUM LERNEN\n"
        "Format:\n"
        "📚 *HEUTE ZUM LERNEN*\n"
        "[Was man lernt, 1 Satz] – [Kanal/Quelle](https://www.youtube.com/results?search_query=[topic+encoded])\n\n"

        "GLOBALREGELN:\n"
        "- Keine Dopplungen zwischen Abschnitten\n"
        "- [bioRxiv]/[medRxiv]/[Journal] → Medizin & Biotech; [arXiv] → AI Research\n"
        "- Gleiches Thema aus mehreren Quellen → 1 Eintrag mit: [Quelle1](URL1) | [Quelle2](URL2)\n"
        "- Linktext = Quellenname (arXiv, bioRxiv, medRxiv, nature.com, etc.)\n"
        "- NUR Kategorie-Labels und 🔹-Labels bold — News-Titel NICHT bold\n"
        "- Trennlinien ═══ exakt so wie vorgegeben, keine anderen Trennzeichen"
    )

    user_message = f"Hier sind die heutigen Artikel:\n\n{articles_block}"

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text
    except anthropic.APIError as exc:
        logger.error("Claude API error: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def _split_message(text: str, max_length: int = 4096) -> list[str]:
    """Split long messages at paragraph boundaries."""
    if len(text) <= max_length:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks


async def send_telegram_digest(digest_body: str, date_str: str) -> None:
    """Format and send the digest via Telegram Bot API."""
    header = (
        f"☀️ *Morgen Leon! – Daily Digest: Medical & Biotech ({date_str})*\n"
        f"_Gefiltert aus bioRxiv, medRxiv, arXiv & Nature Bio_\n\n"
        f"═══════════════════════════════════════\n\n"
    )
    footer = "\n\n═══════════════════════════════════════\n\n💬 Viel Spaß beim Lernen. Bis morgen, Leon! 🧬"
    full_message = header + digest_body + footer

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    chunks = _split_message(full_message)

    async with bot:
        for chunk in chunks:
            try:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=chunk,
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.warning("Markdown send failed (%s), retrying as plain text", exc)
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=chunk,
                )
    logger.info("Telegram message sent (%d chunk(s))", len(chunks))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Starting Medical & Biotech Digest bot")

    since = load_last_run()
    seen_articles = load_seen_articles()
    logger.info(
        "Fetching articles since %s | %d articles in 7-day seen-list",
        since.isoformat(), len(seen_articles)
    )

    arxiv_articles = fetch_arxiv(since)
    biorxiv_articles = fetch_biorxiv(since)
    medrxiv_articles = fetch_medrxiv(since)
    journal_articles = fetch_journals(since)

    all_articles = aggregate_articles(
        arxiv_articles, biorxiv_articles, medrxiv_articles, journal_articles, seen_articles
    )
    logger.info("Aggregated %d unique articles after deduplication", len(all_articles))

    if not all_articles:
        logger.info("No new articles found. Saving timestamp and exiting.")
        save_last_run()
        return

    digest = summarize_with_claude(all_articles)
    if not digest:
        logger.error("Claude summarization returned empty. Aborting without updating timestamp.")
        raise SystemExit(1)

    date_str = datetime.datetime.now().strftime("%d.%m.%Y")
    asyncio.run(send_telegram_digest(digest, date_str))

    save_seen_articles(seen_articles, [a.url for a in all_articles])
    save_last_run()
    logger.info("Done.")


if __name__ == "__main__":
    main()
