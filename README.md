# THIS IS COMPLETELY VIBE CODED. NO HUMAN WRITTEN CODE HERE

---

# New on US Streaming — RSS feeds

Two daily RSS feeds, built by diffing TMDB's `/discover/movie` results against a persisted snapshot (re-arrivals are suppressed):

- **`feed.xml`** — SVOD arrivals on Netflix, Max, Prime Video, Hulu, and Apple TV+.
- **`feed-rentals.xml`** — Amazon Video rentals (PVOD window — useful for films newly home-viewable that haven't reached subscription yet).

The two feeds are independent: a movie can appear in both at different times (e.g. an Amazon rental in April, then a Netflix arrival in October), and Netflix originals will only ever appear in the SVOD feed.

## Setup

1. Create a new GitHub repo and push this directory to it.
2. Get a TMDB API key: <https://www.themoviedb.org/settings/api> (free, instant for personal use).
3. In the repo's **Settings → Secrets and variables → Actions**, add a secret named `TMDB_API_KEY`.
4. In **Settings → Pages**, set source to "Deploy from a branch", branch `main`, folder `/docs`.
5. **Run the workflow manually once in bootstrap mode**:
   - Actions tab → "Update feed" → Run workflow → check `bootstrap`.
   - This seeds `data/seen.json` with everything currently streaming. Without this step, your first scheduled run would emit thousands of "new" arrivals.
6. The workflow then runs daily at 13:00 UTC. Subscribe Feedly to either or both:
   - `https://<your-username>.github.io/<repo-name>/feed.xml` (SVOD)
   - `https://<your-username>.github.io/<repo-name>/feed-rentals.xml` (Amazon rentals)

## Local run

```bash
pip install -r requirements.txt
export TMDB_API_KEY=...
python update_feed.py --bootstrap    # first time only
python update_feed.py                # subsequent runs
```

## How it works

- `data/seen.json` maps `"<feed-slug>:<provider_id>:<movie_id>"` → first-seen date. The feed-slug namespace lets the SVOD and rental feeds track the same movie independently. Committed to the repo so the next run can diff against it.
- Each daily run fetches the relevant catalog for each feed via `/discover/movie?with_watch_providers=…&with_watch_monetization_types=…&watch_region=US`, clamped to the release-date window.
- IDs not in `seen.json` become RSS items for that feed; their key is added to the snapshot so they never re-emit.
- Each feed keeps the most recent 300 items.
- Feed configurations live in `FEEDS` at the top of `update_feed.py` — add or tweak providers/monetization there.

## Caveats

- The discover query is clamped to films released in the last 12 months (`RELEASE_WINDOW_DAYS` in the script). This keeps the request count low and focuses the feed on actual new releases — older catalog additions are not tracked. Widen the constant if you want a longer tail.
- TMDB's discover endpoint caps at 500 pages (10,000 results) per query. With the release-date window in place this is unreachable, but the script still warns if it ever triggers.
- TMDB's watch-provider data is sourced from JustWatch and updates ~daily, so an arrival may show up 1–2 days after it actually lands on the service.
- "First-ever-seen" semantics mean a movie that left a service before you bootstrapped will be treated as new the first time it appears post-bootstrap. This is unavoidable without a longer history.
