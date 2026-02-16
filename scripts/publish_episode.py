#!/usr/bin/env python3
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo


# ----------------------------
# Configuration
# ----------------------------
SITE_BASE = "https://darianaghili.github.io/power_and_utilities_daily"

FEED_PATH = Path("docs/feed.xml")
BRIEF_PATH = Path("docs/briefs/latest.txt")
EPS_DIR = Path("docs/eps")

# OpenAI TTS settings
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "alloy"
OPENAI_TTS_SPEED = 1.0
OPENAI_TTS_FORMAT = "mp3"

# OpenAI TTS input limit per request (must chunk above this)
OPENAI_MAX_CHARS = 4096

# Offline fallback: espeak-ng -> wav -> ffmpeg -> mp3
FALLBACK_VOICE = "en-us+m3"
FALLBACK_SPEED = "140"
FALLBACK_PITCH = "48"

# Fallback MP3 encoding
FALLBACK_MP3_RATE = "22050"
FALLBACK_MP3_BITRATE = "64k"
FALLBACK_MP3_CHANNELS = "1"

# Audio post-processing (recommended for consistent loudness)
# Set NORMALIZE_AUDIO=0 in Actions env to disable.
NORMALIZE_AUDIO = os.environ.get("NORMALIZE_AUDIO", "1") == "1"
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"

# If set, will overwrite an existing mp3 for the same date (use cautiously)
FORCE_REGEN = os.environ.get("FORCE_REGEN", "0") == "1"


class QuotaExceededError(RuntimeError):
    """Raised when OpenAI returns insufficient_quota / exceeded quota."""


def speech_optimize(text: str) -> str:
    """
    Make text more listenable:
    - remove URLs
    - normalize whitespace
    - add pauses between sections
    - improve transitions
    """
    text = text.replace("\r\n", "\n")

    # Remove URLs entirely (they sound bad in TTS)
    text = re.sub(r"https?://\S+", "", text)

    # Remove wrapper markers if present
    text = text.replace("---- SCRIPT START ----", "").replace("---- SCRIPT END ----", "")

    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

    # Add a small pause after headline line (first line)
    lines = text.split("\n")
    if lines:
        lines[0] = lines[0].strip() + "..."
    text = "\n".join(lines)

    # Add audible pauses between paragraphs
    text = re.sub(r"\n\s*\n", "\n\n...\n\n", text)

    # Add a transition cue between numbered stories (2..N)
    text = re.sub(
        r"\n\n(\d+)\.\s",
        lambda m: ("\n\nNext story...\n\n" + m.group(0).lstrip("\n")) if m.group(1) != "1" else m.group(0),
        text,
    )

    # Replace long dashes with commas (TTS-friendly)
    text = text.replace("—", ", ").replace("–", ", ")

    # Final cleanup
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text


def chunk_text(text: str, max_chars: int) -> list[str]:
    """
    Split text into <= max_chars chunks, preferring paragraph boundaries.
    """
    paras = text.split("\n\n")
    chunks: list[str] = []
    cur = ""

    for p in paras:
        candidate = (cur + ("\n\n" if cur else "") + p).strip()
        if len(candidate) <= max_chars:
            cur = candidate
            continue

        if cur:
            chunks.append(cur)
            cur = ""

        # If one paragraph is still too long, hard-split it
        while len(p) > max_chars:
            chunks.append(p[:max_chars])
            p = p[max_chars:]
        cur = p

    if cur:
        chunks.append(cur)

    # Guard: ensure no empty chunks
    return [c for c in chunks if c.strip()]


