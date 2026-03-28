#!/usr/bin/env python3
"""
Paper analysis + podcast script generation for Daily Signals.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import re

import anthropic

logger = logging.getLogger(__name__)


def analyze_paper_deep(title: str, abstract: str, source: str) -> dict:
    """
    Returns structured analysis for the newsletter (JSON fields).
    Kept separate from the podcast script so the newsletter stays independent.
    """
    if not abstract or len(abstract.strip()) < 50:
        return _fallback_analysis(title)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Analysiere diesen wissenschaftlichen Paper für einen deutschen Wissenschaftspodcast.

TITEL: {title}
ABSTRACT: {abstract}
QUELLE: {source}

Gib mir EXAKT diese JSON-Struktur (gesprochene Sprache, kein Aufsatzstil):

{{
  "context": "2-3 Sätze: Was war das Problem vorher? Alltagsanalogie. Kein Fachbegriff ohne Erklärung.",
  "research_question": "Die Kernfrage als echter Fragesatz, max. 1-2 Sätze.",
  "methodology": "Wie gingen sie vor? 2-3 Sätze vereinfacht.",
  "findings": "4-5 Sätze, konkret, mit Zahlen wenn vorhanden. Das Herzstück.",
  "why_breakthrough": "Warum ist das neu? 2 Sätze.",
  "key_concepts": [
    {{"concept": "Begriff", "explanation": "1 Satz Alltagserklärung"}}
  ],
  "implications": "Was ändert sich? 2 Sätze.",
  "limitations": "Was ist noch offen? 1-2 ehrliche Sätze.",
  "practical_applications": "Konkrete Szenarien, 2 Sätze.",
  "wow_factor": "1 Satz der hängen bleibt."
}}

Antwort NUR als valid JSON."""

    for attempt in range(2):
        try:
            response = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = _strip_fences(response.content[0].text)
            analysis = json.loads(text)
            logger.info("Analysis complete: %s", title[:60])
            return analysis
        except json.JSONDecodeError as exc:
            logger.warning("Analysis JSON parse failed (attempt %d/2): %s", attempt + 1, exc)
        except anthropic.APIError as exc:
            logger.error("API error: %s", exc)
            break
    logger.error("Analysis failed after retries: %s", title[:60])
    return _fallback_analysis(title)


def generate_podcast_script(title: str, abstract: str, source: str) -> str:
    """
    Have Claude write the full podcast script directly.

    Pause markers (inserted by Claude where natural in speech):
      [P]   = micro-pause / breath (~350ms) — after short phrases, for emphasis
      [PP]  = sentence pause (~800ms)       — end of thought, before continuing
      [PPP] = beat / dramatic pause (~1600ms) — major shift, let something land

    Structure markers (handled by podcast_generator):
      [INTRO-JINGLE]
      [TRANSITION]
      [OUTRO-JINGLE]
    """
    if not abstract or len(abstract.strip()) < 50:
        return _fallback_script(title, source)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Du schreibst das Skript für eine Podcast-Episode auf Deutsch. Nicht vorlesen – erzählen.

PAPER:
Titel: {title}
Abstract: {abstract}
Quelle: {source}

AUFGABE:
Schreib ein Podcast-Skript, das sich anfühlt wie ein gutes Gespräch – nicht wie ein Vortrag. Die Hörerin soll mit echtem neuen Wissen rausgehen und Spaß dabei gehabt haben.

STRUKTUR (halte dich genau daran):
[INTRO-JINGLE]
[TON:neugierig]
{{Einstieg: themenspezifischer Hook, der sofort ins Thema zieht. Wähle eine Einstiegsform die zum Inhalt passt – z.B. eine überraschende Zahl, eine konträre These, ein konkretes Szenario, eine rhetorische Frage, eine kurze Anekdote, oder eine provokante Behauptung. VERBOTEN: "Stell dir vor", "Willkommen", "Heute sprechen wir", "Hast du dich je gefragt". Der erste Satz muss einzigartig zu DIESEM Paper sein.}}
[PPP]
{{1-2 Sätze: Was werden wir heute entdecken? Macht Lust aufs Weiterhören.}}
[PPP]
[TRANSITION]
[TON:sachlich]
{{Kontext: Warum ist das überhaupt ein Problem? Kurze Alltagsanalogie. Max. 3-4 Sätze.}}
[PP]
[TON:neugierig]
{{Die Frage, die sich die Forscher stellten – neugierig formuliert}}
[PPP]
[TRANSITION]
[TON:sachlich]
{{Wie sie vorgegangen sind – ganz kurz, dann schnell weiter zu den Ergebnissen}}
[PP]
[TON:begeistert]
{{Was sie gefunden haben – das ist der Kern. Hier darf es länger sein. Konkret, mit Zahlen wenn vorhanden. Erzähl es wie eine Geschichte: erst die Erwartung, dann was wirklich passiert ist.}}
[PPP]
[TRANSITION]
[TON:warm]
{{Was das bedeutet – aber nicht abstrakt. Konkret: für wen, wann, wie.}}
[PP]
[TON:sachlich]
{{Was noch offen ist – ehrlich, kurz}}
[PP]
[TON:warm]
{{Abschluss: 1-2 Sätze die hängen bleiben. Persönlich, direkt an Leon.}}
[PPP]
[OUTRO-JINGLE]

