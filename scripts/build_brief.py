import datetime as dt
import feedparser
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# ----------------------------
# Configuration (v1 sources)
# ----------------------------
FEEDS = [
    ("Utility Dive", "https://www.utilitydive.com/feeds/news/"),
    ("POWER Magazine", "https://www.powermag.com/feed/"),
    ("Renewable Energy World", "https://www.renewableenergyworld.com/feed/"),
    ("CleanTechnica", "https://cleantechnica.com/feed/"),
    ("E&E Energywire (Politico)", "https://rss.politico.com/eenews-ew"),
    # Canary's RSS endpoint sometimes changes; if this one fails, we'll swap it.
    ("Canary Media", "https://www.canarymedia.com/rss.rss"),
]

# Higher = more preferred when ranking
SOURCE_WEIGHT = {
    "E&E Energywire (Politico)": 30,
    "Utility Dive": 28,
    "Canary Media": 26,
    "POWER Magazine": 22,
    "Renewable Energy World": 20,
    "CleanTechnica": 14,
}

# US relevance signals
US_SIGNALS = [
    "ferc", "department of energy", "doe", "epa", "nerc",
    "pjm", "miso", "ercot", "caiso", "nyiso", "spp", "iso-ne",
    "united states", "u.s.", "us ",
    # states (abbrev + full names)
    "california", "texas", "new york", "florida", "illinois", "pennsylvania",
    "ohio", "georgia", "north carolina", "michigan", "new jersey", "virginia",
    "washington", "arizona", "massachusetts", "tennessee", "indiana", "missouri",
    "maryland", "wisconsin", "colorado", "minnesota", "south carolina", "alabama",
    "louisiana", "kentucky", "oregon", "oklahoma", "connecticut", "utah", "iowa",
    "nevada", "arkansas", "mississippi", "kansas", "new mexico", "nebraska",
    "west virginia", "idaho", "hawaii", "new hampshire", "maine", "montana",
    "rhode island", "delaware", "south dakota", "north dakota", "alaska", "vermont",
]

# Impact keywords
IMPACT = [
    "transmission", "interconnection", "rate case", "public utility commission",
    "grid", "reliability", "outage", "blackout", "wildfire",
    "pipeline", "lng", "nuclear", "small modular reactor", "smr",
    "data center", "load growth", "capacity market", "resource adequacy",
    "tariff", "rulemaking", "order", "permit", "financing", "acquisition", "merger"
]

# 15 minutes @ 150 wpm => 2250 words. Keep a buffer.
WORD_TARGET = 2100
WORD_HARD_CAP = 2200


def canonicalize(url: str) -> str:
    """Strip common tracking parameters to improve dedupe."""
    try:
        p = urlparse(url)
        qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
              if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}]
        new_query = urlencode(qs)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))
    except Exception:
        return url


def textify(html_or_text: str) -> str:
    # very light cleanup; RSS summaries are often HTML
    s = re.sub(r"<[^>]+>", " ", html_or_text or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_date(entry) -> dt.datetime | None:
    # Try several common RSS date fields
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return None


def score_item(source: str, title: str, summary: str, published: dt.datetime, now_utc: dt.datetime) -> int:
    base = SOURCE_WEIGHT.get(source, 10)

    age_hours = (now_utc - published).total_seconds() / 3600.0
    recency = max(0, int(40 - age_hours))  # 0..40 (rough)

    blob = f"{title} {summary}".lower()
    us = 20 if any(sig in blob for sig in US_SIGNALS) else 0
    impact = min(20, 4 * sum(1 for k in IMPACT if k in blob))  # up to 20

    return base + recency + us + impact


def is_us_relevant(title: str, summary: str) -> bool:
    blob = f"{title} {summary}".lower()
    return any(sig in blob for sig in US_SIGNALS)


def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    window_start = now_utc - dt.timedelta(hours=30)

    items = []
    seen = set()

    for source, feed_url in FEEDS:
        d = feedparser.parse(feed_url)
        for e in d.entries:
            url = canonicalize(getattr(e, "link", "") or "")
            if not url or url in seen:
                continue
            seen.add(url)

            title = (getattr(e, "title", "") or "").strip()
            summary = textify(getattr(e, "summary", "") or getattr(e, "description", "") or "")
            published = parse_date(e)
            if not published:
                continue
            if published < window_start:
                continue
            if not is_us_relevant(title, summary):
                continue

            score = score_item(source, title, summary, published, now_utc)
            items.append({
                "source": source,
                "title": title,
                "url": url,
                "published": published,
                "summary": summary,
                "score": score
            })

    # sort and pick top 5
    items.sort(key=lambda x: x["score"], reverse=True)
    top = items[:5]

    # Build script with word cap
    date_label = now_utc.astimezone(dt.timezone(dt.timedelta(hours=-5))).strftime("%Y-%m-%d")  # crude ET in winter
    lines = []
    lines.append(f"Power, Utilities & Infrastructure — Daily Brief ({date_label})")
    lines.append("")
    lines.append("This is an automated, AI-generated audio briefing. Sources and links are in the show notes.")
    lines.append("")

    # word budgeting
    def word_count(txt: str) -> int:
        return len(re.findall(r"\b\w+\b", txt))

    for idx, it in enumerate(top, 1):
        block = []
        block.append(f"{idx}. {it['title']} — {it['source']}")
        block.append(f"Link: {it['url']}")
        # 3–4 sentence spoken summary (v1: use RSS summary, trimmed)
        s = it["summary"]
        if len(s) > 600:
            s = s[:600].rsplit(" ", 1)[0] + "…"
        block.append(s)
        block.append("")
        candidate = "\n".join(block)

        if word_count("\n".join(lines) + "\n" + candidate) > WORD_TARGET and len(lines) > 6:
            # stop once we exceed target after at least some stories
            break
        lines.append(candidate)

    script = "\n".join(lines)
    # hard cap enforcement
    while word_count(script) > WORD_HARD_CAP:
        # remove last paragraph-ish block
        parts = script.strip().split("\n\n")
        if len(parts) <= 3:
            break
        parts = parts[:-1]
        script = "\n\n".join(parts) + "\n"

    # Write output to docs (published)
    out_path = "docs/briefs"
    # In GitHub Actions we will ensure folder exists before running, but keep simple here
    print("---- SCRIPT START ----")
    print(script)
    print("---- SCRIPT END ----")


if __name__ == "__main__":
    main()
