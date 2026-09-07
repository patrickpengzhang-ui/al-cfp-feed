#!/usr/bin/env python3
"""
al_cfp_crawler.py — Applied Linguistics CFP/Conference crawler.

Combines two kinds of sources into one filtered, deduplicated feed:

  1. RSS/Atom feeds (SOURCES)      — e.g. LINGUIST List, Ling Alert
  2. Crawled HTML listing pages (CRAWL_SOURCES) — e.g. WikiCFP, or any
     other site that lists CFPs/conferences without an RSS feed

For (2), rather than hard-coding fragile CSS-selector/class-name scrapers
that break the moment a site redesigns, this uses a generic block-based
extractor: it finds list-like chunks of HTML (table rows, list items,
or short paragraphs, whichever the page uses), pulls out the first link
and the surrounding text in each chunk, and applies the same
applied-linguistics keyword filter used for the RSS sources. This is
less precise than a hand-written per-site scraper but survives markup
changes much better, and works across sites you haven't added a custom
scraper for. If a specific site needs more accurate extraction (dates,
locations, deadlines as separate fields), add a small custom function to
CUSTOM_SCRAPERS — see the WikiCFP-style example below for the pattern.

Zero third-party dependencies — standard library only.

USAGE
  python3 al_cfp_crawler.py                       # crawl everything live, write output.xml
  python3 al_cfp_crawler.py --out mine.xml          # custom output path
  python3 al_cfp_crawler.py --html index.html       # also write a readable HTML landing page
  python3 al_cfp_crawler.py --no-crawl              # RSS sources only, skip HTML crawling
  python3 al_cfp_crawler.py --no-feeds              # HTML crawling only, skip RSS sources
  python3 al_cfp_crawler.py --local-feed f.xml      # parse a local RSS file instead of live feed URLs (testing)
  python3 al_cfp_crawler.py --local-html f.html --local-html-source "WikiCFP — Linguistics"
                                                     # parse a local HTML file instead of live crawl URLs (testing)
  python3 al_cfp_crawler.py --show-all              # skip keyword filtering (for tuning KEYWORDS)

SCHEDULING IT
  This script writes output files once per run — pair it with cron or a
  GitHub Actions workflow to keep the feed current and host the result
  somewhere public. See README.md for a ready-made workflow.
"""

import argparse
import html
import re
import sys
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, format_datetime

# ---------------------------------------------------------------------------
# 1. RSS/Atom SOURCES — feeds to pull from. Add/remove freely.
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
]

# ---------------------------------------------------------------------------
# 1b. CRAWL_SOURCES — HTML pages without a usable RSS feed, scraped with
#     the generic block extractor below. `base_url` is used to resolve
#     relative links found on the page.
# ---------------------------------------------------------------------------
CRAWL_SOURCES = [
    {
        "name": "WikiCFP — Linguistics",
        "url": "http://www.wikicfp.com/cfp/call?conference=linguistics",
        "base_url": "http://www.wikicfp.com",
    },
    # Add more listing pages here, e.g. association news/events pages:
    # {"name": "AAAL", "url": "https://www.aaal.org/...", "base_url": "https://www.aaal.org"},
    # Sites with heavier JS or unusual layouts may need a custom function
    # in CUSTOM_SCRAPERS instead of relying on the generic extractor.
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
    "computer-assisted language learning", r"\bcall\b conference", "nlp4call",
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

USER_AGENT = "AppliedLinguisticsCFPBot/1.0 (personal RSS aggregator; contact: set your email here)"


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


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(strip_tags(text or ""))).strip()


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------
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
        description = clean_text(text_of("description"))

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


# ---------------------------------------------------------------------------
# Generic HTML block extraction (for sites without RSS)
# ---------------------------------------------------------------------------
_BLOCK_PATTERNS = [
    ("tr", re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)),
    ("li", re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)),
    ("p", re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)),
]
_ANCHOR_PATTERN = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


def crawl_html_source(html_bytes: bytes, source_name: str, base_url: str, min_blocks: int = 3):
    """Generic scraper: find repeating list-like blocks, pull the first link
    and the block's text out of each, and treat that as one candidate item.
    No dates are extracted (the page's own text, kept in the description,
    usually contains them) since layouts vary too much to parse reliably
    without a per-site scraper."""
    text = html_bytes.decode("utf-8", errors="replace")
    items = []

    for label, pattern in _BLOCK_PATTERNS:
        blocks = pattern.findall(text)
        if len(blocks) < min_blocks:
            continue  # this block type doesn't seem to be how the page lists things

        for block in blocks:
            anchor = _ANCHOR_PATTERN.search(block)
            if not anchor:
                continue
            href, anchor_text = anchor.group(1), clean_text(anchor.group(2))
            if not anchor_text:
                continue
            link = urljoin(base_url, href)
            block_text = clean_text(block)
            if not block_text or len(block_text) < len(anchor_text) + 3:
                continue  # nothing but the link itself, not enough context to judge relevance
            items.append({
                "title": anchor_text,
                "link": link,
                "guid": link,
                "pub_dt": None,   # generic extractor doesn't parse dates out of arbitrary layouts
                "description": block_text,
                "source": source_name,
            })
        if items:
            break  # stop after the first block type that produced results

    return items


