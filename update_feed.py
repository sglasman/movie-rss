"""Daily TMDB → RSS pipeline.

Queries TMDB's /discover/movie for each tracked feed configuration (SVOD on
the major US services, plus Amazon's rental storefront), diffs against a
persisted "ever-seen" snapshot, and appends genuinely-new (provider, movie)
arrivals to per-feed RSS files. A separate TV feed tracks new series and new
seasons of streaming originals via /discover/tv by network.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "docs"
SEEN_PATH = DATA_DIR / "seen.json"
PENDING_PATH = DATA_DIR / "pending.json"

API_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "https://www.omdbapi.com/"
IMG_BASE = "https://image.tmdb.org/t/p/w500"
REGION = "US"
MAX_FEED_ITEMS = 300
# Only consider films originally released within this window. Trims request
# volume by ~10x and focuses each feed on actual new releases rather than
# catalog churn. 12 months covers most studio theatrical→SVOD gaps.
RELEASE_WINDOW_DAYS = 365

# A streaming-first film that dropped today has no critical reception yet, so
# the "Ask ChatGPT about reception" link comes back with nothing. Hold those
# posts this many days past the film's earliest release so reviews can land.
# Films with a prior theatrical or festival run publish immediately — reviews
# for those already exist however recently they hit streaming.
HOLD_DAYS = 7

# TMDB release_dates.type values that mean "the public/press has seen it":
# 1 = Premiere (incl. festivals), 2 = Theatrical (limited), 3 = Theatrical.
# The rest — 4 = Digital, 5 = Physical, 6 = TV — are home releases, which for a
# streaming-first film is the arrival we're posting about.
PREMIERE_RELEASE_TYPES = {1, 2, 3}
DIGITAL_RELEASE_TYPE = 4

# Pre-orders only exist on transactional storefronts, so only those feeds hold
# early listings for the digital date. An SVOD listing ahead of a "Digital"
# date is usually a streaming original with messy dates — post it.
EARLY_LISTING_MONETIZATIONS = {"rent", "buy"}
# A digital date further out than this is treated as a placeholder, not a
# reason to sit on the title. 123-day theatrical windows land well inside it.
MAX_DIGITAL_HOLD_DAYS = 90

# Placeholder prompt template for the ChatGPT critical-reception link.
# {title}, {year}, {director} are substituted before URL-encoding.
# Replace the body with your own prompt — structure (template + URL) stays the same.
CHATGPT_PROMPT_TEMPLATE = (
    "Describe the critical and audience reception of the {year} movie \"{title}\" directed by {director}. Focus on specific opinions on aspects like the tone, plotting, acting and production, not aggregator percentages. Keep it spoiler-free."
)


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

# Streaming-original series, tracked by TMDB *network* (who made/commissioned
# it) rather than watch provider (who carries it) — that's what makes a show an
# original. Separate from FEEDS: TV is diffed per season, not per provider.
TV_FEED = FeedConfig(
    slug="tv",
    title="New Streaming Originals",
    description="New series and new seasons from Netflix, HBO Max, Prime Video, Hulu, and Apple TV+.",
    monetization="flatrate",
)
TV_NETWORKS: dict[int, str] = {
    213: "Netflix",
    3186: "HBO Max",
    49: "HBO",
    1024: "Prime Video",
    453: "Hulu",
    2552: "Apple TV+",
}
# Same reasoning as HOLD_DAYS: a season that premiered this morning has no
# reception to ask about. TV dates come from TMDB's own air dates (not
# JustWatch), so the hold is just "air date + N days" — no pending store.
TV_HOLD_DAYS = 7
# Shows with any episode in this window get their season list checked. Must
# comfortably exceed TV_HOLD_DAYS so a season is still in range when its hold
# ends, with slack for missed runs. Unseen seasons that premiered before the
# window are marked seen silently (back catalog, not new).
TV_WINDOW_DAYS = 45

CHATGPT_TV_PROMPT_TEMPLATE = (
    "Describe the critical and audience reception of {subject}. Focus on specific opinions on aspects like the tone, plotting, acting and production, not aggregator percentages. Keep it spoiler-free."
)


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
    first_seen: datetime  # when the title showed up on the service
    published_at: datetime | None = None  # set when a hold delayed the post
    vote_average: float = 0.0
    vote_count: int = 0
    country: str = ""
    runtime: int | None = None
    genres: list[str] = field(default_factory=list)
    director: str = ""
    cast: list[str] = field(default_factory=list)
    mpaa: str = ""
    imdb_id: str = ""
    imdb_rating: str = ""
    rt_rating: str = ""
    mc_rating: str = ""

    @property
    def guid(self) -> str:
        # Dated, so a title re-detected after a phantom early listing gets a
        # fresh guid instead of being deduped against the bogus post by readers.
        # Items already in the feed keep the undated guid they were stored with.
        return (
            f"tmdb-{self.monetization}-{self.provider_id}-{self.movie_id}"
            f"-{self.pub_datetime:%Y%m%d}"
        )

    @property
    def tmdb_url(self) -> str:
        return f"https://www.themoviedb.org/movie/{self.movie_id}"

    @property
    def pub_datetime(self) -> datetime:
        return self.published_at or self.first_seen

    @property
    def was_held(self) -> bool:
        return self.pub_datetime.date() > self.first_seen.date()


def _redact(url: str) -> str:
    return re.sub(r"(api_key|apikey)=[^&]+", r"\1=***", url)


def _body_snippet(r: requests.Response, limit: int = 500) -> str:
    text = (r.text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def tmdb_get(session: requests.Session, path: str, params: dict) -> dict:
    params = {**params, "api_key": os.environ["TMDB_API_KEY"]}
    last_detail = ""
    for attempt in range(5):
        try:
            r = session.get(f"{API_BASE}{path}", params=params, timeout=30)
        except requests.RequestException as e:
            wait = 2 ** attempt
            last_detail = f"network error: {e}"
            print(
                f"  WARN TMDB {path} attempt {attempt + 1}/5: {last_detail}; "
                f"retry in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "2")) + 1
            last_detail = f"429 rate-limited (Retry-After={wait}s)"
            print(
                f"  WARN TMDB {_redact(r.url)} attempt {attempt + 1}/5: {last_detail}",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if 500 <= r.status_code < 600:
            wait = 2 ** attempt
            last_detail = f"{r.status_code} body={_body_snippet(r)!r}"
            print(
                f"  WARN TMDB {_redact(r.url)} attempt {attempt + 1}/5: "
                f"{last_detail}; retry in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if not r.ok:
            # 4xx other than 429 — almost certainly a request-shape bug, no
            # point retrying. Raise with full context.
            raise RuntimeError(
                f"TMDB {r.status_code} on {_redact(r.url)}: {_body_snippet(r)}"
            )

        return r.json()

    raise RuntimeError(
        f"TMDB exhausted 5 retries on {_redact(f'{API_BASE}{path}')} "
        f"(last: {last_detail})"
    )


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


def load_pending() -> dict[str, dict]:
    if not PENDING_PATH.exists():
        return {}
    return json.loads(PENDING_PATH.read_text())


def save_pending(pending: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(pending, indent=2, sort_keys=True))


def _parse_date(raw: str | None) -> date | None:
    try:
        return datetime.strptime((raw or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def all_release_dates(details: dict) -> list[tuple[int, date]]:
    """Every (type, date) pair across all countries; unparseable entries dropped."""
    out: list[tuple[int, date]] = []
    for entry in (details.get("release_dates") or {}).get("results", []):
        for rd in entry.get("release_dates") or []:
            d = _parse_date(rd.get("release_date"))
            if d is None:
                continue
            try:
                rtype = int(rd.get("type") or 0)
            except (TypeError, ValueError):
                rtype = 0
            out.append((rtype, d))
    return out


def hold_until(details: dict, primary_release: str, today: date) -> date | None:
    """Date this arrival should be posted, or None to post immediately.

    Any theatrical or festival release already behind us means reviews exist, so
    the post goes out as normal. Otherwise the film is streaming-first and we sit
    on it until HOLD_DAYS past its earliest release.
    """
    past = [(t, d) for t, d in all_release_dates(details) if d <= today]
    if any(t in PREMIERE_RELEASE_TYPES for t, _ in past):
        return None

    known = [d for _, d in past]
    primary = _parse_date(primary_release)
    if primary is not None and primary <= today:
        known.append(primary)
    # Nothing but future or missing dates — treat it as released today.
    earliest = min(known) if known else today
    due = earliest + timedelta(days=HOLD_DAYS)
    return due if due > today else None


def upcoming_digital_release(details: dict, today: date) -> date | None:
    """The film's US Digital release date, if it's still in the future.

    JustWatch sometimes lists a storefront offer weeks early — a pre-order page,
    or a bad scrape (Nolan's The Odyssey showed as an Amazon rental on 30 Sep
    with a 17 Nov digital date). An offer that predates the digital release
    isn't watchable yet, so it waits for that date and is re-checked then.

    Errs toward posting: any digital date already past means it's out, and a
    date further off than MAX_DIGITAL_HOLD_DAYS is likely a placeholder.
    """
    for entry in (details.get("release_dates") or {}).get("results", []):
        if entry.get("iso_3166_1") != REGION:
            continue
        dates = [
            d
            for rd in entry.get("release_dates") or []
            if rd.get("type") == DIGITAL_RELEASE_TYPE
            and (d := _parse_date(rd.get("release_date"))) is not None
        ]
        if not dates or min(dates) <= today:
            return None
        if min(dates) > today + timedelta(days=MAX_DIGITAL_HOLD_DAYS):
            return None
        return min(dates)
    return None


def still_offered(
    session: requests.Session, movie_id: int, provider_id: int, monetization: str
) -> bool | None:
    """Whether the provider still lists the film; None if TMDB couldn't say."""
    try:
        data = tmdb_get(session, f"/movie/{movie_id}/watch/providers", {})
    except Exception as e:
        print(f"  WARN provider re-check failed for {movie_id}: {e}", file=sys.stderr)
        return None
    offers = ((data.get("results") or {}).get(REGION) or {}).get(monetization) or []
    return any(o.get("provider_id") == provider_id for o in offers)


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
    if a.cast:
        parts.append(f"with {escape(', '.join(a.cast))}")
    return " · ".join(p for p in parts if p)


