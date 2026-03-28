#!/usr/bin/env python3
"""
Daily Signals Podcast Generator.
RSS → Claude storytelling script → ElevenLabs TTS → Jingle mixing → Telegram
"""
from __future__ import annotations

import io
import logging
import os
import pathlib
import random
import re
import tempfile

logger = logging.getLogger(__name__)

# Use bundled ffmpeg binary if system ffmpeg is not available (e.g. Render Python env)
try:
    import imageio_ffmpeg
    _ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["PATH"] = os.path.dirname(_ffmpeg_bin) + os.pathsep + os.environ.get("PATH", "")
    logger.info("Using bundled ffmpeg: %s", _ffmpeg_bin)
except ImportError:
    pass

# Audio files (all optional)
INTRO_JINGLE      = pathlib.Path(__file__).parent / "jingle_intro.mp3"
TRANSITION_JINGLE = pathlib.Path(__file__).parent / "jingle_transition.mp3"
OUTRO_JINGLE      = pathlib.Path(__file__).parent / "jingle_outro.mp3"
BACKGROUND_MUSIC_FILES = [
    pathlib.Path(__file__).parent / "background_music_1.mp3",
    pathlib.Path(__file__).parent / "background_music_2.mp3",
    pathlib.Path(__file__).parent / "background_music.mp3",
]

# Dynamic pause durations (ms)
PAUSES = {
    "[P]":   350,   # breath / micro-pause after short phrases
    "[PP]":  800,   # end of sentence, before next thought
    "[PPP]": 1600,  # dramatic beat, major topic shift
}

TRANSITION_GAP  = 700   # silence around transition jingles (ms)
BG_MUSIC_ENABLED = False
BG_MUSIC_VOLUME  = -22
BG_FADE_MS       = 3000

CHARLOTTE_VOICE_ID = "XB0fDUnXU5powFXDhCwa"

# All structural + pause markers
ALL_MARKERS = ["[INTRO-JINGLE]", "[TRANSITION]", "[OUTRO-JINGLE]"] + list(PAUSES.keys())

# Tone presets — mapped from [TON:xxx] markers Claude inserts in the script
# stability: low = more expressive/varied, high = steady
# style: high = stronger personality, low = neutral
TONE_PRESETS = {
    "warm":       dict(stability=0.38, similarity_boost=0.80, style=0.45, use_speaker_boost=True),
    "neugierig":  dict(stability=0.15, similarity_boost=0.80, style=0.72, use_speaker_boost=True),
    "dramatisch": dict(stability=0.10, similarity_boost=0.80, style=0.85, use_speaker_boost=True),
    "sachlich":   dict(stability=0.42, similarity_boost=0.80, style=0.30, use_speaker_boost=True),
    "begeistert": dict(stability=0.12, similarity_boost=0.80, style=0.80, use_speaker_boost=True),
}
_DEFAULT_TONE = dict(stability=0.22, similarity_boost=0.80, style=0.62, use_speaker_boost=True)
_TONE_MARKER_RE = re.compile(r"\[TON:(warm|neugierig|dramatisch|sachlich|begeistert)\]")


def _normalize_numbers(text: str) -> str:
    """Convert German-formatted numbers and symbols to spoken form for TTS."""
    # Percentages: 50% → 50 Prozent
    text = re.sub(r"(\d+(?:[,\.]\d+)?)\s*%", r"\1 Prozent", text)
    # German thousand separator (dot): 3.000 → 3000, 10.000 → 10000
    text = re.sub(
        r"\b(\d{1,3})\.(\d{3})\b",
        lambda m: m.group(1) + m.group(2),
        text,
    )
    # Decimal with dot in English notation: 0.85 → 0,85 (German comma)
    text = re.sub(
        r"\b(\d+)\.(\d+)\b",
        lambda m: f"{m.group(1)},{m.group(2)}",
        text,
    )
    # µ, μ symbols
    text = text.replace("µ", " Mikro").replace("μ", " Mikro")
    # Common units written out
    text = re.sub(r"\bkg\b", "Kilogramm", text)
    text = re.sub(r"\bmg\b", "Milligramm", text)
    text = re.sub(r"\bml\b", "Milliliter", text)
    text = re.sub(r"\bkm\b", "Kilometer", text)
    return text