# ---------------------------------------------------------------------------
# Custom per-site scrapers (optional, higher precision than the generic one)
# ---------------------------------------------------------------------------
class _WikiCFPRowParser(HTMLParser):
    """Example custom scraper: WikiCFP lists each conference as a table row
    with the event link in one cell and 'When / Where / Deadline' as plain
    text in the following cells. This walks the real tag tree (rather than
    block regex) to pull those fields out separately. Use this as a template
    if you need structured fields (not just a text blob) from a particular
    site."""

    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.rows = []
        self._in_row = False
        self._cell_texts = []
        self._current_cell = None
        self._current_href = None
        self._current_title = None
        self._in_anchor = False
        self._anchor_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._in_row = True
            self._cell_texts = []
        elif tag in ("td", "th") and self._in_row:
            self._current_cell = []
        elif tag == "br" and self._current_cell is not None:
            self._current_cell.append(" ")
        elif tag == "a" and self._in_row and self._current_cell is not None and self._current_href is None:
            href = attrs.get("href", "")
            if "showcfp" in href:  # WikiCFP event detail links
                self._current_href = urljoin(self.base_url, href)
                self._in_anchor = True
                self._anchor_text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_anchor:
            self._current_title = clean_text("".join(self._anchor_text))
            self._in_anchor = False
        elif tag in ("td", "th") and self._current_cell is not None:
            cell_text = clean_text("".join(self._current_cell))
            self._cell_texts.append(cell_text)
            self._current_cell = None
        elif tag == "tr":
            if self._current_href and self._current_title:
                self.rows.append({
                    "title": self._current_title,
                    "link": self._current_href,
                    "extra": " | ".join(t for t in self._cell_texts if t),
                })
            self._in_row = False
            self._current_href = None
            self._current_title = None

    def handle_data(self, data):
        if self._in_anchor:
            self._anchor_text.append(data)
        if self._current_cell is not None:
            self._current_cell.append(data)


def crawl_wikicfp(html_bytes: bytes, source_name: str, base_url: str):
    text = html_bytes.decode("utf-8", errors="replace")
    parser = _WikiCFPRowParser(base_url)
    parser.feed(text)
    items = []
    for row in parser.rows:
        items.append({
            "title": row["title"],
            "link": row["link"],
            "guid": row["link"],
            "pub_dt": None,
            "description": row["extra"],
            "source": source_name,
        })
    return items


# Map a CRAWL_SOURCES "name" to a custom scraper function, if you have one.
# Any source not listed here falls back to the generic block extractor.
CUSTOM_SCRAPERS = {
    "WikiCFP — Linguistics": crawl_wikicfp,
}


# ---------------------------------------------------------------------------
# Shared: filtering, dedup, output
# ---------------------------------------------------------------------------
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
        "    <generator>al_cfp_crawler.py</generator>",
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


def build_html(items, feed_title, feed_description, feed_xml_filename) -> str:
    """Render a simple static landing page listing the current items,
    with a link to subscribe to the underlying RSS file."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for it in items:
        date_str = it["pub_dt"].strftime("%d %b %Y") if it["pub_dt"] else ""
        desc = it["description"]
        if len(desc) > 320:
            desc = desc[:317].rsplit(" ", 1)[0] + "..."
        rows.append(f"""
        <li class="item">
          <div class="item-meta">
            <span class="source">{html.escape(it['source'])}</span>
            {f'<span class="date">{html.escape(date_str)}</span>' if date_str else ''}
          </div>
          <a class="item-title" href="{html.escape(it['link'])}" target="_blank" rel="noopener">{html.escape(it['title'])}</a>
          <p class="item-desc">{html.escape(desc)}</p>
        </li>""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(feed_title)}</title>