SCHREIBREGELN:
- Setze [P], [PP], [PPP] an genau den Stellen, wo eine echte Sprecherin natürlich pausieren würde
- [P] nach kurzen Einwürfen ("Genau.", "Moment mal.", "Interessant, oder?")
- [PP] nach abgeschlossenen Gedanken, vor einem neuen Satz
- [PPP] nach großen Erkenntnissen, vor Themenwechsel, für dramatischen Effekt
- Du kannst [TON:dramatisch] für Schockmomente setzen wo es passt (z.B. überraschende Zahlen, Wendepunkte)
- Kein Einstieg mit "Stell dir vor", "Willkommen", "Heute", "Hast du dich je gefragt" — jeder Hook muss anders sein
- Kurze Sätze. Auch mal Fragmente. Auch mal ein einzelnes Wort.
- Kein Fachbegriff ohne sofortige Erklärung in Klammern oder danach
- Sprich Leon direkt an wo es passt
- Keine Bullet-Points, keine Nummerierungen
- Länge: so lang wie nötig, so kurz wie möglich. Kein Auffüllen.
- Gib NUR das fertige Skript zurück, kein Kommentar davor/danach"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        script = response.content[0].text.strip()
        logger.info("Podcast script generated (%d chars)", len(script))
        return script
    except anthropic.APIError as exc:
        logger.error("Script generation failed: %s", exc)
        return _fallback_script(title, source)


def generate_newsletter_content(title: str, abstract: str, source: str,
                                 model: str = "claude-opus-4-6") -> dict:
    """
    Generate newsletter-specific content for ONE paper.
    Shorter and more scannable than the podcast — different voice, different depth.
    """
    if not abstract or len(abstract.strip()) < 50:
        return _fallback_newsletter(title)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Du schreibst einen Telegram-Newsletter auf Deutsch für Leon – einen jungen Wissenschaftsbegeisterten.

Paper:
TITEL: {title}
ABSTRACT: {abstract}
QUELLE: {source}

Der Newsletter ist KURZ & PRÄGNANT (nicht der Podcast!). Zum schnellen Lesen, nicht zum tiefen Eintauchen.

Gib mir EXAKT dieses JSON:

{{
  "title_simplified": "Maximal 8 Wörter. Kein Fachbegriff. Was ist passiert, ganz simpel. Beispiel: 'Neuer KI-Test erkennt Antibiotikaresistenz in Minuten'",

  "intro": "Genau 2 Sätze. Warm, direkt, neugierig machend. Speziell zu DIESEM Paper. Nicht generisch.",

  "study_phase": "Einer von: '🔬 Zellstudie' | '🐭 Tierstudie' | '👥 Klinische Studie (Phase X)' | '💻 Computermodell' | '📊 Datenanalyse' – mit Kontext, z.B. '💻 Computermodell – 50.000 Genome'",

  "simple_explanation": "2-3 Sätze. Was haben die Forscher gemacht und was kam raus? Für einen 15-Jährigen ohne Biologiehintergrund. Eine gute Analogie wenn möglich.",

  "key_concepts": [
    {{"concept": "Begriff", "explanation": "1 Satz Laien-Erklärung mit Alltagsvergleich"}},
    {{"concept": "Begriff", "explanation": "..."}},
    {{"concept": "Begriff", "explanation": "..."}}
  ],

  "relevance": "2 Sätze. Konkret: für wen relevant, wann, wie. Kein akademisches 'könnte eventuell'.",

  "why_breakthrough": "2 Sätze. Was war bisher das Problem? Was löst das jetzt?",

  "fun_fact": "1 themenrelevanter Fun Fact. Überraschend, kurz, einprägsam."
}}

