 import datetime as dt
+import importlib
+import importlib.util
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
 
+# Preferred neural voice defaults (Microsoft Edge TTS).
+# Falls back to espeak-ng when edge-tts is unavailable.
+EDGE_VOICE = os.getenv("EDGE_TTS_VOICE", "en-US-AndrewNeural")
+EDGE_RATE = os.getenv("EDGE_TTS_RATE", "+0%")
+EDGE_PITCH = os.getenv("EDGE_TTS_PITCH", "+0Hz")
+
 def speech_optimize(text: str) -> str:
     """
     Make the script sound better when spoken:
     - Remove URLs
     - Remove leftover wrappers (if any)
     - Insert pauses between sections and stories
     - Add simple signposting so transitions sound natural
     """
     # Remove URLs (they sound terrible in TTS)
     text = re.sub(r"https?://\S+", "", text)
 
     # Remove any wrapper lines if they still exist
     text = text.replace("---- SCRIPT START ----", "").replace("---- SCRIPT END ----", "")
 
     # Normalize whitespace
     text = re.sub(r"\r\n", "\n", text)
     text = re.sub(r"[ \t]+", " ", text)
 
     # Add an intro pause after the title line
     # (First line is usually the title)
     lines = text.strip().split("\n")
     if lines:
         lines[0] = lines[0].strip() + "..."
     text = "\n".join(lines)
 
     # Add stronger pauses at paragraph breaks:
     # Convert blank lines into an audible pause cue.
     # espeak respects punctuation; ellipses create a noticeable pause.
     text = re.sub(r"\n\s*\n+", "\n\n...\n\n", text)
 
     # Improve story transitions:
     # Insert "Next story" before each numbered item after the first
     text = re.sub(r"\n\n(\d+)\.\s", lambda m: ("\n\nNext story...\n\n" if m.group(1) != "1" else "\n\n") + m.group(0).lstrip("\n"), text)
 
     # Replace em dashes and odd characters with TTS-friendly versions
     text = text.replace("—", ", ")
     text = text.replace("–", ", ")
 
     # Compress excessive ellipses (keep at most 3 dots)
     text = re.sub(r"\.{4,}", "...", text)
 
     # Final cleanup
     text = re.sub(r"\s+\n", "\n", text)
     text = re.sub(r"\n{3,}", "\n\n", text)
 
     return text.strip()
 
 
 def ensure_tools_exist():
-    # Ensure espeak-ng and ffmpeg exist
-    for tool in ["espeak-ng", "ffmpeg"]:
+    # Ensure ffmpeg + fallback tts engine exist.
+    for tool in ["ffmpeg", "espeak-ng"]:
         if subprocess.run(["bash", "-lc", f"command -v {tool}"], capture_output=True).returncode != 0:
             raise RuntimeError(f"Missing required tool: {tool}")
 
-def generate_mp3(text: str, mp3_path: Path):
+
+def edge_tts_available() -> bool:
+    return importlib.util.find_spec("edge_tts") is not None
+
+
+def render_with_edge_tts(text: str, wav_path: Path):
+    edge_tts = importlib.import_module("edge_tts")
+
+    async def _render():
+        communicate = edge_tts.Communicate(
+            text,
+            voice=EDGE_VOICE,
+            rate=EDGE_RATE,
+            pitch=EDGE_PITCH,
+        )
+        await communicate.save(str(wav_path))
+
+    import asyncio
+    asyncio.run(_render())
+
+
+def render_with_espeak(text: str, wav_path: Path):
     """
-    Generate WAV with espeak-ng reading from stdin, then convert to MP3 with ffmpeg.
+    Generate WAV with espeak-ng reading from stdin.
     """
-    tmp_wav = mp3_path.with_suffix(".wav")
-
     p = subprocess.run(
-        ["espeak-ng", "-v", "en-us", "-s", "145", "-p", "55", "-w", str(tmp_wav)],
+        ["espeak-ng", "-v", "en-us", "-s", "145", "-p", "55", "-w", str(wav_path)],
         input=text,
         text=True,
         capture_output=True
     )
     if p.returncode != 0:
         raise RuntimeError(f"espeak-ng failed: {p.stderr.strip()}")
 
+def generate_mp3(text: str, mp3_path: Path):
+    """
+    Prefer neural edge-tts output when available, then convert to MP3 with ffmpeg.
+    Fallback to espeak-ng for environments where edge-tts is not installed.
+    """
+    tmp_wav = mp3_path.with_suffix(".wav")
+
+    if edge_tts_available():
+        print(f"Using neural TTS voice: {EDGE_VOICE}")
+        render_with_edge_tts(text, tmp_wav)
+    else:
+        print("edge-tts not installed; using espeak-ng fallback voice.")
+        render_with_espeak(text, tmp_wav)
+
     subprocess.check_call([
     "ffmpeg", "-y",
     "-i", str(tmp_wav),
     "-af",
     "highpass=f=80, lowpass=f=9000, "
     "acompressor=threshold=-18dB:ratio=3:attack=20:release=250",
     "-ac", MP3_CHANNELS,
     "-ar", MP3_RATE,
     "-b:a", MP3_BITRATE,
     str(mp3_path)
 ])
 
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
 
EOF
)