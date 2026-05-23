"""Daily TMDB → RSS pipeline.

Queries TMDB's /discover/movie for each tracked feed configuration (SVOD on
the major US services, plus Amazon's rental storefront), diffs against a
persisted "ever-seen" snapshot, and appends genuinely-new (provider, movie)
arrivals to per-feed RSS files.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "docs"
SEEN_PATH = DATA_DIR / "seen.json"

API_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "https://www.omdbapi.com/"
IMG_BASE = "https://image.tmdb.org/t/p/w500"
REGION = "US"
MAX_FEED_ITEMS = 300
# Only consider films originally released within this window. Trims request
# volume by ~10x and focuses each feed on actual new releases rather than
# catalog churn. 12 months covers most studio theatrical→SVOD gaps.
RELEASE_WINDOW_DAYS = 365


@dataclass
class FeedConfig:
    slug: str  # used to namespace seen.json keys and name the output file
    title: str
    description: str
    monetization: str  # "flatrate" | "rent" | "buy" | "ads" | "free"
    providers: dict[int, str] = field(default_factory=dict)

    @property
    def output_path(self) -> Path:
        return PUBLIC_DIR / f"feed-{self.slug}.xml" if self.slug != "svod" else PUBLIC_DIR / "feed.xml"


FEEDS: list[FeedConfig] = [
    FeedConfig(
        slug="svod",
        title="New on US Streaming",
        description="Movies added to Netflix, Max, Prime Video, Hulu, and Apple TV+ for the first time.",
        monetization="flatrate",
        providers={
            8: "Netflix",
            1899: "Max",
            9: "Prime Video",
            15: "Hulu",
            350: "Apple TV+",
        },
    ),
    FeedConfig(
        slug="rentals",
        title="New on US Digital Rental",
        description="Movies newly available to rent on Amazon Video (PVOD window).",
        monetization="rent",
        providers={10: "Amazon Video"},
    ),
]


@dataclass
class Arrival:
    provider_id: int
    provider_name: str
    monetization: str
    movie_id: int
    title: str
    overview: str
    release_date: str
    poster_path: str | None
    first_seen: datetime
    vote_average: float = 0.0
    vote_count: int = 0
    country: str = ""
    runtime: int | None = None
    genres: list[str] = field(default_factory=list)
    director: str = ""
    mpaa: str = ""
    imdb_id: str = ""
    imdb_rating: str = ""
    rt_rating: str = ""
    mc_rating: str = ""

    @property
    def guid(self) -> str:
        return f"tmdb-{self.monetization}-{self.provider_id}-{self.movie_id}"

    @property
    def tmdb_url(self) -> str:
        return f"https://www.themoviedb.org/movie/{self.movie_id}"


def tmdb_get(session: requests.Session, path: str, params: dict) -> dict:
    params = {**params, "api_key": os.environ["TMDB_API_KEY"]}
    for attempt in range(5):
        r = session.get(f"{API_BASE}{path}", params=params, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "2"))
            time.sleep(wait + 1)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"TMDB rate-limited repeatedly for {path}")


def fetch_provider_catalog(
    session: requests.Session, provider_id: int, monetization: str
) -> dict[int, dict]:
    """Return {movie_id: discover-result} for the (provider, monetization) pair."""
    catalog: dict[int, dict] = {}
    today = datetime.now(timezone.utc).date()
    window_start = today - timedelta(days=RELEASE_WINDOW_DAYS)
    params = {
        "watch_region": REGION,
        "with_watch_providers": str(provider_id),
        "with_watch_monetization_types": monetization,
        "language": "en-US",
        "include_adult": "false",
        "sort_by": "primary_release_date.desc",
        "primary_release_date.gte": window_start.isoformat(),
        "primary_release_date.lte": today.isoformat(),
    }
    page = 1
    while True:
        data = tmdb_get(session, "/discover/movie", {**params, "page": page})
        for m in data.get("results", []):
            catalog[m["id"]] = m
        total_pages = min(data.get("total_pages", 1), 500)
        if page >= total_pages:
            if data.get("total_pages", 0) > 500:
                print(
                    f"  WARN provider {provider_id} ({monetization}): "
                    f"{data['total_pages']} pages exceeds TMDB cap of 500; truncating",
                    file=sys.stderr,
                )
            break
        page += 1
        time.sleep(0.05)
    return catalog


def load_seen() -> dict[str, str]:
    if not SEEN_PATH.exists():
        return {}
    return json.loads(SEEN_PATH.read_text())


def save_seen(seen: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True))


def load_existing_items(feed_path: Path) -> list[dict]:
    if not feed_path.exists():
        return []
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(feed_path).getroot()
    except ET.ParseError:
        return []
    items = []
    for item in root.findall(".//item"):
        items.append({child.tag: child.text or "" for child in item})
    return items


def render_feed(cfg: FeedConfig, items: list[dict]) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{escape(cfg.title)}</title>",
        "<link>https://www.themoviedb.org/</link>",
        f"<description>{escape(cfg.description)}</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items[:MAX_FEED_ITEMS]:
        out.append("<item>")
        for tag in ("title", "link", "guid", "pubDate", "category", "description"):
            val = it.get(tag)
            if not val:
                continue
            if tag == "description":
                out.append(f"<description><![CDATA[{val}]]></description>")
            elif tag == "guid":
                out.append(f'<guid isPermaLink="false">{escape(val)}</guid>')
            else:
                out.append(f"<{tag}>{escape(val)}</{tag}>")
        out.append("</item>")
    out.append("</channel>")
    out.append("</rss>")
    return "\n".join(out)


def _runtime_str(minutes: int | None) -> str:
    if not minutes:
        return ""
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _metadata_line(a: Arrival) -> str:
    year = a.release_date[:4] if a.release_date else ""
    parts = [
        escape(year) if year else "",
        escape(a.country) if a.country else "",
        escape(_runtime_str(a.runtime)),
        escape(a.mpaa) if a.mpaa else "",
        escape(", ".join(a.genres)) if a.genres else "",
    ]
    if a.director:
        parts.append(f"dir. {escape(a.director)}")
    return " · ".join(p for p in parts if p)


def _ratings_line(a: Arrival) -> str:
    parts = []
    if a.vote_count:
        parts.append(f"TMDB {a.vote_average:.1f} ({a.vote_count:,})")
    if a.imdb_rating:
        parts.append(f"IMDb {escape(a.imdb_rating)}")
    if a.rt_rating:
        parts.append(f"RT {escape(a.rt_rating)}")
    if a.mc_rating:
        parts.append(f"Metacritic {escape(a.mc_rating)}")
    return " · ".join(parts)


def arrival_to_item(a: Arrival) -> dict:
    year = a.release_date[:4] if a.release_date else "????"
    poster_html = (
        f'<p><img src="{IMG_BASE}{a.poster_path}" alt="{escape(a.title)}"/></p>'
        if a.poster_path
        else ""
    )
    verb = "to rent on" if a.monetization == "rent" else "on"
    header = (
        f"<p><strong>New {verb} {escape(a.provider_name)}</strong></p>"
    )
    meta = _metadata_line(a)
    meta_html = f"<p>{meta}</p>" if meta else ""
    ratings = _ratings_line(a)
    ratings_html = f"<p>{ratings}</p>" if ratings else ""
    overview_html = f"<p>{escape(a.overview)}</p>" if a.overview else ""
    desc = poster_html + header + meta_html + ratings_html + overview_html
    return {
        "title": f"[{a.provider_name}] {a.title} ({year})",
        "link": a.tmdb_url,
        "guid": a.guid,
        "pubDate": format_datetime(a.first_seen),
        "category": a.provider_name,
        "description": desc,
    }


def fetch_movie_details(session: requests.Session, movie_id: int) -> dict:
    """One-shot bundle: details + credits + per-country release certifications."""
    return tmdb_get(
        session,
        f"/movie/{movie_id}",
        {"language": "en-US", "append_to_response": "credits,release_dates,external_ids"},
    )


def extract_director(details: dict) -> str:
    crew = (details.get("credits") or {}).get("crew") or []
    directors = [c.get("name", "") for c in crew if c.get("job") == "Director"]
    return ", ".join(d for d in directors if d)


def extract_mpaa(details: dict) -> str:
    for entry in (details.get("release_dates") or {}).get("results", []):
        if entry.get("iso_3166_1") != "US":
            continue
        for rd in entry.get("release_dates", []) or []:
            cert = (rd.get("certification") or "").strip()
            if cert:
                return cert
    return ""


def extract_country(details: dict) -> str:
    countries = details.get("production_countries") or []
    names = [c.get("name", "") for c in countries if c.get("name")]
    return " / ".join(names)


def fetch_omdb_ratings(session: requests.Session, imdb_id: str) -> dict[str, str]:
    """Return {'imdb': '7.5', 'rt': '85%', 'mc': '72'} — empty fields if missing/unavailable.

    No-op (returns {}) when OMDB_API_KEY is not set, so the script keeps working
    without the sidecar configured.
    """
    key = os.environ.get("OMDB_API_KEY")
    if not key or not imdb_id:
        return {}
    try:
        r = session.get(
            OMDB_BASE,
            params={"i": imdb_id, "apikey": key, "r": "json"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return {}
    if data.get("Response") != "True":
        return {}
    out: dict[str, str] = {}
    imdb = data.get("imdbRating")
    if imdb and imdb != "N/A":
        out["imdb"] = imdb
    for rating in data.get("Ratings", []) or []:
        src = rating.get("Source", "")
        val = rating.get("Value", "")
        if src == "Rotten Tomatoes" and val:
            out["rt"] = val
        elif src == "Metacritic" and val:
            # OMDb returns "72/100" — keep just the numerator.
            out["mc"] = val.split("/", 1)[0]
    return out


def enrich(session: requests.Session, a: Arrival) -> None:
    """Hydrate an Arrival in-place with TMDB details + OMDb ratings."""
    try:
        details = fetch_movie_details(session, a.movie_id)
    except requests.RequestException as e:
        print(f"  WARN details fetch failed for {a.movie_id}: {e}", file=sys.stderr)
        return
    a.vote_average = float(details.get("vote_average") or 0.0)
    a.vote_count = int(details.get("vote_count") or 0)
    a.runtime = details.get("runtime") or None
    a.genres = [g.get("name", "") for g in (details.get("genres") or []) if g.get("name")]
    a.country = extract_country(details)
    a.director = extract_director(details)
    a.mpaa = extract_mpaa(details)
    a.imdb_id = (details.get("external_ids") or {}).get("imdb_id") or details.get("imdb_id") or ""

    if a.imdb_id:
        ratings = fetch_omdb_ratings(session, a.imdb_id)
        a.imdb_rating = ratings.get("imdb", "")
        a.rt_rating = ratings.get("rt", "")
        a.mc_rating = ratings.get("mc", "")
        time.sleep(0.1)  # be gentle with OMDb's free tier


def process_feed(
    cfg: FeedConfig,
    session: requests.Session,
    seen: dict[str, str],
    now: datetime,
    bootstrap: bool,
) -> None:
    print(f"\n=== Feed: {cfg.slug} ({cfg.monetization}) ===")
    today = now.date().isoformat()
    arrivals: list[Arrival] = []
    for pid, pname in cfg.providers.items():
        print(f"Fetching {pname} (id={pid}, {cfg.monetization})…")
        catalog = fetch_provider_catalog(session, pid, cfg.monetization)
        print(f"  {len(catalog)} titles in window")
        for mid, m in catalog.items():
            key = f"{cfg.slug}:{pid}:{mid}"
            if key in seen:
                continue
            seen[key] = today
            if bootstrap:
                continue
            arrivals.append(
                Arrival(
                    provider_id=pid,
                    provider_name=pname,
                    monetization=cfg.monetization,
                    movie_id=mid,
                    title=m.get("title") or m.get("original_title") or "Untitled",
                    overview=m.get("overview", ""),
                    release_date=m.get("release_date", ""),
                    poster_path=m.get("poster_path"),
                    first_seen=now,
                )
            )

    print(f"New arrivals for {cfg.slug}: {len(arrivals)}")
    for a in arrivals:
        enrich(session, a)
        time.sleep(0.05)
    new_items = [arrival_to_item(a) for a in arrivals]
    existing = load_existing_items(cfg.output_path)
    merged = new_items + existing
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    cfg.output_path.write_text(render_feed(cfg, merged), encoding="utf-8")
    print(f"Wrote {cfg.output_path} with {min(len(merged), MAX_FEED_ITEMS)} items.")


def main() -> int:
    bootstrap = "--bootstrap" in sys.argv
    session = requests.Session()
    seen = load_seen()
    now = datetime.now(timezone.utc)

    for cfg in FEEDS:
        process_feed(cfg, session, seen, now, bootstrap)

    save_seen(seen)
    if bootstrap:
        print("\nBootstrap mode: seeded seen.json without emitting feed entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