REGELN:
- Kurze Sätze
- Kein Fachbegriff ohne sofortige Erklärung
- Persönlich (du/dich/dir), nicht akademisch
- Antwort NUR als valid JSON"""

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            content = json.loads(_strip_fences(response.content[0].text))
            logger.info("Newsletter content generated for: %s", title[:60])
            return content
        except json.JSONDecodeError as exc:
            logger.warning("JSON parse failed (attempt %d/2): %s", attempt + 1, exc)
        except anthropic.APIError as exc:
            logger.error("API error: %s", exc)
            break
    logger.error("Newsletter content generation failed after retries: %s", title[:60])
    return _fallback_newsletter(title)


_NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

_SOURCE_DISPLAY = {
    "biorxiv": "bioRxiv",
    "medrxiv": "medRxiv",
    "arxiv": "arXiv",
    "journal": "Journal",
}


def _translate_titles_to_german(articles: list) -> list[str]:
    """Batch-translate English paper titles to short German summaries via Claude."""
    if not articles:
        return []
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    titles_block = "\n".join(f"{i+1}. {a.title}" for i, a in enumerate(articles))
    prompt = (
        f"Übersetze diese Wissenschaftstitel ins Deutsche. "
        f"Kurz, verständlich, max. 10 Wörter pro Titel. Keine Fachbegriffe ohne Erklärung. "
        f"Antworte NUR mit einer nummerierten Liste, genau {len(articles)} Einträge:\n\n"
        f"{titles_block}"
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        lines = [l.strip() for l in resp.content[0].text.strip().splitlines() if l.strip()]
        # Strip leading "1. ", "2. " etc.
        translated = []
        for line in lines:
            line = re.sub(r"^\d+[\.\)]\s*", "", line)
            translated.append(line)
        # Pad/trim to match input length
        while len(translated) < len(articles):
            translated.append(articles[len(translated)].title[:70])
        return translated[: len(articles)]
    except Exception as exc:
        logger.warning("Title translation failed: %s", exc)
        return [a.title[:70] for a in articles]


def format_newsletter(
    papers: list[tuple],  # [(content, paper_url, source, alt_sources?), ...]
    date_str: str,
    side_articles: list | None = None,
) -> str:
    """
    Assemble the Telegram newsletter text from multiple papers.
    papers: list of (content_dict, paper_url, source[, alt_sources]) — first is lead.
    """
    THIN_SEP = "───────────────────────────────────────"
    THICK_SEP = "═══════════════════════════════════════"

    def _render_highlight(content: dict, paper_url: str, source: str,
                          alt_sources: list) -> str:
        concepts = content.get("key_concepts", [])[:3]
        concepts_text = "\n".join(
            f"• *{c['concept']}:* {c['explanation']}"
            for c in concepts if c.get("concept")
        )
        src_label = _SOURCE_DISPLAY.get(source, source)
        source_links = f"[{src_label}]({paper_url})"
        for alt_src, alt_url in (alt_sources or []):
            alt_label = _SOURCE_DISPLAY.get(alt_src, alt_src)
            source_links += f" | [{alt_label}]({alt_url})"
        return (
            f"🏆 _{content.get('study_phase', '')}_\n\n"
            f"*{content.get('title_simplified', '')}*\n\n"
            f"🔹 *Für Anfänger erklärt:*\n"
            f"{content.get('simple_explanation', '')}\n\n"
            f"🔹 *Key Concepts:*\n"
            f"{concepts_text}\n\n"
            f"🔹 *Warum relevant für dich:*\n"
            f"{content.get('relevance', '')}\n\n"
            f"🔹 *Warum ist das ein Durchbruch:*\n"
            f"{content.get('why_breakthrough', '')}\n\n"
            f"{source_links}"
        )

    # Lead intro from first paper
    lead_content = papers[0][0] if papers else {}
    intro_text = lead_content.get("intro", "")

    highlights_parts = []
    for entry in papers:
        content, paper_url, source = entry[0], entry[1], entry[2]
        alt_sources = entry[3] if len(entry) > 3 else []
        highlights_parts.append(_render_highlight(content, paper_url, source, alt_sources))
    highlights_block = f"\n\n{THIN_SEP}\n\n".join(highlights_parts)

    # Build Worth Watching section
    worth_watching = ""
    if side_articles:
        medical, ai_papers = [], []
        for a in side_articles[:10]:
            if a.source in ("biorxiv", "medrxiv", "journal"):
                medical.append(a)
            else:
                ai_papers.append(a)

        def _build_section(header: str, articles_subset: list) -> str:
            subset = articles_subset[:5]
            de_titles = _translate_titles_to_german(subset)
            lines = []
            for i, (a, de_title) in enumerate(zip(subset, de_titles)):
                src_label = _SOURCE_DISPLAY.get(a.source, a.source)
                emoji = _NUMBER_EMOJIS[i] if i < len(_NUMBER_EMOJIS) else f"{i+1}."
                lines.append(f"{emoji} {de_title} – [{src_label}]({a.url})")
            return header + "\n\n" + "\n\n".join(lines)

        sections = []
        if medical:
            sections.append(_build_section("🧬 *Medizin & Biotech*", medical))
        if ai_papers:
            sections.append(_build_section("📊 *AI Research*", ai_papers))

        if sections:
            worth_watching = (
                f"{THICK_SEP}\n\n"
                "💡 *AUCH INTERESSANT*\n\n"
                + "\n\n".join(sections)
                + "\n\n"
            )

    fun_fact = lead_content.get("fun_fact", "")

    return (
        f"📡 *DAILY SIGNALS*\n"
        f"_Breakthroughs decoded for you_\n\n"
        f"☀️ Morgen Leon!\n\n"
        f"{intro_text}\n\n"
        f"{THICK_SEP}\n\n"
        f"🏆 *HIGHLIGHTS DES TAGES*\n\n"
        f"{highlights_block}\n\n"
        f"{worth_watching}"
        f"{THICK_SEP}\n\n"
        f"💬 Fun Fact: _{fun_fact}_\n\n"
        f"💬 Viel Spaß beim Lernen. Bis morgen, Leon! 🧬"
    )


def _format_learning_resource(_content: dict) -> str:
    """Pick a random video from learning_videos.json."""
    videos_file = pathlib.Path(__file__).parent / "learning_videos.json"
    try:
        videos = json.loads(videos_file.read_text())
        if videos:
            pick = random.choice(videos)
            return f"[{pick['title']}]({pick['url']})"
    except Exception as exc:
        logger.warning("Could not load learning_videos.json: %s", exc)
    return "[Kurzgesagt – In a Nutshell](https://www.youtube.com/@kurzgesagt)"


def _fallback_newsletter(title: str) -> dict:
    return {
        "title_simplified": title[:80],
        "intro": "Heute gibt es einen spannenden wissenschaftlichen Durchbruch für dich. Schau selbst.",
        "study_phase": "🔬 Wissenschaftliche Studie",
        "simple_explanation": "Forscher haben neue Erkenntnisse in diesem Bereich gewonnen.",
        "key_concepts": [],
        "relevance": "Diese Forschung könnte zukünftige Behandlungen beeinflussen.",
        "why_breakthrough": "Ein neuer Ansatz in der Wissenschaft.",
        "fun_fact": "Wissenschaft macht täglich kleine Schritte, die zusammen Großes bewegen.",
    }


def slugify_title(title: str, max_len: int = 50) -> str:
    """Convert paper title to a safe filename slug."""
    slug = title.lower()
    slug = re.sub(r"[äöü]", lambda m: {"ä": "ae", "ö": "oe", "ü": "ue"}[m.group()], slug)
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")[:max_len].rstrip("_")
    return slug or "podcast"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    return text


def _fallback_analysis(title: str) -> dict:
    return {
        "context": f"Forscher haben sich mit dem Thema '{title}' beschäftigt.",
        "research_question": "Was können wir über dieses Thema lernen?",
        "methodology": "Moderne wissenschaftliche Methoden wurden eingesetzt.",
        "findings": "Die Studie liefert neue Erkenntnisse.",
        "why_breakthrough": "Diese Arbeit trägt zum Fortschritt bei.",
        "key_concepts": [],
        "implications": "Die Ergebnisse könnten Forschung beeinflussen.",
        "limitations": "Weitere Studien sind notwendig.",
        "practical_applications": "Anwendungen könnten folgen.",
        "wow_factor": "Wissenschaft macht täglich Schritte nach vorne.",
    }


def _fallback_script(title: str, source: str) -> str:
    return f"""[INTRO-JINGLE]

Heute geht es um eine Studie, die mich wirklich beschäftigt hat.
[PP]
{title}
[PPP]
[TRANSITION]
Leider war der Abstract dieser Studie zu kurz, um eine vollständige Episode zu machen.
[PP]
Aber schau sie dir an – {source} hat sie veröffentlicht, und das Thema klingt spannend.
[PPP]
[OUTRO-JINGLE]"""