def _chatgpt_link(a: Arrival) -> str:
    year = a.release_date[:4] if a.release_date else "unknown year"
    director = a.director or "unknown director"
    prompt = CHATGPT_PROMPT_TEMPLATE.format(
        title=a.title, year=year, director=director
    )
    return f"https://chatgpt.com/?prompt={quote(prompt, safe='')}"


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
    # Held posts land up to a week after the film actually appeared, so say so
    # rather than leaving the date silently off by that much.
    held_note = (
        f" · added {a.first_seen.day} {a.first_seen:%b}, held for reviews"
        if a.was_held
        else ""
    )
    header = (
        f"<p><strong>New {verb} {escape(a.provider_name)}</strong>{held_note}</p>"
    )
    meta = _metadata_line(a)
    meta_html = f"<p>{meta}</p>" if meta else ""
    ratings = _ratings_line(a)
    ratings_html = f"<p>{ratings}</p>" if ratings else ""
    overview_html = f"<p>{escape(a.overview)}</p>" if a.overview else ""
    chatgpt_html = (
        f'<p><a href="{escape(_chatgpt_link(a))}">Ask ChatGPT about reception →</a></p>'
    )
    desc = poster_html + header + meta_html + ratings_html + overview_html + chatgpt_html
    return {
        "title": f"[{a.provider_name}] {a.title} ({year})",
        "link": a.tmdb_url,
        "guid": a.guid,
        "pubDate": format_datetime(a.pub_datetime),
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


def extract_top_cast(details: dict, n: int = 2) -> list[str]:
    cast = (details.get("credits") or {}).get("cast") or []
    cast_sorted = sorted(cast, key=lambda c: c.get("order", 9999))
    return [c.get("name", "") for c in cast_sorted[:n] if c.get("name")]


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
    except (requests.RequestException, ValueError) as e:
        print(f"  WARN OMDb {imdb_id} failed: {e}", file=sys.stderr)
        return {}
    if data.get("Response") != "True":
        print(
            f"  WARN OMDb {imdb_id}: Response=False error={data.get('Error')!r}",
            file=sys.stderr,
        )
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


def fetch_details_safe(session: requests.Session, movie_id: int) -> dict | None:
    try:
        return fetch_movie_details(session, movie_id)
    except requests.RequestException as e:
        print(f"  WARN details fetch failed for {movie_id}: {e}", file=sys.stderr)
        return None


def apply_details(a: Arrival, details: dict) -> None:
    """Hydrate an Arrival in-place from a TMDB details bundle."""
    a.vote_average = float(details.get("vote_average") or 0.0)
    a.vote_count = int(details.get("vote_count") or 0)
    a.runtime = details.get("runtime") or None
    a.genres = [g.get("name", "") for g in (details.get("genres") or []) if g.get("name")]
    a.country = extract_country(details)
    a.director = extract_director(details)
    a.cast = extract_top_cast(details, n=2)
    a.mpaa = extract_mpaa(details)
    a.imdb_id = (details.get("external_ids") or {}).get("imdb_id") or details.get("imdb_id") or ""


def apply_ratings(session: requests.Session, a: Arrival) -> None:
    if not a.imdb_id:
        return
    ratings = fetch_omdb_ratings(session, a.imdb_id)
    a.imdb_rating = ratings.get("imdb", "")
    a.rt_rating = ratings.get("rt", "")
    a.mc_rating = ratings.get("mc", "")
    time.sleep(0.1)  # be gentle with OMDb's free tier


def enrich(session: requests.Session, a: Arrival) -> None:
    """Hydrate an Arrival in-place with TMDB details + OMDb ratings."""
    details = fetch_details_safe(session, a.movie_id)
    if details is None:
        return
    apply_details(a, details)
    apply_ratings(session, a)


def release_due(
    cfg: FeedConfig,
    session: requests.Session,
    seen: dict[str, str],
    pending: dict[str, dict],
    now: datetime,
) -> list[Arrival]:
    """Pop this feed's held arrivals whose wait is up, hydrated and ready to post."""
    today = now.date()
    released: list[Arrival] = []
    for key in sorted(k for k in pending if k.startswith(f"{cfg.slug}:")):
        rec = pending[key]
        # A malformed record gets released rather than stranded in the store.
        due = _parse_date(rec.get("publish_after")) or today
        if due > today and rec.get("reason") == "digital":
            # TMDB digital dates get corrected; re-read daily so a film whose
            # date moved up (or vanished) isn't held to the stale one.
            details = fetch_details_safe(session, int(rec["movie_id"]))
            if details is not None:
                fresh = upcoming_digital_release(details, today)
                due = fresh or today
                rec["publish_after"] = due.isoformat()
        if due > today:
            continue
        offered = still_offered(
            session, int(rec["movie_id"]), int(rec["provider_id"]), cfg.monetization
        )
        if offered is None:
            continue  # TMDB hiccup — try again tomorrow
        if not offered:
            # The listing evaporated while we waited. Forget we saw it, so the
            # real arrival is detected fresh whenever it actually lands.
            del pending[key]
            seen.pop(key, None)
            print(f"  DROP {rec.get('title')} — no longer offered, will re-detect")
            continue
        arrived = rec.get("arrived")
        try:
            first_seen = datetime.fromisoformat(arrived) if arrived else now
        except ValueError:
            first_seen = now
        if rec.get("reason") == "digital":
            # The early listing wasn't a real arrival; today is. Dating it
            # from the pre-order would misreport it as "held for reviews".
            first_seen = now
        a = Arrival(
            provider_id=int(rec["provider_id"]),
            provider_name=rec["provider_name"],
            monetization=cfg.monetization,
            movie_id=int(rec["movie_id"]),
            title=rec.get("title") or "Untitled",
            overview=rec.get("overview", ""),
            release_date=rec.get("release_date", ""),
            poster_path=rec.get("poster_path"),
            first_seen=first_seen,
            published_at=now,
        )
        # Re-enriched now, not at hold time — a week of accumulated ratings is
        # the whole reason we waited.
        enrich(session, a)
        released.append(a)
        del pending[key]
        print(f"  RELEASE {a.title} (held since {first_seen.date().isoformat()})")
        time.sleep(0.05)
    return released


def process_feed(
    cfg: FeedConfig,
    session: requests.Session,
    seen: dict[str, str],
    pending: dict[str, dict],
    now: datetime,
    bootstrap: bool,
) -> None:
    print(f"\n=== Feed: {cfg.slug} ({cfg.monetization}) ===")
    today = now.date()
    candidates: list[tuple[str, Arrival]] = []
    for pid, pname in cfg.providers.items():
        print(f"Fetching {pname} (id={pid}, {cfg.monetization})…")
        catalog = fetch_provider_catalog(session, pid, cfg.monetization)
        print(f"  {len(catalog)} titles in window")
        for mid, m in catalog.items():
            key = f"{cfg.slug}:{pid}:{mid}"
            if key in seen:
                continue
            seen[key] = today.isoformat()
            if bootstrap:
                continue
            candidates.append(
                (
                    key,
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
                    ),
                )
            )

    print(f"New arrivals for {cfg.slug}: {len(candidates)}")
    arrivals: list[Arrival] = []
    for key, a in candidates:
        details = fetch_details_safe(session, a.movie_id)
        if details is None:
            # No release data to judge by — post it rather than hold blind.
            arrivals.append(a)
            continue
        apply_details(a, details)
        due = hold_until(details, a.release_date, today)
        reason = "reviews"
        digital = (
            upcoming_digital_release(details, today)
            if cfg.monetization in EARLY_LISTING_MONETIZATIONS
            else None
        )
        if digital and (due is None or digital > due):
            due, reason = digital, "digital"
        if due:
            pending[key] = {
                "publish_after": due.isoformat(),
                "reason": reason,
                "arrived": a.first_seen.isoformat(),
                "provider_id": a.provider_id,
                "provider_name": a.provider_name,
                "movie_id": a.movie_id,
                "title": a.title,
                "overview": a.overview,
                "release_date": a.release_date,
                "poster_path": a.poster_path,
            }
            why = (
                "listed before its US digital date"
                if reason == "digital"
                else "streaming-first"
            )
            print(f"  HOLD {a.title} — {why}, posting {due.isoformat()}")
            time.sleep(0.05)
            continue
        apply_ratings(session, a)
        arrivals.append(a)
        time.sleep(0.05)

    if not bootstrap:
        arrivals.extend(release_due(cfg, session, seen, pending, now))
    new_items = [arrival_to_item(a) for a in arrivals]
    existing = load_existing_items(cfg.output_path)
    merged = new_items + existing
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    cfg.output_path.write_text(render_feed(cfg, merged), encoding="utf-8")
    print(f"Wrote {cfg.output_path} with {min(len(merged), MAX_FEED_ITEMS)} items.")