def openai_tts_mp3(text: str) -> bytes:
    """
    Call OpenAI TTS and return MP3 bytes.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable (add it as a GitHub Actions secret).")

    url = "https://api.openai.com/v1/audio/speech"
    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "input": text,
        "format": OPENAI_TTS_FORMAT,
        "speed": OPENAI_TTS_SPEED,
        # Optional (supported by gpt-4o-mini-tts):
        # "instructions": "Professional broadcast narration. Neutral tone. Moderate pace. Clear enunciation."
    }

    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        # Treat quota/billing as a distinct condition so we can fall back cleanly.
        if e.code == 429 and ("insufficient_quota" in body or "exceeded your current quota" in body):
            raise QuotaExceededError(body) from e
        raise RuntimeError(f"OpenAI TTS HTTPError {e.code}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"OpenAI TTS request failed: {e}") from e


def concat_mp3_files(mp3_paths: list[Path], out_path: Path):
    """
    Concatenate MP3 files using ffmpeg concat demuxer.
    """
    if not mp3_paths:
        raise RuntimeError("No MP3 chunks to concatenate.")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        list_path = Path(td) / "concat_list.txt"
        lines = [f"file '{p.resolve()}'" for p in mp3_paths]
        list_path.write_text("\n".join(lines), encoding="utf-8")

        subprocess.check_call(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(out_path),
            ]
        )


def normalize_mp3(in_path: Path, out_path: Path):
    """
    Normalize loudness for consistent podcast playback.
    """
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(in_path),
            "-af",
            LOUDNORM_FILTER,
            str(out_path),
        ]
    )


def fallback_espeak_to_mp3(text: str, mp3_path: Path):
    """
    Offline fallback: espeak-ng -> wav -> ffmpeg -> mp3
    """
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        wav_path = Path(td) / "speech.wav"

        p = subprocess.run(
            ["espeak-ng", "-v", FALLBACK_VOICE, "-s", FALLBACK_SPEED, "-p", FALLBACK_PITCH, "-w", str(wav_path)],
            input=text,
            text=True,
            capture_output=True,
        )
        if p.returncode != 0:
            raise RuntimeError(f"espeak-ng failed: {p.stderr.strip()}")

        subprocess.check_call(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(wav_path),
                "-af",
                "highpass=f=80, lowpass=f=9000, "
                "acompressor=threshold=-18dB:ratio=3:attack=20:release=250, "
                + LOUDNORM_FILTER,
                "-ac",
                FALLBACK_MP3_CHANNELS,
                "-ar",
                FALLBACK_MP3_RATE,
                "-b:a",
                FALLBACK_MP3_BITRATE,
                str(mp3_path),
            ]
        )


def file_size_bytes(path: Path) -> int:
    return path.stat().st_size


def escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def update_feed_xml(
    title: str,
    description: str,
    pub_dt_utc: dt.datetime,
    enclosure_url: str,
    enclosure_len: int,
    guid: str,
):
    """
    Insert a new <item> right before the placeholder marker in feed.xml.
    Also update <lastBuildDate>.
    """
    if not FEED_PATH.exists():
        raise RuntimeError("docs/feed.xml not found.")

    xml = FEED_PATH.read_text(encoding="utf-8")

    last_build = format_datetime(pub_dt_utc)
    xml = re.sub(
        r"<lastBuildDate>.*?</lastBuildDate>",
        f"<lastBuildDate>{last_build}</lastBuildDate>",
        xml,
        flags=re.DOTALL,
    )

    # Avoid duplicates
    if guid in xml or enclosure_url in xml:
        print("Episode already present in feed; skipping feed update.")
        FEED_PATH.write_text(xml, encoding="utf-8")
        return

    item_xml = f"""
    <item>
      <title>{escape_xml(title)}</title>
      <description>{escape_xml(description)}</description>
      <itunes:summary>{escape_xml(description)}</itunes:summary>
      <pubDate>{format_datetime(pub_dt_utc)}</pubDate>
      <guid isPermaLink="false">{escape_xml(guid)}</guid>
      <enclosure url="{escape_xml(enclosure_url)}" length="{enclosure_len}" type="audio/mpeg" />
    </item>
