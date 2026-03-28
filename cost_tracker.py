#!/usr/bin/env python3
"""
Tracks per-run API costs and sends a monthly Telegram report.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import re

logger = logging.getLogger(__name__)

def _resolve_state_dir() -> pathlib.Path:
    candidate = pathlib.Path(os.environ.get("STATE_DIR", pathlib.Path(__file__).parent))
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except PermissionError:
        return pathlib.Path(__file__).parent

_STATE_DIR = _resolve_state_dir()
COSTS_FILE = _STATE_DIR / "costs.json"

# Prices per 1M tokens (USD)
CLAUDE_PRICES = {
    "claude-opus-4-6":            {"in": 15.0,  "out": 75.0},
    "claude-sonnet-4-6":          {"in":  3.0,  "out": 15.0},
    "claude-haiku-4-5-20251001":  {"in":  0.80, "out":  4.0},
}
EL_PRICE_PER_1K_CHARS = 0.30  # eleven_multilingual_v2 pay-as-you-go

# Fixed token estimates per run based on current model config
_CLAUDE_CALLS = [
    ("claude-opus-4-6",            500,  800),   # newsletter lead (Opus)
    ("claude-haiku-4-5-20251001",  500,  800),   # newsletter #2
    ("claude-haiku-4-5-20251001",  500,  800),   # newsletter #3
    ("claude-haiku-4-5-20251001",  500,  800),   # newsletter #4
    ("claude-haiku-4-5-20251001",  500,  800),   # newsletter #5
    ("claude-haiku-4-5-20251001",  300,  150),   # title translation batch 1
    ("claude-haiku-4-5-20251001",  300,  150),   # title translation batch 2
    ("claude-sonnet-4-6",          700, 2000),   # podcast script
]


def _claude_cost() -> float:
    total = 0.0
    for model, inp, out in _CLAUDE_CALLS:
        p = CLAUDE_PRICES[model]
        total += (inp / 1_000_000) * p["in"]
        total += (out / 1_000_000) * p["out"]
    return total


def _elevenlabs_cost(script: str) -> float:
    clean = re.sub(r"\[(INTRO-JINGLE|TRANSITION|OUTRO-JINGLE|PPP|PP|P)\]", "", script)
    chars = len(clean.strip())
    return (chars / 1000) * EL_PRICE_PER_1K_CHARS


def _load() -> dict:
    if not COSTS_FILE.exists():
        return {"last_report": None, "runs": []}
    try:
        return json.loads(COSTS_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {"last_report": None, "runs": []}


def _save(data: dict) -> None:
    COSTS_FILE.write_text(json.dumps(data, indent=2))


def record_run(script: str) -> None:
    """Record estimated costs for one run."""
    data = _load()
    now = datetime.datetime.now(datetime.timezone.utc)
    claude = _claude_cost()
    el = _elevenlabs_cost(script)
    data["runs"].append({
        "date": now.isoformat(),
        "claude_usd": round(claude, 4),
        "elevenlabs_usd": round(el, 4),
        "total_usd": round(claude + el, 4),
        "el_chars": len(re.sub(r"\[(INTRO-JINGLE|TRANSITION|OUTRO-JINGLE|PPP|PP|P)\]", "", script).strip()),
    })
    # Keep only last 90 days
    cutoff = now - datetime.timedelta(days=90)
    data["runs"] = [
        r for r in data["runs"]
        if datetime.datetime.fromisoformat(r["date"]) > cutoff
    ]
    _save(data)
    logger.info("Run cost recorded: Claude $%.4f + ElevenLabs $%.4f = $%.4f", claude, el, claude + el)


def should_send_monthly_report() -> bool:
    """True if we're in a new calendar month since last report AND previous month has runs."""
    data = _load()
    now = datetime.datetime.now(datetime.timezone.utc)

    last_str = data.get("last_report")
    if last_str:
        last = datetime.datetime.fromisoformat(last_str)
        if not (now.year, now.month) > (last.year, last.month):
            return False

    # Check if previous month has any recorded runs
    if now.month == 1:
        report_year, report_month = now.year - 1, 12
    else:
        report_year, report_month = now.year, now.month - 1

    return any(
        datetime.datetime.fromisoformat(r["date"]).year == report_year
        and datetime.datetime.fromisoformat(r["date"]).month == report_month
        for r in data.get("runs", [])
    )


def build_monthly_report() -> str:
    """Format the monthly cost report as a Telegram message."""
    data = _load()
    now = datetime.datetime.now(datetime.timezone.utc)

    # Collect runs from the previous calendar month
    if now.month == 1:
        report_year, report_month = now.year - 1, 12
    else:
        report_year, report_month = now.year, now.month - 1

    month_runs = [
        r for r in data.get("runs", [])
        if datetime.datetime.fromisoformat(r["date"]).year == report_year
        and datetime.datetime.fromisoformat(r["date"]).month == report_month
    ]

    month_names = {
        1: "Januar", 2: "Februar", 3: "März", 4: "April",
        5: "Mai", 6: "Juni", 7: "Juli", 8: "August",
        9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
    }
    month_name = month_names[report_month]

    if not month_runs:
        return (
            f"📊 *Daily Signals – Monatsbericht {month_name} {report_year}*\n\n"
            f"Keine Läufe aufgezeichnet."
        )

    total_claude = sum(r["claude_usd"] for r in month_runs)
    total_el = sum(r["elevenlabs_usd"] for r in month_runs)
    total = total_claude + total_el
    avg = total / len(month_runs)
    total_chars = sum(r.get("el_chars", 0) for r in month_runs)

    return (
        f"📊 *Daily Signals – Monatsbericht {month_name} {report_year}*\n\n"
        f"🗓 {len(month_runs)} Läufe\n\n"
        f"💰 *Kosten*\n"
        f"Claude API: ${total_claude:.2f}\n"
        f"ElevenLabs: ${total_el:.2f} ({total_chars:,} Zeichen)\n"
        f"━━━━━━━━━━━━━━\n"
        f"Gesamt: *${total:.2f}*\n\n"
        f"📈 Ø pro Lauf: ${avg:.2f}"
    )


def mark_report_sent() -> None:
    data = _load()
    data["last_report"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _save(data)
