#!/usr/bin/env python3
"""
al_cfp_feed.py — Applied Linguistics CFP/Conference RSS aggregator.

Pulls items from a handful of linguistics call-for-papers sources,
keeps only the ones that look relevant to applied linguistics
(language teaching, SLA, translation, sociolinguistics, discourse
analysis, corpus linguistics, etc.), de-duplicates, sorts by date,
and writes a single clean RSS 2.0 feed you can host or subscribe to
directly in an RSS reader.

Zero third-party dependencies — standard library only, so it runs
anywhere with plain `python3 al_cfp_feed.py`.

USAGE
  python3 al_cfp_feed.py                    # fetch live sources, write output.xml
  python3 al_cfp_feed.py --out mine.xml      # custom output path
  python3 al_cfp_feed.py --local fixture.xml # parse a local file instead of URLs (offline/demo/testing)
  python3 al_cfp_feed.py --show-all          # write every item, unfiltered (for tuning keywords)

SCHEDULING IT
  This script does not run on its own — you need something to invoke
  it on a schedule and host the resulting output.xml somewhere public
  so an RSS reader can poll it. Two easy options:

  1. Cron (any machine that's on a schedule), e.g. daily at 7am:
       0 7 * * * /usr/bin/python3 /path/to/al_cfp_feed.py --out /path/to/public_html/al_cfp.xml

  2. GitHub Actions (free, no server needed) — commit this script to a
     repo with GitHub Pages enabled, and add a workflow that runs it on
     a schedule and commits output.xml. See the README this was shipped
     with for a ready-made workflow file.
"""

import argparse
import html
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, format_datetime

# ---------------------------------------------------------------------------
# 1. SOURCES — RSS feeds to pull from. Add/remove freely.
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "name": "LINGUIST List — Calls for Papers",
        "url": "https://linguistlist.org/issues/rss/calls",
    },
    {
        "name": "LINGUIST List — Conference Announcements",
        "url": "https://linguistlist.org/issues/rss/confs",
    },
    {
        "name": "Ling Alert",
        "url": "https://lingalert.com/feed/",
    },
    # WikiCFP's linguistics category page (wikicfp.com/cfp/call?conference=linguistics)
    # does not expose a stable RSS URL, so it isn't polled automatically here —
    # check it by hand periodically, or add a scraper source if you need it.
]

# ---------------------------------------------------------------------------
# 2. KEYWORDS — an item is kept if any of these appear in its title or
#    description (case-insensitive, word-boundary aware where it matters).
#    Tune this list freely; run with --show-all to see everything a source
#    publishes before deciding what to add/remove.
# ---------------------------------------------------------------------------
KEYWORDS = [
    "applied linguistics",
    "second language acquisition", r"\bsla\b",
    r"\btesol\b", r"\btefl\b", r"\btesl\b", r"\besl\b", r"\befl\b",
    "english language teaching", r"\belt\b",
    "language teaching", "language pedagogy", "language education",
    "language learning", "language testing", "language assessment",
    "language for specific purposes", r"\blsp\b",
    "computer-assisted language learning", r"\bcall\b conference",
    "translation studies", "interpreting studies", "translator training",
    "sociolinguistics", "discourse analysis", "conversation analysis",
    "pragmatics", "corpus linguistics",
    "language policy", "language planning",
    "bilingual", "multilingual", "plurilingual",
    "heritage language", "language contact",
    "world englishes", "english as a lingua franca", r"\belf\b",
    "intercultural communication", "language and identity",
    "language attitudes", "language variation",
    "task-based language", "content and language integrated",
    r"\bclil\b", "language teacher education", "materials development",
    "second language writing", "second language reading",
    "language assessment literacy", "vocabulary acquisition",
    "language attrition", "language socialization",
]
KEYWORD_PATTERN = re.compile("|".join(KEYWORDS), re.IGNORECASE)

USER_AGENT = "AppliedLinguisticsCFPBot/1.0 (personal RSS aggregator)"