@dataclass
class SeasonArrival:
    show_id: int
    season_number: int
    network: str
    title: str
    overview: str
    air_date: date
    poster_path: str | None
    first_seen: datetime
    episode_count: int = 0
    episode_runtime: int | None = None
    vote_average: float = 0.0
    vote_count: int = 0
    country: str = ""
    genres: list[str] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    rating: str = ""
    imdb_id: str = ""
    imdb_rating: str = ""
    rt_rating: str = ""
    mc_rating: str = ""

    @property
    def is_new_series(self) -> bool:
        return self.season_number == 1

    @property
    def tmdb_url(self) -> str:
        return f"https://www.themoviedb.org/tv/{self.show_id}/season/{self.season_number}"


def fetch_tv_catalog(session: requests.Session, network_id: int) -> dict[int, dict]:
    """Return {show_id: discover-result} for the network's shows airing in the window."""
    catalog: dict[int, dict] = {}
    today = datetime.now(timezone.utc).date()
    params = {
        "with_networks": str(network_id),
        "language": "en-US",
        "include_adult": "false",
        "sort_by": "first_air_date.desc",
        "air_date.gte": (today - timedelta(days=TV_WINDOW_DAYS)).isoformat(),
        "air_date.lte": today.isoformat(),
    }
    page = 1
    while True:
        data = tmdb_get(session, "/discover/tv", {**params, "page": page})
        for s in data.get("results", []):
            catalog[s["id"]] = s
        total_pages = min(data.get("total_pages", 1), 500)
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.05)
    return catalog


