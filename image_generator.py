#!/usr/bin/env python3
"""
Generates a styled highlight card image for the Daily Signals newsletter.
Uses Pillow. Falls back gracefully if Pillow or fonts are unavailable.
"""
from __future__ import annotations

import io
import logging
import pathlib
import textwrap

logger = logging.getLogger(__name__)

# Colour palette
_BG       = (10, 10, 22)
_ACCENT   = (0, 229, 255)       # electric cyan
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


def _find_font(bold: bool = False) -> str | None:
    """Return path to a usable TTF font, or None if none found."""
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


def generate_highlight_card(
    title_de: str,
    intro: str,
    source: str,
    study_phase: str = "",
    date_str: str = "",
) -> bytes | None:
    """
    Generate a 1200×630 PNG highlight card.
    Returns PNG bytes, or None if Pillow is unavailable or generation fails.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("Pillow not installed — skipping highlight image")
        return None

    try:
        W, H = 1200, 630
        PAD  = 64

        img  = Image.new("RGB", (W, H), color=_BG)
        draw = ImageDraw.Draw(img)

        font_bold_path = _find_font(bold=True)
        font_reg_path  = _find_font(bold=False)

        def _font(size: int, bold: bool = False):
            path = font_bold_path if bold else font_reg_path
            if path:
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass
            return ImageFont.load_default()

        f_brand  = _font(26, bold=True)
        f_label  = _font(22)
        f_title  = _font(54, bold=True)
        f_body   = _font(28)
        f_badge  = _font(21, bold=True)

        # ── Top accent bar ──────────────────────────────────────────────────
        draw.rectangle([(0, 0), (W, 5)], fill=_ACCENT)

        # ── Branding left ───────────────────────────────────────────────────
        draw.text((PAD, 26), "DAILY SIGNALS", font=f_brand, fill=_ACCENT)

        # ── Date right ──────────────────────────────────────────────────────
        if date_str:
            bbox   = draw.textbbox((0, 0), date_str, font=f_label)
            text_w = bbox[2] - bbox[0]
            draw.text((W - PAD - text_w, 30), date_str, font=f_label, fill=_DIM)

        # ── Divider ─────────────────────────────────────────────────────────
        draw.rectangle([(PAD, 76), (W - PAD, 78)], fill=(28, 28, 50))

        # ── Subtle vertical grid lines (texture) ────────────────────────────
        for x in range(0, W, 80):
            draw.line([(x, 82), (x, H - 8)], fill=_GRID, width=1)

        y = 104

        # ── Study phase badge ───────────────────────────────────────────────
        if study_phase:
            # Strip leading emoji so plain TTF fonts render the badge cleanly
            import re as _re
            badge_text = _re.sub(r"^[\U00002000-\U0010ffff]+\s*", "", study_phase).strip()
            if not badge_text:
                badge_text = study_phase
            bbox = draw.textbbox((0, 0), badge_text, font=f_badge)
            bw   = bbox[2] - bbox[0] + 28
            bh   = bbox[3] - bbox[1] + 14
            draw.rounded_rectangle([(PAD, y), (PAD + bw, y + bh)], radius=8, fill=_BADGE_BG)
            draw.text((PAD + 14, y + 7), badge_text, font=f_badge, fill=_ACCENT)
            y += bh + 22

        # ── Title (word-wrapped, max 3 lines) ───────────────────────────────
        wrapped = textwrap.fill(title_de, width=36)
        for line in wrapped.splitlines()[:3]:
            draw.text((PAD, y), line, font=f_title, fill=_TITLE)
            bbox = draw.textbbox((PAD, y), line, font=f_title)
            y += bbox[3] - bbox[1] + 10
        y += 18

        # ── Intro snippet (first ~150 chars, 2 lines max) ───────────────────
        intro_snip = intro[:150].rstrip()
        if len(intro) > 150:
            # break at last space before cutoff
            cut = intro[:150].rfind(" ")
            intro_snip = (intro[:cut] if cut > 80 else intro[:150]) + " …"
        for line in textwrap.fill(intro_snip, width=68).splitlines()[:2]:
            draw.text((PAD, y), line, font=f_body, fill=_BODY)
            bbox = draw.textbbox((PAD, y), line, font=f_body)
            y += bbox[3] - bbox[1] + 8

        # ── Bottom bar: source label ─────────────────────────────────────────
        draw.rectangle([(0, H - 5), (W, H)], fill=_ACCENT)
        src_label = _SOURCE_LABELS.get(source, source)
        draw.text((PAD, H - 42), f"Quelle: {src_label}", font=f_label, fill=_DIM)

        # ── Right bottom watermark ───────────────────────────────────────────
        wm = "daily-signals.bot"
        bbox   = draw.textbbox((0, 0), wm, font=f_label)
        wm_w   = bbox[2] - bbox[0]
        draw.text((W - PAD - wm_w, H - 42), wm, font=f_label, fill=_DIM)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        logger.info("Highlight card generated (%d bytes)", buf.tell())
        return buf.getvalue()

    except Exception as exc:
        logger.error("Image generation failed: %s", exc)
        return None