def fetch(url_or_path: str) -> bytes:
    """Fetch a URL, or read a local file if it looks like a path."""
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        req = urllib.request.Request(url_or_path, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    with open(url_or_path, "rb") as f:
        return f.read()


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def parse_rss(xml_bytes: bytes, source_name: str):
    """Parse an RSS 2.0 (or close enough) document into a list of item dicts."""
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  ! could not parse feed from {source_name}: {e}", file=sys.stderr)
        return items

    for item in root.iter("item"):
        def text_of(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        title = html.unescape(text_of("title"))
        link = text_of("link")
        guid = text_of("guid") or link
        pub_date_raw = text_of("pubDate")
        description = html.unescape(strip_tags(text_of("description"))).strip()
        description = re.sub(r"\s+", " ", description)

        pub_dt = None
        if pub_date_raw:
            try:
                pub_dt = parsedate_to_datetime(pub_date_raw)
            except (TypeError, ValueError):
                # LINGUIST List uses ISO 8601 with offset, e.g. 2026-09-04T11:05:02-04:00
                try:
                    pub_dt = datetime.fromisoformat(pub_date_raw)
                except ValueError:
                    pub_dt = None
        if pub_dt and pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)

        items.append({
            "title": title,
            "link": link,
            "guid": guid,
            "pub_dt": pub_dt,
            "description": description,
            "source": source_name,
        })
    return items


def is_relevant(item) -> bool:
    haystack = f"{item['title']} {item['description']}"
    return bool(KEYWORD_PATTERN.search(haystack))


def dedupe(items):
    seen = set()
    out = []
    for it in items:
        key = it["guid"] or it["link"] or it["title"]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def build_rss(items, feed_title, feed_link, feed_description) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        f"    <title>{html.escape(feed_title)}</title>",
        f"    <link>{html.escape(feed_link)}</link>",
        f"    <description>{html.escape(feed_description)}</description>",
        "    <language>en-us</language>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
        "    <generator>al_cfp_feed.py</generator>",
    ]
    for it in items:
        pub_str = format_datetime(it["pub_dt"]) if it["pub_dt"] else ""
        desc = it["description"]
        if len(desc) > 500:
            desc = desc[:497].rsplit(" ", 1)[0] + "..."
        title_with_source = f"[{it['source']}] {it['title']}"
        parts.append("    <item>")
        parts.append(f"      <title>{html.escape(title_with_source)}</title>")
        parts.append(f"      <link>{html.escape(it['link'])}</link>")
        parts.append(f'      <guid isPermaLink="false">{html.escape(it["guid"])}</guid>')
        if pub_str:
            parts.append(f"      <pubDate>{pub_str}</pubDate>")
        parts.append(f"      <description>{html.escape(desc)}</description>")
        parts.append("    </item>")
    parts.append("  </channel>")
    parts.append("</rss>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="output.xml", help="output RSS file path (default: output.xml)")
    ap.add_argument("--local", metavar="FILE", help="parse a single local RSS file instead of fetching SOURCES (for testing/demo)")
    ap.add_argument("--show-all", action="store_true", help="skip keyword filtering; include every item found (useful for tuning KEYWORDS)")
    args = ap.parse_args()

    all_items = []

    if args.local:
        print(f"Reading local fixture: {args.local}")
        raw = fetch(args.local)
        all_items.extend(parse_rss(raw, source_name="(local test feed)"))
    else:
        for src in SOURCES:
            print(f"Fetching: {src['name']} <{src['url']}>")
            try:
                raw = fetch(src["url"])
            except Exception as e:
                print(f"  ! failed to fetch {src['name']}: {e}", file=sys.stderr)
                continue
            parsed = parse_rss(raw, src["name"])
            print(f"  -> {len(parsed)} items")
            all_items.extend(parsed)

    all_items = dedupe(all_items)

    if args.show_all:
        kept = all_items
    else:
        kept = [it for it in all_items if is_relevant(it)]

    kept.sort(key=lambda it: it["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    print(f"\n{len(all_items)} total items fetched, {len(kept)} kept after filtering "
          f"({'no filtering — show-all mode' if args.show_all else 'applied-linguistics keyword match'}).")

    rss = build_rss(
        kept,
        feed_title="Applied Linguistics — Calls for Papers & Conferences",
        feed_link="https://linguistlist.org/",
        feed_description=(
            "Aggregated, filtered feed of calls for papers and conference "
            "announcements relevant to applied linguistics, pulled from "
            "LINGUIST List and other sources."
        ),
    )
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
