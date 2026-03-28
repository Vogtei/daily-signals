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
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import anthropic
import feedparser
import httpx
from dotenv import load_dotenv
from telegram import Bot

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(override=True)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

_STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", pathlib.Path(__file__).parent))
_STATE_DIR.mkdir(parents=True, exist_ok=True)
LAST_RUN_FILE = _STATE_DIR / "last_run.json"
SEEN_ARTICLES_FILE = _STATE_DIR / "seen_articles.json"

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
    # Nature family
    "https://www.nature.com/nature.rss",
    "https://www.nature.com/nm.rss",           # Nature Medicine
    "https://www.nature.com/nbt.rss",           # Nature Biotechnology
    "https://www.nature.com/ng.rss",            # Nature Genetics
    "https://www.nature.com/ncomms.rss",        # Nature Communications
    # Science family
    "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
    "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=stm",  # Science Translational Medicine
    # Clinical top journals
    "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss",
    "https://www.thelancet.com/action/showFeed?jc=lancet&type=etoc&feed=rss",
    "https://jamanetwork.com/rss/site_3/67.xml",  # JAMA
    "https://www.bmj.com/rss/thebmj.xml",
    # Cell family
    "https://www.cell.com/cell/rss",
    "https://www.cell.com/cell-reports-medicine/rss",
    # Open access / preprint-adjacent
    "https://elifesciences.org/rss/recent.xml",
    "https://www.pnas.org/rss/current.xml",
    # AI/ML in medicine
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
    abstract: str = ""
    alt_sources: list = field(default_factory=list)  # [(source, url), ...]


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

def load_seen_articles() -> dict[str, dict]:
    """
    Load seen articles index. Format (v2):
      { url: {"ts": iso, "title": str}, ... }
    Also accepts legacy {url: iso_str} and upgrades on the fly.
    """
    if not SEEN_ARTICLES_FILE.exists():
        return {}
    try:
        raw = json.loads(SEEN_ARTICLES_FILE.read_text())
        upgraded = {}
        for url, val in raw.items():
            if isinstance(val, str):
                upgraded[url] = {"ts": val, "title": ""}
            else:
                upgraded[url] = val
        return upgraded
    except (json.JSONDecodeError, ValueError):
        return {}


