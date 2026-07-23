"""
poller.py — hourly beach-cam scraper.

Replaces the manual "upload a frame every morning" step for the two beaches
that have cameras. Fetches the newest frame from video-monitoring.com and pushes
it through the SAME code path a manual upload takes (main.run_ingest), so
ingestion method never leaks into model or scoring code.

Run it directly:

    python poller.py                # every configured beach
    python poller.py lake-worth     # just one
    python poller.py --force        # ignore the daylight window and the dedup

─────────────────────────────────────────────────────────────────────────────
HOW THE CAM SITE ACTUALLY WORKS

There is no API. The slideshow page is driven by a plain-text manifest that
lists every frame the camera has ever published, whitespace-delimited:

    pics/s4  *jan0516k  j211310i  j211327l  …  *jul1626r  …  l221217x  l221235t
    └─ base    └─ directory marker (weekly rotation)          └─ newest frame

  - token 1 is the base path
  - a token starting with '*' CHANGES the current directory
  - every other token is an image stem inside the current directory
  - the LAST stem is the most recent frame

Two variants of each frame exist, and the naming is the opposite of what you'd
guess — the manifest token is the FULL-RESOLUTION original, and replacing its
last character with '_' gives the downscaled one:

    …/pics/s4/jul1626r/l221235t.jpg   5184x3456, ~1.5 MB   (original)
    …/pics/s4/jul1626r/l221235_.jpg   1200x800,  ~138 KB   (display)

WE USE THE 1200x800 DISPLAY VARIANT, deliberately. CrowdCounter slices with SAHI
at 512px with 20% overlap, so 1200x800 is ~6 tiles (seconds) while the original
is ~117 tiles (minutes on CPU). The tile count also changes the apparent size of
a person within each tile, so switching resolutions would silently shift
crowd_count relative to every reading already in the database.

WHICH MANIFEST: station indices are NOT consistent between cameras — Lake
Worth's ss4 is "North Shore", but Boynton's ss4 is "West View" and its
"North View" is ss10. So we resolve the manifest by STATION NAME out of the
scenes[] array in slideshow.htm rather than hardcoding an index.

FRAGILITY: all of the above is scraped, not contracted. It can change without
notice. Every step here fails loudly into the log and leaves the last good
reading untouched — a broken scrape must degrade to stale data, never to a
crash or a corrupted reading.
"""

import argparse
import asyncio
import re
import sqlite3
import sys
from datetime import datetime

import httpx

# main.py resolves "data/" and "models/" RELATIVE to the working directory, so
# this must be run from backend/ (the cron entry below does exactly that).
from main import (IngestError, get_db, init_db, load_models, run_ingest,
                  snapshot_index)

# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

BASE_URL = "https://video-monitoring.com"

# Only these two beaches have cameras. Jupiter and Boca are absent on purpose:
# there is nothing to poll, and their sargassum signal is unsupported anyway.
CAMS = {
    "lake-worth": {
        "path": "beachcams/lakeworthinlet",
        "station": "North Shore",
        "fallback_manifest": "ss4.txt",   # used only if scenes[] parsing fails
    },
    "boynton": {
        "path": "beachcams/boyntoninlet",
        "station": "North View",
        "fallback_manifest": "ss10.txt",
    },
}

# Identify ourselves, same courtesy already practised for the NWS API.
USER_AGENT = "sargassum-beach-app/1.0 (beach conditions index; hourly poll)"

# The cameras publish nothing overnight (frames run roughly 06:00-20:00 local).
# Enforced HERE as well as in the cron schedule so a manual or misconfigured
# 02:00 run exits immediately instead of re-fetching the same dusk frame.
DAYLIGHT_START_HOUR = 6
DAYLIGHT_END_HOUR = 20

# The manifests are 1.2-1.7 MB and grow forever, but the server supports range
# requests. A 40 KB tail reliably spans several weekly directory markers, which
# is all we need to resolve the newest frame.
TAIL_BYTES = 40_000