def fetch_tv_details_safe(session: requests.Session, show_id: int) -> dict | None:
    try:
        return tmdb_get(
            session,
            f"/tv/{show_id}",
            {"language": "en-US", "append_to_response": "credits,content_ratings,external_ids"},
        )
    except (requests.RequestException, RuntimeError) as e:
        print(f"  WARN TV details fetch failed for {show_id}: {e}", file=sys.stderr)
        return None


def build_season_arrival(
    details: dict, season: dict, network: str, aired: date, now: datetime
) -> SeasonArrival:
    n = int(season["season_number"])
    # Season overviews are often blank for later seasons; the show's still helps.
    overview = season.get("overview") or details.get("overview") or ""
    runtimes = details.get("episode_run_time") or []
    runtime = runtimes[0] if runtimes else (details.get("last_episode_to_air") or {}).get("runtime")
    rating = next(
        (
            r.get("rating", "")
            for r in (details.get("content_ratings") or {}).get("results", [])
            if r.get("iso_3166_1") == REGION
        ),
        "",
    )
    return SeasonArrival(
        show_id=int(details["id"]),
        season_number=n,
        network=network,
        title=details.get("name") or details.get("original_name") or "Untitled",
        overview=overview,
        air_date=aired,
        poster_path=season.get("poster_path") or details.get("poster_path"),
        first_seen=now,
        episode_count=int(season.get("episode_count") or 0),
        episode_runtime=runtime or None,
        vote_average=float(details.get("vote_average") or 0.0),
        vote_count=int(details.get("vote_count") or 0),
        country=extract_country(details),
        genres=[g.get("name", "") for g in (details.get("genres") or []) if g.get("name")],
        creators=[c.get("name", "") for c in (details.get("created_by") or []) if c.get("name")],
        cast=extract_top_cast(details, n=2),
        rating=rating,
        imdb_id=(details.get("external_ids") or {}).get("imdb_id") or "",
    )