def save_seen_articles(seen: dict[str, dict], new_articles: list) -> None:
    """Add new articles (url + title) and purge entries older than 7 days."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=7)
    for a in new_articles:
        seen[a.url] = {"ts": now.isoformat(), "title": a.title}
    seen = {
        url: val for url, val in seen.items()
        if datetime.datetime.fromisoformat(val["ts"]) > cutoff
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
                abstract = (getattr(entry, "summary", "") or "")[:2000]
                articles.append(Article(title=title, url=url, published=pub, source="journal", abstract=abstract))
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
        abstract_raw = paper.get("abstract", "")
        abstract_lower = abstract_raw.lower()

        if not title or not doi:
            continue

        text = f"{title.lower()} {category} {abstract_lower[:300]}"
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

        articles.append(Article(title=title, url=paper_url, published=pub, source=server, abstract=abstract_raw.strip()))

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
        summary_el = entry.find("atom:summary", NS)
        abstract = " ".join(summary_el.text.split()) if summary_el is not None and summary_el.text else ""

        try:
            pub = datetime.datetime.fromisoformat(pub_el.text.strip().replace("Z", "+00:00"))
        except ValueError:
            continue

        if pub > since:
            articles.append(Article(title=title, url=url, published=pub, source="arxiv", abstract=abstract))

    return articles


# ---------------------------------------------------------------------------
# Aggregation & deduplication
# ---------------------------------------------------------------------------

def _titles_similar(a: str, b: str, threshold: float = 0.80) -> bool:
    """Fuzzy title comparison — True if ≥ threshold similarity."""
    return SequenceMatcher(None, a.lower()[:120], b.lower()[:120]).ratio() >= threshold


def aggregate_articles(
    arxiv: list[Article],
    biorxiv: list[Article],
    medrxiv: list[Article],
    journals: list[Article],
    seen_articles: dict[str, dict],
    max_articles: int = 60,
) -> list[Article]:
    """Merge, semantic-deduplicate (within run + cross-run), skip seen, cap."""
    seen_urls: set[str] = set()
    deduped: list[Article] = []

    # Build list of previously seen titles for cross-run fuzzy check
    seen_titles: list[str] = [
        v["title"] for v in seen_articles.values() if v.get("title")
    ]

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

        # Cross-run semantic dedup: skip if similar title was sent before
        if any(_titles_similar(article.title, t) for t in seen_titles):
            logger.debug("Skipping (seen last week): %s", article.title[:60])
            continue

        # Within-run semantic dedup: merge into existing if title similar
        duplicate = next(
            (e for e in deduped if _titles_similar(article.title, e.title)),
            None,
        )
        if duplicate:
            duplicate.alt_sources.append((article.source, article.url))
            seen_urls.add(normalized)
            continue

        seen_urls.add(normalized)
        deduped.append(article)
        if len(deduped) >= max_articles:
            break

    return deduped


def select_diverse_highlights(articles: list[Article], n: int = 5) -> list[Article]:
    """Pick n highlights with source diversity: ~2 bio, ~2 med, ~1 arxiv."""
    with_abstract = [a for a in articles if a.abstract and len(a.abstract.strip()) >= 50]

    bio = [a for a in with_abstract if a.source in ("biorxiv", "journal")]
    med = [a for a in with_abstract if a.source == "medrxiv"]
    ai  = [a for a in with_abstract if a.source == "arxiv"]

    selected: list[Article] = []
    used: set[str] = set()

    def pick(pool: list[Article], count: int) -> None:
        for a in pool:
            if len(selected) - len(used) + len(selected) >= n:
                break
            if a.url not in used and len([s for s in selected if s.source == a.source or
                    (a.source in ("biorxiv","journal") and s.source in ("biorxiv","journal"))]) < count:
                selected.append(a)
                used.add(a.url)

    # Simpler quota approach
    selected.clear()
    used.clear()

    def _pick_up_to(pool: list[Article], count: int) -> list[Article]:
        picks = []
        for a in pool:
            if a.url not in used and len(picks) < count:
                picks.append(a)
                used.add(a.url)
        return picks

    selected += _pick_up_to(bio, 2)
    selected += _pick_up_to(med, 2)
    selected += _pick_up_to(ai, 1)

    # Fill remaining slots from any source
    if len(selected) < n:
        rest = [a for a in with_abstract if a.url not in used]
        selected += rest[:n - len(selected)]

    return selected[:n]


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

    from claude_analyzer import (
        generate_newsletter_content,
        format_newsletter, generate_podcast_script,
    )
    from podcast_generator import PodcastGenerator

    gen = PodcastGenerator()
    top_paper = gen.get_top_paper(all_articles)

    if not top_paper:
        logger.error("No suitable paper found. Aborting.")
        raise SystemExit(1)

    logger.info("Top paper: %s", top_paper.title[:80])
    date_str = datetime.datetime.now().strftime("%d.%m.%Y")

    # --- Text Newsletter (top 5 highlights, source-diverse) ---
    highlight_papers = select_diverse_highlights(all_articles, n=5)
    # Ensure top_paper is always first
    if highlight_papers and highlight_papers[0].url != top_paper.url:
        highlight_papers = [p for p in highlight_papers if p.url != top_paper.url]
        highlight_papers = [top_paper] + highlight_papers[:4]

    papers_data = []
    for i, paper in enumerate(highlight_papers):
        model = "claude-opus-4-6" if i == 0 else "claude-haiku-4-5-20251001"
        content = generate_newsletter_content(
            title=paper.title,
            abstract=paper.abstract,
            source=paper.source,
            model=model,
        )
        papers_data.append((content, paper.url, paper.source, paper.alt_sources))
        logger.info("Newsletter content generated for highlight %d: %s", i + 1, paper.title[:60])

    highlight_urls = {p.url for p in highlight_papers}
    side_articles = [a for a in all_articles if a.url not in highlight_urls]
    newsletter_text = format_newsletter(
        papers=papers_data,  # now 4-tuples: (content, url, source, alt_sources)
        date_str=date_str,
        side_articles=side_articles,
    )

    async def _send_newsletter():
        from telegram import Bot
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        chunks = _split_message(newsletter_text)
        async with bot:
            for chunk in chunks:
                try:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=chunk,
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=chunk)
        logger.info("Newsletter sent (%d chunk(s))", len(chunks))

    asyncio.run(_send_newsletter())

    # --- Podcast (non-fatal) ---
    script = ""
    try:
        script = generate_podcast_script(
            title=top_paper.title,
            abstract=top_paper.abstract,
            source=top_paper.source,
        )
        mp3_path = gen.run({}, top_paper, script)

        async def _send_podcast():
            from telegram import Bot
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            async with bot:
                lead_content = papers_data[0][0] if papers_data else {}
                await gen.send_to_telegram(
                    mp3_path, bot, TELEGRAM_CHAT_ID,
                    title_de=lead_content.get("title_simplified", top_paper.title),
                    source=top_paper.source,
                    paper_url=top_paper.url,
                )

        asyncio.run(_send_podcast())

    except Exception as exc:
        logger.error("Podcast generation failed (newsletter already sent): %s", exc)

    # --- Cost tracking + monthly report ---
    from cost_tracker import record_run, should_send_monthly_report, build_monthly_report, mark_report_sent
    record_run(script)

    if should_send_monthly_report():
        report_text = build_monthly_report()

        async def _send_report():
            from telegram import Bot
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            async with bot:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=report_text,
                    parse_mode="Markdown",
                )

        try:
            asyncio.run(_send_report())
            mark_report_sent()
            logger.info("Monthly cost report sent.")
        except Exception as exc:
            logger.error("Failed to send monthly report: %s", exc)

    save_seen_articles(seen_articles, all_articles)
    save_last_run()
    logger.info("Done.")


if __name__ == "__main__":
    main()
