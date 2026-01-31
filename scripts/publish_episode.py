import datetime as dt
import os
import re
import subprocess
from pathlib import Path
from email.utils import format_datetime

SITE_BASE = "https://darianaghili.github.io/power_and_utilities_daily"
FEED_PATH = Path("docs/feed.xml")
BRIEF_PATH = Path("docs/briefs/latest.txt")
EPS_DIR = Path("docs/eps")

# Audio settings: voice-only, small file sizes
MP3_BITRATE = "64k"   # good for speech
MP3_RATE = "22050"    # speech-friendly sample rate
MP3_CHANNELS = "1"    # mono

def strip_non_speech(text: str) -> str:
    """
    Keep this simple: remove URLs (they sound bad) and compress whitespace.
    """
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def ensure_tools_exist():
    # Ensure espeak-ng and ffmpeg exist
    for tool in ["espeak-ng", "ffmpeg"]:
        if subprocess.run(["bash", "-lc", f"command -v {tool}"], capture_output=True).returncode != 0:
            raise RuntimeError(f"Missing required tool: {tool}")

def generate_mp3(text: str, mp3_path: Path):
    """
    Generate WAV with espeak-ng, then convert to MP3 with ffmpeg.
    """
    tmp_wav = mp3_path.with_suffix(".wav")

    # espeak-ng -> wav
    subprocess.check_call([
        "espeak-ng",
        "-v", "en-us",
        "-s", "150",              # speed ~150 wpm-ish
        "-w", str(tmp_wav),
        text
    ])

    # wav -> mp3 (mono speech)
    subprocess.check_call([
        "ffmpeg", "-y",
        "-i", str(tmp_wav),
        "-ac", MP3_CHANNELS,
        "-ar", MP3_RATE,
        "-b:a", MP3_BITRATE,
        str(mp3_path)
    ])

    # cleanup wav
    try:
        tmp_wav.unlink()
    except Exception:
        pass

def file_size_bytes(path: Path) -> int:
    return path.stat().st_size

def update_feed_xml(title: str, description: str, pub_dt_utc: dt.datetime, enclosure_url: str, enclosure_len: int, guid: str):
    """
    Insert a new <item> right before the placeholder 'Feed initialized' item.
    Also update <lastBuildDate>.
    """
    xml = FEED_PATH.read_text(encoding="utf-8")

    # Update lastBuildDate
    last_build = format_datetime(pub_dt_utc)
    xml = re.sub(r"<lastBuildDate>.*?</lastBuildDate>",
                 f"<lastBuildDate>{last_build}</lastBuildDate>",
                 xml, flags=re.DOTALL)

    # Avoid duplicate episode insertion
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

    # Insert before placeholder "Feed initialized" item
    marker = "<!-- Placeholder episode"
    if marker not in xml:
        raise RuntimeError("Could not find placeholder marker in feed.xml to insert episode before it.")

    xml = xml.replace(marker, item_xml + "    " + marker)

    FEED_PATH.write_text(xml, encoding="utf-8")

def escape_xml(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))

def main():
    if not BRIEF_PATH.exists():
        raise RuntimeError("Brief file not found: docs/briefs/latest.txt")

    ensure_tools_exist()

    EPS_DIR.mkdir(parents=True, exist_ok=True)

    # Episode date in US/Eastern (for file naming), but publish timestamp in UTC
    # Use UTC date to avoid timezone ambiguity in GitHub runners; good enough for daily.
    now_utc = dt.datetime.now(dt.timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")

    mp3_filename = f"{date_str}.mp3"
    mp3_path = EPS_DIR / mp3_filename

    # Read brief and prep speech text
    brief_raw = BRIEF_PATH.read_text(encoding="utf-8")
    speech_text = strip_non_speech(brief_raw)

    # Generate audio (idempotent: if MP3 exists, regenerate anyway to match latest brief)
    generate_mp3(speech_text, mp3_path)

    # Build enclosure URL + file size
    enclosure_url = f"{SITE_BASE}/eps/{mp3_filename}"
    enclosure_len = file_size_bytes(mp3_path)

    # Episode metadata
    title = f"Daily Brief — {date_str}"
    description = "Automated daily briefing. Links are in the show notes."
    guid = f"daily-brief-{date_str}"

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