def _tv_chatgpt_link(a: SeasonArrival) -> str:
    year = a.air_date.year
    if a.is_new_series:
        subject = f'the {year} TV series "{a.title}" ({a.network})'
    else:
        subject = (
            f'season {a.season_number} ({year}) of the TV series "{a.title}" '
            f"({a.network}), and how it compares to earlier seasons"
        )
    prompt = CHATGPT_TV_PROMPT_TEMPLATE.format(subject=subject)
    return f"https://chatgpt.com/?prompt={quote(prompt, safe='')}"


def season_to_item(a: SeasonArrival) -> dict:
    poster_html = (
        f'<p><img src="{IMG_BASE}{a.poster_path}" alt="{escape(a.title)}"/></p>'
        if a.poster_path
        else ""
    )
    what = "New series" if a.is_new_series else f"Season {a.season_number}"
    header = (
        f"<p><strong>{what} on {escape(a.network)}</strong>"
        f" · premiered {a.air_date.day} {a.air_date:%b}</p>"
    )
    episodes = (
        f"{a.episode_count} episode{'s' if a.episode_count != 1 else ''}"
        if a.episode_count
        else ""
    )
    runtime = _runtime_str(a.episode_runtime)
    meta_parts = [
        str(a.air_date.year),
        escape(a.country),
        episodes,
        f"~{runtime}" if runtime else "",
        escape(a.rating),
        escape(", ".join(a.genres)),
        f"created by {escape(', '.join(a.creators))}" if a.creators else "",
        f"with {escape(', '.join(a.cast))}" if a.cast else "",
    ]
    meta = " · ".join(p for p in meta_parts if p)
    ratings = _ratings_line(a)  # duck-typed: same rating fields as Arrival
    desc = (
        poster_html
        + header
        + (f"<p>{meta}</p>" if meta else "")
        + (f"<p>{ratings}</p>" if ratings else "")
        + (f"<p>{escape(a.overview)}</p>" if a.overview else "")
        + f'<p><a href="{escape(_tv_chatgpt_link(a))}">Ask ChatGPT about reception →</a></p>'
    )
    title = (
        f"[{a.network}] {a.title} ({a.air_date.year})"
        if a.is_new_series
        else f"[{a.network}] {a.title} — Season {a.season_number}"
    )
    return {
        "title": title,
        "link": a.tmdb_url,
        "guid": f"tmdb-tv-{a.show_id}-s{a.season_number}",
        "pubDate": format_datetime(a.first_seen),
        "category": a.network,
        "description": desc,
    }