<link rel="alternate" type="application/rss+xml" title="{html.escape(feed_title)}" href="{html.escape(feed_xml_filename)}">
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    max-width: 760px;
    margin: 0 auto;
    padding: 2.5rem 1.25rem 4rem;
    line-height: 1.5;
    color: #1a1a1a;
    background: #fafafa;
  }}
  header {{ margin-bottom: 2rem; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 0.4rem; }}
  .subtitle {{ color: #555; margin-bottom: 1rem; }}
  .meta-bar {{
    display: flex; gap: 1rem; align-items: center; flex-wrap: wrap;
    font-size: 0.85rem; color: #666;
  }}
  .rss-link {{
    display: inline-flex; align-items: center; gap: 0.35rem;
    background: #ff7a2f; color: white; text-decoration: none;
    padding: 0.35rem 0.8rem; border-radius: 999px; font-weight: 600;
    font-size: 0.85rem;
  }}
  .rss-link:hover {{ background: #e5691f; }}
  ul.items {{ list-style: none; margin: 0; padding: 0; }}
  li.item {{
    padding: 1.1rem 0;
    border-bottom: 1px solid #e2e2e2;
  }}
  li.item:last-child {{ border-bottom: none; }}
  .item-meta {{
    display: flex; gap: 0.6rem; font-size: 0.78rem;
    text-transform: uppercase; letter-spacing: 0.02em;
    color: #888; margin-bottom: 0.3rem;
  }}
  .item-meta .source {{ font-weight: 600; color: #ff7a2f; }}
  .item-title {{
    display: block; font-size: 1.05rem; font-weight: 600;
    color: #111; text-decoration: none; margin-bottom: 0.3rem;
  }}
  .item-title:hover {{ text-decoration: underline; }}
  .item-desc {{ font-size: 0.92rem; color: #444; margin: 0; }}
  footer {{ margin-top: 3rem; font-size: 0.8rem; color: #888; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #16171a; color: #e6e6e6; }}
    .subtitle, .meta-bar {{ color: #a0a0a0; }}
    li.item {{ border-bottom-color: #2a2b2f; }}
    .item-title {{ color: #f2f2f2; }}
    .item-desc {{ color: #c2c2c2; }}
  }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(feed_title)}</h1>
  <p class="subtitle">{html.escape(feed_description)}</p>
  <div class="meta-bar">
    <a class="rss-link" href="{html.escape(feed_xml_filename)}">Subscribe (RSS)</a>
    <span>{len(items)} open calls listed</span>
    <span>Last updated {html.escape(now_str)}</span>
  </div>
</header>
<ul class="items">{''.join(rows) if rows else '<li class="item">No items matched the current filters.</li>'}</ul>
<footer>
  Auto-generated by <code>al_cfp_crawler.py</code>. Sources: LINGUIST List, Ling Alert, WikiCFP.
</footer>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="output.xml", help="output RSS file path (default: output.xml)")
    ap.add_argument("--html", metavar="FILE", help="also write a static HTML landing page to this path (e.g. docs/index.html)")
    ap.add_argument("--no-feeds", action="store_true", help="skip RSS/Atom SOURCES")
    ap.add_argument("--no-crawl", action="store_true", help="skip HTML CRAWL_SOURCES")
    ap.add_argument("--local-feed", metavar="FILE", help="parse a local RSS file instead of fetching SOURCES (testing)")
    ap.add_argument("--local-html", metavar="FILE", help="parse a local HTML file instead of fetching CRAWL_SOURCES (testing)")
    ap.add_argument("--local-html-source", metavar="NAME", default="WikiCFP — Linguistics",
                     help="source name to use for --local-html, so the matching CUSTOM_SCRAPERS entry (if any) is picked (default: %(default)s)")
    ap.add_argument("--local-html-base", metavar="URL", default="http://www.wikicfp.com",
                     help="base URL for resolving relative links in --local-html (default: %(default)s)")
    ap.add_argument("--show-all", action="store_true", help="skip keyword filtering; include every item found (useful for tuning KEYWORDS)")
    args = ap.parse_args()

    all_items = []

    # --- RSS/Atom sources ---
    if args.local_feed:
        print(f"Reading local feed fixture: {args.local_feed}")
        raw = fetch(args.local_feed)
        all_items.extend(parse_rss(raw, source_name="(local test feed)"))
    elif not args.no_feeds:
        for src in SOURCES:
            print(f"Fetching feed: {src['name']} <{src['url']}>")
            try:
                raw = fetch(src["url"])
            except Exception as e:
                print(f"  ! failed to fetch {src['name']}: {e}", file=sys.stderr)
                continue
            parsed = parse_rss(raw, src["name"])
            print(f"  -> {len(parsed)} items")
            all_items.extend(parsed)

    # --- Crawled HTML sources ---
    if args.local_html:
        print(f"Reading local HTML fixture: {args.local_html} (as '{args.local_html_source}')")
        raw = fetch(args.local_html)
        scraper = CUSTOM_SCRAPERS.get(args.local_html_source, crawl_html_source)
        parsed = scraper(raw, args.local_html_source, args.local_html_base)
        print(f"  -> {len(parsed)} items")
        all_items.extend(parsed)
    elif not args.no_crawl:
        for src in CRAWL_SOURCES:
            print(f"Crawling: {src['name']} <{src['url']}>")
            try:
                raw = fetch(src["url"])
            except Exception as e:
                print(f"  ! failed to fetch {src['name']}: {e}", file=sys.stderr)
                continue
            scraper = CUSTOM_SCRAPERS.get(src["name"], crawl_html_source)
            parsed = scraper(raw, src["name"], src["base_url"])
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

    feed_title = "Applied Linguistics — Calls for Papers & Conferences"
    feed_description = (
        "Aggregated, filtered feed of calls for papers and conference "
        "announcements relevant to applied linguistics, crawled and "
        "pulled from LINGUIST List, Ling Alert, WikiCFP and other sources."
    )

    rss = build_rss(
        kept,
        feed_title=feed_title,
        feed_link="https://linguistlist.org/",
        feed_description=feed_description,
    )
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Wrote {args.out}")

    if args.html:
        xml_filename = args.out.rsplit("/", 1)[-1]
        page = build_html(kept, feed_title, feed_description, xml_filename)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(page)
        print(f"Wrote {args.html}")


if __name__ == "__main__":
    main()