HTTP_TIMEOUT = 30.0

# Matches:  scenes[6] = new scene('ss10.txt', 'North View', 'sstn/s10.jpg', 'ntl');
# Quotes vary between ' and " across cameras, hence the backreference.
_SCENE_RE = re.compile(
    r"""new\s+scene\(\s*(['"])(?P<file>ss\d+\.txt)\1\s*,\s*(['"])(?P<name>[^'"]+)\3""",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────────────────────────────────

async def resolve_manifest(client: httpx.AsyncClient, cam: dict) -> str:
    """Station display name -> manifest filename, read from slideshow.htm.

    Falls back to the recorded filename if the page shape changes, so a site
    redesign degrades to "probably still right" rather than to a hard stop.
    """
    url = f"{BASE_URL}/{cam['path']}/slideshow.htm"
    try:
        r = await client.get(url, params={"station": cam["station"]})
        r.raise_for_status()
        scenes = {m.group("name").strip(): m.group("file")
                  for m in _SCENE_RE.finditer(r.text)}
        if cam["station"] in scenes:
            return scenes[cam["station"]]
        print(f"[poll]   station {cam['station']!r} not in scenes {sorted(scenes)} "
              f"— falling back to {cam['fallback_manifest']}")
    except Exception as e:
        print(f"[poll]   manifest resolve failed ({e}) "
              f"— falling back to {cam['fallback_manifest']}")
    return cam["fallback_manifest"]


async def newest_frame(client: httpx.AsyncClient, cam: dict, manifest: str) -> tuple[str, str]:
    """Return (directory, stem) for the most recent frame in the manifest.

    Reads only the tail of the file via a range request. Raises RuntimeError if
    the tail can't be interpreted, so the caller can skip this beach rather than
    guess at a URL.
    """
    url = f"{BASE_URL}/{cam['path']}/{manifest}"

    r = await client.get(url, headers={"Range": f"bytes=-{TAIL_BYTES}"})
    r.raise_for_status()
    text = r.text
    partial = r.status_code == 206

    if partial:
        # A byte range starts mid-file, so the FIRST token is very likely a
        # truncated fragment of a real one ("…26r" from "*jul1626r"). Drop it —
        # using it would produce a plausible-looking but wrong URL.
        text = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""

    tokens = text.split()
    directory, stem = None, None
    for tok in tokens:
        if tok.startswith("*"):
            directory = tok[1:]
        else:
            stem = tok

    # No directory marker in the tail means the current directory started more
    # than TAIL_BYTES ago. Rare (markers rotate weekly) but not impossible —
    # re-read the whole file rather than emit a wrong path.
    if directory is None and partial:
        print("[poll]   no directory marker in tail — refetching full manifest")
        r = await client.get(url)
        r.raise_for_status()
        directory, stem = None, None
        for tok in r.text.split()[1:]:     # token 0 is the base path
            if tok.startswith("*"):
                directory = tok[1:]
            else:
                stem = tok

    if not directory or not stem:
        raise RuntimeError(f"could not parse manifest {manifest}")
    return directory, stem


def frame_urls(cam: dict, manifest: str, directory: str, stem: str) -> tuple[str, str]:
    """(display_url, original_url) for one frame.

    The base path mirrors the manifest name: ss10.txt -> pics/s10. Deriving it
    avoids a second full-file fetch just to read token 0.
    """
    station_no = manifest.removeprefix("ss").removesuffix(".txt")
    base = f"{BASE_URL}/{cam['path']}/pics/s{station_no}/{directory}"
    return (
        f"{base}/{stem[:-1]}_.jpg",   # 1200x800 — what we feed the models
        f"{base}/{stem}.jpg",         # 5184x3456 original
    )


# ─────────────────────────────────────────────────────────────────────────
# DEDUP
# ─────────────────────────────────────────────────────────────────────────

def last_stem(beach_id: str) -> str | None:
    """The source_stem of the most recent stored reading, or None."""
    try:
        with get_db() as conn:
            row = conn.execute("""
                SELECT source_stem FROM readings
                WHERE beach_id = ? AND source_stem IS NOT NULL
                ORDER BY taken_at DESC LIMIT 1
            """, (beach_id,)).fetchone()
        return row["source_stem"] if row else None
    except sqlite3.OperationalError as e:
        # Almost certainly "no such column: source_stem" on a database created
        # before the migration. init_db() adds it; say so plainly.
        print(f"[poll]   dedup query failed ({e}) — run init_db() to migrate")
        return None


# ─────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────

async def poll_beach(client: httpx.AsyncClient, beach_id: str, force: bool = False) -> str:
    """Poll one beach. Returns "ingested", "skipped" or "failed" — never raises,
    so one broken camera can't stop the other from ingesting."""
    cam = CAMS[beach_id]
    print(f"[poll] {beach_id}")

    try:
        manifest = await resolve_manifest(client, cam)
        directory, stem = await newest_frame(client, cam, manifest)
        print(f"[poll]   manifest={manifest} newest={directory}/{stem}")

        previous = last_stem(beach_id)
        if not force and stem == previous:
            print(f"[poll]   no new frame since {previous} — skipping")
            return "skipped"

        display_url, _original_url = frame_urls(cam, manifest, directory, stem)
        r = await client.get(display_url)
        r.raise_for_status()

        # Cheap guard before spending model time: a 200 that isn't an image
        # (an error page, a redirect to HTML) would otherwise fail deep inside
        # run_ingest with a much less obvious message.
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("image/"):
            raise RuntimeError(f"expected an image, got {ctype!r} from {display_url}")

        print(f"[poll]   fetched {len(r.content) // 1024} KB — running models")
        result = run_ingest(beach_id, r.content, source_stem=stem)
        # Record the blended index onto this row so the forecast timeline has a
        # real historical series. Without it /history returns the raw ML
        # signals but never the number the dashboard actually displays.
        idx = await snapshot_index(beach_id, result["reading_id"])
        print(f"[poll]   stored: coverage={result.get('coverage_pct')} "
              f"crowd={result.get('crowd_count')} "
              f"water={result.get('water_severity')} index={idx}")
        return "ingested"

    except IngestError as e:
        print(f"[poll]   BAD IMAGE: {e}")
        return "failed"
    except Exception as e:
        print(f"[poll]   FAILED: {type(e).__name__}: {e}")
        return "failed"


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scrape the newest beach cam frames.")
    parser.add_argument("beaches", nargs="*", choices=list(CAMS),
                        help="beach ids to poll (default: all with cameras)")
    parser.add_argument("--force", action="store_true",
                        help="ignore the daylight window and the dedup check")
    args = parser.parse_args(argv)

    targets = args.beaches or list(CAMS)

    hour = datetime.now().hour
    if not args.force and not (DAYLIGHT_START_HOUR <= hour < DAYLIGHT_END_HOUR):
        print(f"[poll] {hour:02d}:xx is outside the "
              f"{DAYLIGHT_START_HOUR:02d}-{DAYLIGHT_END_HOUR:02d} daylight window "
              f"— cameras publish nothing overnight. Use --force to override.")
        return 0

    # The lifespan hook never runs for a standalone script, so set up the
    # database and load the models ourselves.
    init_db()
    load_models()

    outcomes = {}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                 headers={"User-Agent": USER_AGENT},
                                 follow_redirects=True) as client:
        for beach_id in targets:
            outcomes[beach_id] = await poll_beach(client, beach_id, force=args.force)

    print(f"[poll] done: {outcomes}")

    # Non-zero only when EVERY beach failed — that's an outage worth waking up
    # for. A single camera failing is normal and shouldn't spam cron mail.
    if outcomes and all(v == "failed" for v in outcomes.values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
