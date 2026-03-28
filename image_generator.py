#!/usr/bin/env python3
"""
Highlight image for the Daily Signals newsletter.

Primary:  DALL-E 3 — AI-generated illustration of the paper topic
          (requires OPENAI_API_KEY env var)
Fallback: Pillow text card  — used when no OpenAI key is available
          or when DALL-E fails
"""
from __future__ import annotations

import io
import logging
import os
import pathlib
import textwrap

logger = logging.getLogger(__name__)

# ── Pillow card palette ────────────────────────────────────────────────────────
_BG       = (10, 10, 22)
_ACCENT   = (0, 229, 255)
_TITLE    = (255, 255, 255)
_BODY     = (176, 190, 197)
_DIM      = (90, 108, 125)
_BADGE_BG = (18, 28, 90)
_GRID     = (18, 18, 36)

_SOURCE_LABELS = {
    "biorxiv": "bioRxiv",
    "medrxiv": "medRxiv",
    "arxiv":   "arXiv",
    "journal": "Journal",
}

# ── Paper og:image ────────────────────────────────────────────────────────────

def _fetch_paper_image(paper_url: str) -> bytes | None:
    """
    Try to get an og:image from journal paper pages (Nature, Science, NEJM, etc.).
    bioRxiv and medRxiv block all scraping — skipped here, Wikipedia handles those.
    """
    if not paper_url:
        return None
    # Preprint servers block all bots — skip immediately
    if "biorxiv.org" in paper_url or "medrxiv.org" in paper_url or "arxiv.org" in paper_url:
        return None
    try:
        import httpx
        from html.parser import HTMLParser

        class _OGParser(HTMLParser):
            og_image: str | None = None
            def handle_starttag(self, tag, attrs):
                if tag == "meta":
                    d = dict(attrs)
                    content = d.get("content", "")
                    prop = d.get("property", "") or d.get("name", "")
                    if prop in ("og:image", "twitter:image", "twitter:image:src") and content:
                        if not self.og_image:   # first match wins
                            self.og_image = content

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }
        with httpx.Client(timeout=15, headers=headers, follow_redirects=True) as client:
            resp = client.get(paper_url)
            resp.raise_for_status()
            parser = _OGParser()
            parser.feed(resp.text[:30_000])
            if not parser.og_image:
                return None
            img_resp = client.get(parser.og_image)
            img_resp.raise_for_status()
            ct = img_resp.headers.get("content-type", "")
            if not ct.startswith("image/") or len(img_resp.content) < 8_000:
                return None
            logger.info("Journal og:image: %d bytes from %s", len(img_resp.content), paper_url)
            return img_resp.content
    except Exception as exc:
        logger.debug("Paper image fetch failed (%s): %s", type(exc).__name__, exc)
        return None


# ── Wikipedia image ────────────────────────────────────────────────────────────

def _fetch_wikipedia_image(title_en: str) -> bytes | None:
    """Search Wikipedia for the topic and return the best thumbnail."""
    if not title_en:
        return None
    try:
        import httpx
        keywords = " ".join(title_en.split()[:6])
        wiki_headers = {
            "User-Agent": "DailySignalsBot/1.0 (https://github.com/Vogtei/daily-signals; bot)",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=12, headers=wiki_headers) as client:
            search = client.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search",
                        "srsearch": keywords, "format": "json", "srlimit": 5},
            )
            search.raise_for_status()
            results = search.json().get("query", {}).get("search", [])
            if not results:
                return None
            for hit in results:
                slug = hit["title"].replace(" ", "_")
                try:
                    s = client.get(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}",
                        timeout=10,
                    )
                    s.raise_for_status()
                    data = s.json()
                    thumb = data.get("originalimage") or data.get("thumbnail")
                    if not thumb:
                        continue
                    if thumb.get("width", 0) < 300:
                        continue
                    ir = client.get(thumb["source"], timeout=20, follow_redirects=True)
                    ir.raise_for_status()
                    if len(ir.content) < 15_000:
                        continue
                    logger.info("Wikipedia image '%s': %d bytes", hit["title"], len(ir.content))
                    return ir.content
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("Wikipedia image fetch failed (%s): %s", type(exc).__name__, exc)
    return None


