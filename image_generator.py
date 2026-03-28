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

# ── DALL-E ─────────────────────────────────────────────────────────────────────

def _build_visual_prompt(title_en: str, abstract: str) -> str:
    """
    Use Claude Haiku to write a focused visual scene description,
    then wrap it in a consistent cinematic style directive for DALL-E.
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=180,
            messages=[{"role": "user", "content": (
                "Write a 2-sentence visual scene description in English for a DALL-E image "
                "that illustrates this science paper. Be concrete and visual: mention specific "
                "objects, organisms, structures or processes (e.g. glowing DNA strands, "
                "microscopic bacteria, brain scan cross-section, robotic surgery arm). "
                "No text, no people's faces, no abstract metaphors.\n\n"
                f"Title: {title_en}\nAbstract: {abstract[:400]}"
            )}],
        )
        scene = resp.content[0].text.strip()
        logger.debug("DALL-E visual prompt scene: %s", scene[:100])
    except Exception as exc:
        logger.warning("Prompt generation failed (%s), using title fallback", exc)
        scene = f"Scientific visualization of: {title_en}"

    return (
        f"{scene} "
        "Render as cinematic scientific photography: deep navy and black background, "
        "glowing bioluminescent cyan and electric-blue rim lighting, "
        "ultra-sharp macro detail, award-winning science journal cover composition. "
        "No text, no labels, no watermarks."
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
) -> bytes | None:
    """
    Return image bytes for the newsletter highlight.

    Tries DALL-E 3 first (needs OPENAI_API_KEY).
    Falls back to a Pillow text card if DALL-E is unavailable or fails.
    Returns None only if both paths fail.
    """
    img = _generate_dalle_image(title_en or title_de, abstract)
    if img:
        return img
    logger.info("Using Pillow fallback card")
    return _generate_pillow_card(title_de, intro, source, study_phase, date_str)