def process_tv_feed(
    session: requests.Session, seen: dict[str, str], now: datetime, bootstrap: bool
) -> None:
    cfg = TV_FEED
    print(f"\n=== Feed: {cfg.slug} (originals by network) ===")
    today = now.date()
    # First run with no TV history: seed rather than post every season in the
    # window. Seasons still inside the hold aren't marked, so they post on time.
    seed = bootstrap or not any(k.startswith(f"{cfg.slug}:") for k in seen)
    if seed and not bootstrap:
        print("  No TV history yet — seeding without posting.")

    shows: dict[int, int] = {}  # show_id -> network it was discovered under
    for nid, name in TV_NETWORKS.items():
        print(f"Fetching {name} (network={nid})…")
        catalog = fetch_tv_catalog(session, nid)
        print(f"  {len(catalog)} shows with episodes in window")
        for sid in catalog:
            shows.setdefault(sid, nid)

    hold_cutoff = today - timedelta(days=TV_HOLD_DAYS)
    window_start = today - timedelta(days=TV_WINDOW_DAYS)
    arrivals: list[SeasonArrival] = []
    for sid, nid in shows.items():
        details = fetch_tv_details_safe(session, sid)
        time.sleep(0.05)
        if details is None:
            continue  # nothing marked seen, so it's re-checked tomorrow
        # Label with the show's primary network among ours (a co-production can
        # list several), falling back to the one discover matched.
        network = next(
            (TV_NETWORKS[n["id"]] for n in details.get("networks") or [] if n.get("id") in TV_NETWORKS),
            TV_NETWORKS[nid],
        )
        for season in details.get("seasons") or []:
            n = season.get("season_number") or 0
            if n < 1:
                continue  # season 0 is specials
            aired = _parse_date(season.get("air_date"))
            if aired is None or aired > hold_cutoff:
                continue  # unaired or still in its hold; left unseen for later
            key = f"{cfg.slug}:{sid}:s{n}"
            if key in seen:
                continue
            seen[key] = today.isoformat()
            if seed or aired < window_start:
                continue
            a = build_season_arrival(details, season, network, aired, now)
            if a.imdb_id:
                r = fetch_omdb_ratings(session, a.imdb_id)
                a.imdb_rating, a.rt_rating, a.mc_rating = r.get("imdb", ""), r.get("rt", ""), r.get("mc", "")
                time.sleep(0.1)
            arrivals.append(a)
            print(f"  NEW {a.title} S{n} ({network}, premiered {aired.isoformat()})")

    print(f"New seasons for {cfg.slug}: {len(arrivals)}")
    arrivals.sort(key=lambda a: a.air_date, reverse=True)
    merged = [season_to_item(a) for a in arrivals] + load_existing_items(cfg.output_path)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    cfg.output_path.write_text(render_feed(cfg, merged), encoding="utf-8")
    print(f"Wrote {cfg.output_path} with {min(len(merged), MAX_FEED_ITEMS)} items.")


def main() -> int:
    bootstrap = "--bootstrap" in sys.argv
    session = requests.Session()
    seen = load_seen()
    pending = load_pending()
    now = datetime.now(timezone.utc)

    for cfg in FEEDS:
        process_feed(cfg, session, seen, pending, now, bootstrap)
    # The movie feeds are already written; a TV failure mustn't lose their
    # seen.json updates (that would re-post today's arrivals tomorrow).
    try:
        process_tv_feed(session, seen, now, bootstrap)
    except Exception:
        import traceback

        traceback.print_exc()
        print("::error::TV feed failed; movie feeds were still updated")

    save_seen(seen)
    save_pending(pending)
    if pending:
        print(f"\n{len(pending)} title(s) on hold awaiting reviews.")
    if bootstrap:
        print("\nBootstrap mode: seeded seen.json without emitting feed entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