# ── DALL-E ─────────────────────────────────────────────────────────────────────

def _build_visual_prompt(title_en: str, abstract: str) -> str:
    """
    Use Claude Haiku to write a rich cinematic DALL-E scene prompt from the paper.
    Style mirrors high-end science visualisation: specific scene + holographic/bioluminescent
    aesthetic + quality boosters.
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=220,
            messages=[{"role": "user", "content": (
                "Write a DALL-E image prompt in English for this science paper. "
                "Follow this exact structure (comma-separated phrases, no full sentences):\n"
                "1. Main action/scene specific to the paper topic (e.g. 'AI neural network analyzing glowing cancer cell clusters')\n"
                "2. Key visual elements related to the science (e.g. 'holographic DNA strands, bioluminescent protein structures, robotic surgical arm')\n"
                "3. Environment (e.g. 'futuristic dark-blue hospital lab, high-tech clinical setting')\n"
                "4. Concept keywords from the paper (2-3 specific terms)\n\n"
                "Rules: ultra-specific to THIS paper's topic, no generic 'scientist in lab', "
                "no human faces, no text/labels in image, vivid and visual.\n\n"
                f"Title: {title_en}\nAbstract: {abstract[:500]}"
            )}],
        )
        # Strip any markdown headers Haiku might prepend
        import re as _re
        scene = _re.sub(r"^#+\s.*\n?", "", resp.content[0].text.strip()).strip()
        logger.debug("DALL-E scene: %s", scene[:120])
    except Exception as exc:
        logger.warning("Prompt generation failed (%s), using fallback", exc)
        scene = f"Futuristic scientific visualization of {title_en}, glowing molecular structures, bioluminescent data"

    return (
        f"{scene}, "
        "cinematic lighting, electric blue and cyan glow, deep navy background, "
        "ultra-detailed, photorealistic, 8k, depth of field, science journal cover quality, "
        "no text, no labels, no watermarks"
    )


def _generate_dalle_image(title_en: str, abstract: str) -> bytes | None:
    """Generate image via DALL-E 3. Returns JPEG bytes or None."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.info("OPENAI_API_KEY not set — falling back to Pillow card")
        return None

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai package not installed — falling back to Pillow card")
        return None

    try:
        import httpx

        prompt = _build_visual_prompt(title_en, abstract)
        client = OpenAI(api_key=api_key)
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1792x1024",
            quality="standard",
            n=1,
        )
        image_url = response.data[0].url
        img_resp = httpx.get(image_url, timeout=60, follow_redirects=True)
        img_resp.raise_for_status()
        logger.info("DALL-E 3 image downloaded (%d bytes)", len(img_resp.content))
        return img_resp.content

    except Exception as exc:
        logger.error("DALL-E 3 generation failed (%s): %s", type(exc).__name__, exc)
        return None


# ── Pillow card (fallback) ─────────────────────────────────────────────────────

def _find_font(bold: bool = False) -> str | None:
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
    for path in candidates:
        if pathlib.Path(path).exists():
            return path
    return None