class PodcastGenerator:

    # ------------------------------------------------------------------
    # Paper selection
    # ------------------------------------------------------------------

    def get_top_paper(self, articles: list):
        for a in articles:
            if a.abstract and len(a.abstract.strip()) >= 50:
                return a
        return articles[0] if articles else None

    # ------------------------------------------------------------------
    # TTS + assembly
    # ------------------------------------------------------------------

    def generate_speech(self, script: str):
        """
        Split script at markers → TTS each text block → assemble with
        jingles and dynamic pauses → optional background music.
        Tone presets ([TON:xxx]) and number normalization applied per block.
        Returns a pydub AudioSegment.
        """
        try:
            from elevenlabs import VoiceSettings
            from elevenlabs.client import ElevenLabs
            from pydub import AudioSegment
        except ImportError as exc:
            raise RuntimeError(f"Import failed ({exc}) — pip install elevenlabs pydub") from exc

        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        el_client = ElevenLabs(api_key=api_key)

        def _make_settings(tone_dict: dict) -> VoiceSettings:
            return VoiceSettings(**tone_dict)

        segments = self._split_at_markers(script, ALL_MARKERS)

        # Generate TTS for all speech blocks, respecting active tone
        speech_blocks: dict[int, object] = {}
        current_tone = dict(_DEFAULT_TONE)

        for idx, (seg_type, text) in enumerate(segments):
            if seg_type == "tone_change":
                current_tone = dict(TONE_PRESETS.get(text, _DEFAULT_TONE))
            elif seg_type == "speech" and text.strip():
                clean = _normalize_numbers(text)
                audio = self._tts_request(
                    el_client, _make_settings(current_tone), clean
                )
                if audio is not None:
                    speech_blocks[idx] = audio

        successful = len(speech_blocks)
        total_speech = sum(1 for t, _ in segments if t == "speech")
        logger.info("TTS: %d/%d speech blocks generated", successful, total_speech)
        if successful == 0:
            raise RuntimeError("All TTS requests failed — check ELEVENLABS_API_KEY and logs above")

        # Assemble final track
        jingle_intro      = self._load_audio(INTRO_JINGLE)
        jingle_transition = self._load_audio(TRANSITION_JINGLE)
        jingle_outro      = self._load_audio(OUTRO_JINGLE)

        track = AudioSegment.empty()
        for idx, (seg_type, _) in enumerate(segments):
            if seg_type == "[INTRO-JINGLE]":
                if jingle_intro:
                    track += jingle_intro
                track += AudioSegment.silent(TRANSITION_GAP)
            elif seg_type == "[TRANSITION]":
                track += AudioSegment.silent(TRANSITION_GAP)
                if jingle_transition:
                    track += jingle_transition
                else:
                    track += AudioSegment.silent(1000)
                track += AudioSegment.silent(TRANSITION_GAP)
            elif seg_type == "[OUTRO-JINGLE]":
                track += AudioSegment.silent(TRANSITION_GAP)
                if jingle_outro:
                    track += jingle_outro
            elif seg_type in PAUSES:
                track += AudioSegment.silent(PAUSES[seg_type])
            elif seg_type == "speech":
                block = speech_blocks.get(idx)
                if block:
                    track += block
            # tone_change segments produce no audio

        if BG_MUSIC_ENABLED:
            track = self._mix_background(track)

        logger.info("Audio assembled: %.1f min", len(track) / 60000)
        return track

    def _split_at_markers(self, script: str, markers: list[str]) -> list[tuple[str, str]]:
        """Split script into [(type, text)] where type is a marker name or 'speech'.
        Also handles [TON:xxx] tone markers → ('tone_change', tone_name).
        """
        result = []
        remaining = script

        while remaining:
            # Check for [TON:xxx] marker first
            tone_match = _TONE_MARKER_RE.search(remaining)
            tone_pos = tone_match.start() if tone_match else len(remaining)

            next_pos = len(remaining)
            next_marker = None
            for m in markers:
                pos = remaining.find(m)
                if pos != -1 and pos < next_pos:
                    next_pos = pos
                    next_marker = m

            # Pick whichever comes first: tone marker or structural marker
            if tone_match and tone_pos < next_pos:
                before = remaining[:tone_pos].strip()
                if before:
                    result.append(("speech", before))
                result.append(("tone_change", tone_match.group(1)))
                remaining = remaining[tone_pos + len(tone_match.group(0)):].strip()
                continue

            if next_marker is None:
                if remaining.strip():
                    result.append(("speech", remaining.strip()))
                break

            before = remaining[:next_pos].strip()
            if before:
                result.append(("speech", before))
            result.append((next_marker, ""))
            remaining = remaining[next_pos + len(next_marker):].strip()

        return result

    def _tts_request(self, el_client, voice_settings, text: str):
        try:
            from pydub import AudioSegment
            stream = el_client.text_to_speech.convert(
                voice_id=CHARLOTTE_VOICE_ID,
                text=text,
                model_id="eleven_multilingual_v2",
                voice_settings=voice_settings,
            )
            return AudioSegment.from_mp3(io.BytesIO(b"".join(stream)))
        except Exception as exc:
            logger.error("TTS failed (%s): %s", type(exc).__name__, exc)
            return None

    def _mix_background(self, voice_track):
        from pydub import AudioSegment
        available = [p for p in BACKGROUND_MUSIC_FILES if p.exists()]
        if not available:
            return voice_track
        bg = self._load_audio(random.choice(available))
        if not bg:
            return voice_track
        target_ms = len(voice_track)
        loops = (target_ms // len(bg)) + 2
        bg_quiet = ((bg * loops)[:target_ms] + BG_MUSIC_VOLUME)
        bg_quiet = bg_quiet.fade_in(BG_FADE_MS).fade_out(BG_FADE_MS)
        return voice_track.overlay(bg_quiet)

    def _load_audio(self, path: pathlib.Path):
        if not path or not path.exists():
            return None
        try:
            from pydub import AudioSegment
            return AudioSegment.from_mp3(str(path))
        except Exception as exc:
            logger.warning("Could not load %s: %s", path.name, exc)
            return None

    # ------------------------------------------------------------------
    # Export + Telegram
    # ------------------------------------------------------------------

    def export_podcast(self, audio, slug: str) -> str:
        filename = f"daily_signals_{slug}.mp3"
        output_path = os.path.join(tempfile.gettempdir(), filename)
        audio.export(output_path, format="mp3", bitrate="128k")
        logger.info("Exported: %s (%.1f min)", filename, len(audio) / 60000)
        return output_path

    async def send_to_telegram(self, mp3_path: str, bot, chat_id: str,
                                title_de: str, source: str, paper_url: str) -> None:
        import datetime
        date_str = datetime.datetime.now().strftime("%d.%m.%Y")
        caption = (
            f"🎙 *Daily Signals Podcast – {date_str}*\n\n"
            f"*{title_de}*\n\n"
            f"[{source}]({paper_url})"
        )
        with open(mp3_path, "rb") as f:
            await bot.send_audio(
                chat_id=chat_id,
                audio=f,
                caption=caption,
                parse_mode="Markdown",
            )
        logger.info("Podcast sent to Telegram")

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, analysis: dict, top_paper, script: str) -> str:
        """TTS + mix + export. Returns MP3 path."""
        from claude_analyzer import slugify_title
        slug = slugify_title(top_paper.title)
        audio = self.generate_speech(script)
        return self.export_podcast(audio, slug)
