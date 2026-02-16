import datetime as dt
import json
import os
import re
import urllib.request
import urllib.error
import subprocess
import tempfile
from email.utils import format_datetime
from pathlib import Path


# ----------------------------
# Configuration
# ----------------------------
SITE_BASE = "https://darianaghili.github.io/power_and_utilities_daily"

FEED_PATH = Path("docs/feed.xml")
BRIEF_PATH = Path("docs/briefs/latest.txt")
EPS_DIR = Path("docs/eps")

# Fallback (offline) TTS settings
FALLBACK_VOICE = "en-us+m3"
FALLBACK_SPEED = "140"
FALLBACK_PITCH = "48"

# Fallback MP3 encoding
FALLBACK_MP3_RATE = "22050"
FALLBACK_MP3_BITRATE = "64k"
FALLBACK_MP3_CHANNELS = "1"

# ElevenLabs settings
ELEVEN_VOICE_ID = "pqHfZKP75CvOlQylNhV4"
ELEVEN_MODEL_ID = "eleven_multilingual_v2"
ELEVEN_OUTPUT_FORMAT = "mp3_44100_128"  # good quality MP3

# Length control for spoken output (optional safety)
MAX_CHARS = 12000  # if you hit API limits, we will add chunking later


def speech_optimize(text: str) -> str:
    """
    Make text more listenable:
    - remove URLs
    - normalize whitespace
    - add pauses between sections
    - slightly improve transitions
    """
    text = text.replace("\r\n", "\n")

    # Remove URLs entirely (they sound bad in TTS)
    text = re.sub(r"https?://\S+", "", text)

    # Remove any leftover wrapper markers if they exist
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

    # Optional: add a transition cue between numbered stories (2..N)
    # Looks for paragraph breaks followed by "2. " / "3. " etc.
    text = re.sub(r"\n\n(\d+)\.\s", lambda m: ("\n\nNext story...\n\n" + m.group(0).lstrip("\n")) if m.group(1) != "1" else m.group(0), text)

    # Replace long dashes with commas (TTS-friendly)
    text = text.replace("—", ", ").replace("–", ", ")

    # Final cleanup
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text


def elevenlabs_tts_mp3(text: str) -> bytes:
    """
    Call ElevenLabs TTS and return MP3 bytes.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ELEVENLABS_API_KEY environment variable (add it as a GitHub Actions secret).")

    if len(text) > MAX_CHARS:
        # Keep a hard stop rather than fail silently.
        # If you hit this, we will add chunking + concatenation.
        raise RuntimeError(f"Text too long for current MAX_CHARS ({MAX_CHARS}). Length={len(text)}")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}?output_format={ELEVEN_OUTPUT_FORMAT}"
    payload = {
        "text": text,
        "model_id": ELEVEN_MODEL_ID,
    }

    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "xi-api-key": api_key,
            "Accept": "audio/mpeg",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"ElevenLabs HTTPError {e.code}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"ElevenLabs request failed: {e}") from e

def fallback_espeak_to_mp3(text: str, mp3_path: Path):
    """
    Offline fallback: espeak-ng -> wav -> ffmpeg -> mp3
    """
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        wav_path = Path(td) / "speech.wav"

        # Create WAV using espeak-ng reading from stdin (robust)
        p = subprocess.run(
            ["espeak-ng", "-v", FALLBACK_VOICE, "-s", FALLBACK_SPEED, "-p", FALLBACK_PITCH, "-w", str(wav_path)],
            input=text,
            text=True,
            capture_output=True
        )
        if p.returncode != 0:
            raise RuntimeError(f"espeak-ng failed: {p.stderr.strip()}")

        # Convert WAV -> MP3 with basic radio-style processing
        subprocess.check_call([
            "ffmpeg", "-y",
            "-i", str(wav_path),
            "-af",
            "highpass=f=80, lowpass=f=9000, "
            "acompressor=threshold=-18dB:ratio=3:attack=20:release=250, "
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ac", FALLBACK_MP3_CHANNELS,
            "-ar", FALLBACK_MP3_RATE,
            "-b:a", FALLBACK_MP3_BITRATE,
            str(mp3_path)
        ])

def file_size_bytes(path: Path) -> int:
    return path.stat().st_size


def escape_xml(s: str) -> str:
    return (s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def update_feed_xml(title: str, description: str, pub_dt_utc: dt.datetime, enclosure_url: str, enclosure_len: int, guid: str):
    """
    Insert a new <item> right before the placeholder marker in feed.xml.
    Also update <lastBuildDate>.
    """
    if not FEED_PATH.exists():
        raise RuntimeError("docs/feed.xml not found.")

    xml = FEED_PATH.read_text(encoding="utf-8")

    # Update lastBuildDate
    last_build = format_datetime(pub_dt_utc)
    xml = re.sub(r"<lastBuildDate>.*?</lastBuildDate>",
                 f"<lastBuildDate>{last_build}</lastBuildDate>",
                 xml, flags=re.DOTALL)

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
        raise RuntimeError("Could not find placeholder marker in feed.xml. Keep the placeholder comment so we can insert new items safely.")

    xml = xml.replace(marker, item_xml + "    " + marker)
    FEED_PATH.write_text(xml, encoding="utf-8")


def main():
    if not BRIEF_PATH.exists():
        raise RuntimeError("Brief file not found: docs/briefs/latest.txt")

    EPS_DIR.mkdir(parents=True, exist_ok=True)

    is_test = os.environ.get("TEST_RUN", "0") == "1"

    now_utc = dt.datetime.now(dt.timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")

    if is_test:
        mp3_filename = "test-elevenlabs.mp3"
        guid = f"test-elevenlabs-{int(now_utc.timestamp())}"
        title = "ElevenLabs voice test"
    else:
        mp3_filename = f"{date_str}.mp3"
        guid = f"daily-brief-{date_str}"
        title = f"Daily Brief — {date_str}"

    mp3_path = EPS_DIR / mp3_filename

    brief_raw = BRIEF_PATH.read_text(encoding="utf-8")
    speech_text = speech_optimize(brief_raw)

    print(f"TTS text length: {len(speech_text)} characters")

    try:
        audio_bytes = elevenlabs_tts_mp3(speech_text)
        mp3_path.write_bytes(audio_bytes)
        print("TTS: ElevenLabs (primary) succeeded.")
    except Exception as e:
        print(f"TTS: ElevenLabs failed: {e}")
        print("TTS: Falling back to offline espeak-ng.")
        fallback_espeak_to_mp3(speech_text, mp3_path)
        print("TTS: Fallback succeeded.")

    enclosure_len = file_size_bytes(mp3_path)
    enclosure_url = f"{SITE_BASE}/eps/{mp3_filename}"

    description = "Automated daily briefing. Links are in the show notes."

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
        guid=guid
    )

    print(f"Published episode: {title}")
    print(f"MP3: {mp3_path} ({enclosure_len} bytes)")
    print(f"Enclosure URL: {enclosure_url}")



if __name__ == "__main__":
    main()