def _generate_pillow_card(
    title_de: str,
    intro: str,
    source: str,
    study_phase: str,
    date_str: str,
) -> bytes | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("Pillow not installed — no highlight image")
        return None

    import re as _re

    try:
        W, H = 1200, 630
        PAD  = 64
        img  = Image.new("RGB", (W, H), color=_BG)
        draw = ImageDraw.Draw(img)

        fbp = _find_font(bold=True)
        frp = _find_font(bold=False)

        def _f(size: int, bold: bool = False):
            path = fbp if bold else frp
            if path:
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass
            return ImageFont.load_default()

        f_brand = _f(26, bold=True)
        f_label = _f(22)
        f_title = _f(54, bold=True)
        f_body  = _f(28)
        f_badge = _f(21, bold=True)

        draw.rectangle([(0, 0), (W, 5)], fill=_ACCENT)
        draw.text((PAD, 26), "DAILY SIGNALS", font=f_brand, fill=_ACCENT)

        if date_str:
            bb = draw.textbbox((0, 0), date_str, font=f_label)
            draw.text((W - PAD - (bb[2] - bb[0]), 30), date_str, font=f_label, fill=_DIM)

        draw.rectangle([(PAD, 76), (W - PAD, 78)], fill=(28, 28, 50))
        for x in range(0, W, 80):
            draw.line([(x, 82), (x, H - 8)], fill=_GRID, width=1)

        y = 104
        if study_phase:
            badge_text = _re.sub(r"^[\U00002000-\U0010ffff]+\s*", "", study_phase).strip() or study_phase
            bb  = draw.textbbox((0, 0), badge_text, font=f_badge)
            bw  = bb[2] - bb[0] + 28
            bh  = bb[3] - bb[1] + 14
            draw.rounded_rectangle([(PAD, y), (PAD + bw, y + bh)], radius=8, fill=_BADGE_BG)
            draw.text((PAD + 14, y + 7), badge_text, font=f_badge, fill=_ACCENT)
            y += bh + 22

        for line in textwrap.fill(title_de, width=36).splitlines()[:3]:
            draw.text((PAD, y), line, font=f_title, fill=_TITLE)
            bb = draw.textbbox((PAD, y), line, font=f_title)
            y += bb[3] - bb[1] + 10
        y += 18

        intro_snip = intro[:150].rstrip()
        if len(intro) > 150:
            cut = intro[:150].rfind(" ")
            intro_snip = (intro[:cut] if cut > 80 else intro[:150]) + " …"
        for line in textwrap.fill(intro_snip, width=68).splitlines()[:2]:
            draw.text((PAD, y), line, font=f_body, fill=_BODY)
            bb = draw.textbbox((PAD, y), line, font=f_body)
            y += bb[3] - bb[1] + 8

        draw.rectangle([(0, H - 5), (W, H)], fill=_ACCENT)
        src_label = _SOURCE_LABELS.get(source, source)
        draw.text((PAD, H - 42), f"Quelle: {src_label}", font=f_label, fill=_DIM)
        wm = "daily-signals.bot"
        bb = draw.textbbox((0, 0), wm, font=f_label)
        draw.text((W - PAD - (bb[2] - bb[0]), H - 42), wm, font=f_label, fill=_DIM)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        logger.info("Pillow card generated (%d bytes)", buf.tell())
        return buf.getvalue()

    except Exception as exc:
        logger.error("Pillow card generation failed: %s", exc)
        return None


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_highlight_card(
    title_de: str,
    intro: str,
    source: str,
    study_phase: str = "",
    date_str: str = "",
    title_en: str = "",
    abstract: str = "",
    paper_url: str = "",
) -> bytes | None:
    """
    Return image bytes for the newsletter highlight.

    Priority:
      1. og:image scraped from the paper page (bioRxiv / medRxiv / journal)
      2. Wikipedia thumbnail for the topic
      3. DALL-E 3 AI-generated image (needs OPENAI_API_KEY)
      4. Pillow text card (always works)
    """
    img = _fetch_paper_image(paper_url)
    if img:
        logger.info("Using paper og:image")
        return img

    img = _fetch_wikipedia_image(title_en or title_de)
    if img:
        logger.info("Using Wikipedia image")
        return img

    img = _generate_dalle_image(title_en or title_de, abstract)
    if img:
        return img

    logger.info("Using Pillow fallback card")
    return _generate_pillow_card(title_de, intro, source, study_phase, date_str)