"""

    marker = "<!-- Placeholder episode"
    if marker not in xml:
        raise RuntimeError(
            "Could not find placeholder marker in feed.xml. Keep the placeholder comment so we can insert new items safely."
        )

    xml = xml.replace(marker, item_xml + "    " + marker)
    FEED_PATH.write_text(xml, encoding="utf-8")


def generate_mp3_openai(speech_text: str, date_str: str, mp3_path: Path):
    """
    Generate MP3 via OpenAI TTS (chunk -> per-chunk mp3 -> concat -> optional normalize).
    Writes chunk artifacts to OS temp directory to avoid committing them.
    """
    chunks = chunk_text(speech_text, OPENAI_MAX_CHARS)
    print(f"TTS: OpenAI chunk count: {len(chunks)}")

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"tts_{date_str}_"))
    chunk_files: list[Path] = []

    try:
        for i, ch in enumerate(chunks, start=1):
            print(f"TTS: OpenAI chunk {i}/{len(chunks)} length={len(ch)}")
            audio_bytes = openai_tts_mp3(ch)
            chunk_path = tmp_dir / f"chunk_{i:03d}.mp3"
            chunk_path.write_bytes(audio_bytes)
            chunk_files.append(chunk_path)

        concat_mp3_files(chunk_files, mp3_path)

        if NORMALIZE_AUDIO:
            normalized = mp3_path.with_suffix(".normalized.mp3")
            normalize_mp3(mp3_path, normalized)
            normalized.replace(mp3_path)
            print("Audio normalization: succeeded.")

        print("TTS: OpenAI succeeded.")
    finally:
        for p in chunk_files:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


def main():
    if not BRIEF_PATH.exists():
        raise RuntimeError("Brief file not found: docs/briefs/latest.txt")

    EPS_DIR.mkdir(parents=True, exist_ok=True)

    is_test = os.environ.get("TEST_RUN", "0") == "1"

    now_utc = dt.datetime.now(dt.timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    date_str = now_et.strftime("%Y-%m-%d")

    if is_test:
        mp3_filename = "test-openai.mp3"
        guid = f"test-openai-{int(now_utc.timestamp())}"
        title = "OpenAI voice test"
    else:
        mp3_filename = f"{date_str}.mp3"
        guid = f"daily-brief-{date_str}"
        title = f"Daily Brief — {date_str}"

    mp3_path = EPS_DIR / mp3_filename

    if mp3_path.exists() and not FORCE_REGEN:
        print(f"Episode MP3 already exists for {date_str}; skipping publish.")
        return

    brief_raw = BRIEF_PATH.read_text(encoding="utf-8")
    speech_text = speech_optimize(brief_raw)
    print(f"TTS text length: {len(speech_text)} characters")

    # --- TTS generation: OpenAI primary, offline fallback on failure/quota ---
    try:
        generate_mp3_openai(speech_text, date_str, mp3_path)
    except QuotaExceededError as e:
        print("TTS: OpenAI quota exceeded; using offline fallback (espeak-ng).")
        fallback_espeak_to_mp3(speech_text, mp3_path)
        print("TTS: Fallback succeeded.")
    except Exception as e:
        # For any other OpenAI failure, also fall back (keeps daily publishing reliable).
        print(f"TTS: OpenAI failed: {e}")
        print("TTS: Using offline fallback (espeak-ng).")
        fallback_espeak_to_mp3(speech_text, mp3_path)
        print("TTS: Fallback succeeded.")

    enclosure_len = file_size_bytes(mp3_path)
    enclosure_url = f"{SITE_BASE}/eps/{mp3_filename}"

    # Disclosure recommended
    description = "Automated daily briefing. Narration is AI-generated. Links are in the show notes."

    if is_test:
        print("TEST_RUN=1 set; skipping feed.xml update.")
        print(f"Test MP3 written: {mp3_path} ({enclosure_len} bytes)")
        print(f"URL: {enclosure_url}")
        return

    update_feed_xml(
        title=title,
        description=description,
        pub_dt_utc=now_utc,
        enclosure_url=enclosure_url,
        enclosure_len=enclosure_len,
        guid=guid,
    )

    print(f"Published episode: {title}")
    print(f"MP3: {mp3_path} ({enclosure_len} bytes)")
    print(f"Enclosure URL: {enclosure_url}")


if __name__ == "__main__":
    main()
