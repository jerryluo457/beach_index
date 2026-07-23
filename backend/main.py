"""
main.py — FastAPI application

ARCHITECTURE NOTE
-----------------
This file is deliberately THIN. It does exactly three things:
  1. Translates HTTP <-> Python (routes, request/response shapes)
  2. Owns application lifecycle (model loading, DB init)
  3. Orchestrates calls into the other modules

It contains NO model math and NO scoring formulas. If you find yourself
writing an if/elif on coverage thresholds in here, it belongs in scoring.py.
That discipline is what lets you unit-test the actual product logic without
spinning up a server.

THE THREE MODULES IT SEWS TOGETHER
----------------------------------
  sources.py    external data (NWS weather, NOAA tides, EPA UV)  -> live, per-request
  inference.py  ML models (sargassum, water, crowd) but also ingests image through argv              -> expensive, at ingest
  scoring.py    sub-scores + geometric-mean index                -> pure functions

DATA CADENCE (the reason for the hybrid design)
-----------------------------------------------
Sargassum changes on a scale of hours-to-days and costs seconds of GPU-less
compute -> run at INGEST, store the result.
Weather changes hourly and is a cheap cached HTTP call -> fetch PER REQUEST.
Combining stale weather with fresh sargassum (or vice versa) would make one
of the two signals wrong, so the index is ASSEMBLED at request time from
stored ML output + live weather.
"""

import io
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Single source of truth for "what beaches exist" — imported, never redefined.
# Defining a second BEACHES dict here would guarantee the two drift apart.
from sources import BEACHES, fetch_beach_conditions

# scoring.py's Beach is a DATACLASS that computes scores.
# The Beach below is a PYDANTIC MODEL that describes the API response.
# Different jobs, same noun — alias the import to keep them straight.
from scoring import Beach as BeachScorer

from inference import SargassumDetector, WaterSeverityClassifier, CrowdCounter

import summarize


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

DB_PATH = "data/readings.db"
UPLOAD_DIR = Path("data/uploads")

# Which cameras the sargassum model is validated on. Tested generalization:
# works on Lake Worth (trained) and Boynton (same camera vendor/angle),
# degrades on Jupiter/Boca (different viewpoints).
#
# DESIGN CHOICE: this gate lives HERE, not in inference.py. inference.py stays
# a pure "image in -> prediction out" unit with no concept of beaches; main.py
# owns the product decision about which beaches get which features.
SARGASSUM_SUPPORTED = {"lake-worth", "boynton"}

# Fallbacks when an external signal is unavailable. scoring.py's Beach requires
# uv_index and rip_risk, so something must be passed.
#
# DESIGN CHOICE: pick NEUTRAL-to-CONSERVATIVE values, not optimistic ones.
# A missing rip-current reading should not make a beach look SAFER than one
# with a confirmed 'low' reading — this is a safety signal, so absence of
# data is not evidence of absence of risk.
DEFAULT_UV = 6.0            # mid-range for South Florida daytime
DEFAULT_RIP_RISK = "moderate"

# Every ingested frame is downscaled to this width before inference so that
# crowd counts stay comparable across beaches and across upload methods. See
# the normalization block in run_ingest() for why this is a correctness
# requirement rather than a speed knob. Changing it invalidates comparisons
# against readings already in the database.
INGEST_WIDTH = 1200


# ─────────────────────────────────────────────────────────────────────────
# LIFESPAN — load models ONCE at startup
# ─────────────────────────────────────────────────────────────────────────
# THE most important ML-serving pattern here. Loading a model takes ~1-2s of
# disk + init. Doing it inside an endpoint would make EVERY request pay that
# cost. The lifespan hook runs once when uvicorn boots, stashes the loaded
# models, and every subsequent request reuses the warm objects.
#
# The `yield` splits startup from shutdown: everything before it runs at boot,
# everything after runs when the server stops.

ml: dict = {}   # holds loaded models, shared across all requests


def load_models():
    """Populate the `ml` registry. Idempotent — safe to call twice.

    Factored out of `lifespan` because poller.py imports this module WITHOUT
    starting an ASGI server, so the lifespan hook never fires for it. Without
    this, the poller would import run_ingest, find `ml` empty, silently skip
    every model, and cheerfully store a reading with all-null scores.

    Each model is wrapped individually: a missing water_best.pt shouldn't
    prevent the (working, primary) sargassum model from serving. Degraded > dead.
    """
    if "sand" not in ml:
        try:
            ml["sand"] = SargassumDetector("models/best.pt")
            print("[models]   sargassum model loaded")
        except Exception as e:
            print(f"[models]   sargassum model FAILED: {e}")

    if "water" not in ml:
        try:
            ml["water"] = WaterSeverityClassifier("models/water_best.pt")
            print("[models]   water model loaded (beta)")
        except Exception as e:
            print(f"[models]   water model FAILED: {e}")

    if "crowd" not in ml:
        try:
            # Path, not bare "yolo26m.pt" — a bare name makes Ultralytics
            # DOWNLOAD the weights on every cold container start.
            ml["crowd"] = CrowdCounter(model_path="models/yolo26m.pt")
            print("[models]   crowd model loaded (beta)")
        except Exception as e:
            print(f"[models]   crowd model FAILED: {e}")

    if "summarizer" not in ml:
        try:
            ml["summarizer"] = summarize.load_summarizer()
            print("[models]   summarizer loaded")
        except Exception as e:
            # Entirely optional: without it, summaries fall back to the
            # deterministic template, which is accurate either way.
            print(f"[models]   summarizer unavailable ({e}) — using templates")

    return ml


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- STARTUP ----
    Path("data").mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    load_models()

    yield   # ← server runs here, handling requests

    # ---- SHUTDOWN ----
    ml.clear()
    print("[shutdown] models released")


app = FastAPI(title="Sargassum Beach API", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────────────────
# Browsers block a page on one origin (scheme+host+port) from reading
# responses from another origin unless the server explicitly allows it.
# Your React dev server is :5173, this API is :8000 — DIFFERENT origins.
#
# This is why /docs and curl work fine while React mysteriously fails: the
# restriction is enforced by the BROWSER, not the server. Without this
# middleware you WILL see "blocked by CORS policy" the first time you set
# MOCK = false in api.js.
#
# Origins are listed explicitly rather than "*" — a habit worth keeping.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",     # Vite dev server
        # TODO: add the deployed frontend URL (Vercel) when you ship
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────
# STATIC MEDIA — cam frames and their overlays
# ─────────────────────────────────────────────────────────────────────────
# The detail page's cam feed needs the stored frame plus its two overlay PNGs.
# Serving them straight off disk keeps the JSON payload free of image data.
#
# Note this needs NO CORS entry: a plain <img src> is not a cross-origin READ,
# so the browser never applies the same-origin check it applies to fetch().
# (It would matter if the frontend drew these into a canvas and read pixels
# back — it doesn't.)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=UPLOAD_DIR), name="media")


def _first_not_none(*values):
    """First value that is not None — unlike `a or b`, this keeps a real 0.

    Exists because measurements legitimately hit zero (UV after sunset, wind on
    a still day, 0% rain) and `or` cannot tell those apart from "no data".
    """
    for v in values:
        if v is not None:
            return v
    return None


def path_to_media_url(path: Optional[str]) -> Optional[str]:
    """Filesystem path under data/uploads -> the /media URL that serves it.

    None-safe on purpose: every caller is a field that is legitimately absent
    (beach never ingested, model failed, camera unsupported).
    """
    if not path:
        return None
    return f"/media/{Path(path).name}"


# ─────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS — the API contract
# ─────────────────────────────────────────────────────────────────────────
# These are the single source of truth for the shape of your API. Two payoffs:
#   1. /docs auto-generates the exact schema — this page IS the spec you hand
#      to whoever (or whatever) builds the frontend.
#   2. If your code returns a malformed beach, FastAPI errors LOUDLY here
#      instead of shipping broken JSON that makes the React map crash later.
#
# Keep these mirrored with frontend/src/api.js's MOCK data — that's what makes
# flipping MOCK = false a non-event.

class SubScores(BaseModel):
    """0-10 sub-scores. MUST mirror the keys in scoring.py's WEIGHTS dict —
    all six of them. None means that signal was unavailable for this beach
    (beta camera, failed API call, missing sensor), and scoring.py's
    aggregate renormalizes the remaining weights around it."""
    weather: Optional[float]
    sargassum: Optional[float]    # None on beta beaches (Jupiter, Boca)
    rip: Optional[float]
    water: Optional[float]        # beta model
    crowd: Optional[float]        # beta model
    uv: Optional[float]


class BeachSummary(BaseModel):
    """One beach as the frontend sees it. Matches the BeachCard props."""
    id: str
    name: str
    lat: float
    lon: float
    index: Optional[float]
    subscores: SubScores
    temp_f: Optional[float]
    water_temp_f: Optional[float]
    sargassum_label: Optional[str]
    crowd_label: Optional[str]
    supported: bool
    short_forecast: Optional[str]
    updated_at: Optional[str]
    # Tide is fetched by sources.py but wasn't being surfaced — these two
    # fields close that gap. Tide doesn't feed the INDEX (it's informational,
    # not a quality signal), it just displays on the card.
    next_tide: Optional[str] = None            # e.g. "High at 21:56"
    next_tide_height_ft: Optional[float] = None

    # ---- RAW MEASUREMENTS ----------------------------------------------
    # Everything below was ALREADY being fetched and fed into scoring.py, then
    # thrown away before the response. The sub-scores are not a substitute for
    # them, because scoring is LOSSY and in places non-invertible.
    #
    # uv_index is the clearest example, and the reason this block exists. The
    # UV INDEX and the UV SUB-SCORE are different quantities pointing in
    # OPPOSITE directions:
    #
    #     uv_index  : EPA scale, 0 to 11+, HIGHER IS WORSE (a measurement)
    #     subscores.uv : 0-10,             HIGHER IS BETTER (a quality term)
    #
    # scoring.uv_score() clips at both ends, so every index <= 3 collapses to a
    # sub-score of 10 and cannot be recovered. The frontend used to invert the
    # sub-score to guess the index; it must read this field instead.
    #
    # These are all display-only. None of them feeds the index — that already
    # happened upstream in scoring.py.
    uv_index: Optional[float] = None       # EPA scale, higher = worse
    rip_risk: Optional[str] = None         # "low" | "moderate" | "high"
    coverage_pct: Optional[float] = None   # sargassum %, supported cameras only
    crowd_count: Optional[int] = None      # people detected in the frame
    humidity_pct: Optional[float] = None
    precip_prob: Optional[float] = None    # 0-100
    wind_mph: Optional[float] = None

    # ---- CAM FEED ------------------------------------------------------
    # URLs (not filesystem paths) for the stored frame and its overlays. Both
    # overlays are transparent PNGs at the frame's own dimensions, so the
    # frontend stacks them as plain <img> layers with no scaling maths.
    # None whenever the beach has no camera, has never been ingested, or the
    # relevant model failed on the last frame.
    frame_url: Optional[str] = None
    sargassum_mask_url: Optional[str] = None
    crowd_overlay_url: Optional[str] = None

    # One-paragraph plain-language read of the conditions, generated locally.
    # None if the summariser is unavailable — never blocks the rest of the page.
    plain_summary: Optional[str] = None

class IngestResult(BaseModel):
    beach: str
    coverage_pct: Optional[float]
    sargassum_label: Optional[str]
    crowd_count: Optional[int]
    water_severity: Optional[str]
    stored_at: str


# ─────────────────────────────────────────────────────────────────────────
# STORAGE — SQLite
# ─────────────────────────────────────────────────────────────────────────
# DESIGN CHOICE: SQLite over an in-memory dict.
#
# The in-memory version breaks in a specific, easy-to-miss way: your Dockerfile
# runs `uvicorn --workers 2`, and each worker process gets its OWN copy of a
# module-level dict. An /ingest handled by worker 1 would be invisible to a
# /beaches served by worker 2. SQLite is a shared file, so all workers see it.
#
# It also stores HISTORY, not just "latest" — which the forecast timeline
# scrubber needs. A dict would only ever hold the current value.
#
# UPGRADE PATH: the schema below maps 1:1 onto Postgres. When you outgrow a
# single file (concurrent writes, hosted deployment with ephemeral disk),
# swap the connection and the queries barely change.

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row    # rows behave like dicts, not tuples
    return conn


def init_db():
    """Create the readings table if it doesn't exist. Called once at startup."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                beach_id      TEXT NOT NULL,
                taken_at      TEXT NOT NULL,      -- ISO8601 UTC
                coverage_pct  REAL,
                sarg_score    REAL,
                sarg_label    TEXT,
                crowd_count   INTEGER,
                crowd_tier    TEXT,
                water_severity TEXT,
                image_path    TEXT,
                source_stem   TEXT,
                mask_path     TEXT,
                crowd_overlay_path TEXT,
                -- The blended index AT THE MOMENT OF INGEST. Without this
                -- there is no index history at all: the index is otherwise
                -- only ever computed live in assemble_beach(), so nothing
                -- persists it and a timeline chart has nothing to plot.
                index_at_ingest       REAL
            )
        """)
        # Index on (beach_id, taken_at) because EVERY read is "latest reading
        # for beach X" or "readings for beach X over a time range" — without
        # it, those become full table scans as history accumulates.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_beach_time
            ON readings (beach_id, taken_at DESC)
        """)

        # MIGRATION. `CREATE TABLE IF NOT EXISTS` is a no-op on a database that
        # already exists, so it will NOT add source_stem to a table created
        # before that column was introduced — the CREATE above only helps fresh
        # installs. Existing databases need an explicit ALTER, or poller.py's
        # dedup query dies with "no such column".
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(readings)")}
        for col, decl in (
            ("source_stem", "TEXT"),
            ("mask_path", "TEXT"),
            ("crowd_overlay_path", "TEXT"),
            ("index_at_ingest", "REAL"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {decl}")
                print(f"[db] migrated: added readings.{col}")

        # Cached LLM summaries. Keyed by beach with a fingerprint of the inputs
        # the text was generated from, so a summary is only regenerated when
        # the underlying readings actually change — not on every page load.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                beach_id     TEXT PRIMARY KEY,
                fingerprint  TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                generated_at TEXT NOT NULL
            )
        """)


def save_reading(beach_id: str, result: dict):
    """Persist one ML run's output."""
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO readings
              (beach_id, taken_at, coverage_pct, sarg_score, sarg_label,
               crowd_count, crowd_tier, water_severity, image_path, source_stem,
               mask_path, crowd_overlay_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            beach_id,
            result["taken_at"],
            result.get("coverage_pct"),
            result.get("sarg_score"),
            result.get("sarg_label"),
            result.get("crowd_count"),
            result.get("crowd_tier"),
            result.get("water_severity"),
            result.get("image_path"),
            # NULL for manual uploads — only the poller knows a cam frame id.
            result.get("source_stem"),
            result.get("mask_path"),
            result.get("crowd_overlay_path"),
        ))
        # Returned so the caller can snapshot the index onto this exact row.
        return cur.lastrowid


def cached_summary(beach_id: str, beach: dict) -> Optional[str]:
    """The plain-language paragraph for this beach, generating only if needed.

    Generation costs seconds of CPU, but the inputs change at most hourly (new
    ingest) or every ~15 minutes (weather refresh). Regenerating per page load
    would burn that cost repeatedly to produce byte-identical text, so the
    result is keyed to a fingerprint of the inputs: same inputs, cached text;
    changed inputs, regenerate once.
    """
    fp = summarize.fingerprint(beach)
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT fingerprint, summary_text FROM summaries WHERE beach_id = ?",
                (beach_id,)).fetchone()
            if row and row["fingerprint"] == fp:
                return row["summary_text"]
    except Exception as e:
        print(f"[summary] cache read failed for {beach_id}: {e}")

    text = (summarize.generate_summary(beach, ml["summarizer"])
            if "summarizer" in ml else summarize.fallback_summary(beach))
    if not text:
        return None

    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO summaries (beach_id, fingerprint, summary_text, generated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(beach_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    summary_text = excluded.summary_text,
                    generated_at = excluded.generated_at
            """, (beach_id, fp, text, datetime.now(timezone.utc).isoformat()))
    except Exception as e:
        print(f"[summary] cache write failed for {beach_id}: {e}")
    return text


def latest_reading(beach_id: str) -> Optional[dict]:
    """Most recent ML reading for a beach, or None if never ingested."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM readings
            WHERE beach_id = ?
            ORDER BY taken_at DESC
            LIMIT 1
        """, (beach_id,)).fetchone()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────
# ASSEMBLY — where the three modules actually meet
# ─────────────────────────────────────────────────────────────────────────

async def assemble_beach(beach_id: str) -> BeachSummary:
    """Build one beach's full summary: live external data + stored ML output
    -> scoring.py -> API response shape.

    This function IS the hybrid design in code:
      - `fetch_beach_conditions` = live weather/tide/UV (cached ~15min)
      - `latest_reading`         = stored sargassum/crowd/water from last ingest
      - `BeachScorer.get_score`  = combines both into the index
    """
    cfg = BEACHES[beach_id]

    conditions = await fetch_beach_conditions(beach_id)   # live
    reading = latest_reading(beach_id)                     # stored

    weather = conditions.get("weather")
    surf = conditions.get("surf") or {}

    # Only trust the sargassum model on cameras it's validated for. A stored
    # reading might exist for a beta beach (if you ingested a frame there to
    # test), but we deliberately don't surface it as a real signal.
    supported = beach_id in SARGASSUM_SUPPORTED
    coverage = reading.get("coverage_pct") if (reading and supported) else None
    crowd_count = reading.get("crowd_count") if reading else None

    # MEASURED vs ASSUMED — these two locals are deliberately allowed to be
    # None. UV: EPA is authoritative (MFL's SRF omits it); fall back to the SRF
    # value in case you later add an office that DOES publish it.
    #
    # The scorer needs a number and substitutes DEFAULT_* when a source is
    # down, but the RESPONSE must not: reporting the fallback as though it were
    # a reading would show the user a confident "UV Index 6" that nobody
    # measured. Display gets None (renders as "—"), scoring gets the default.
    # FALSY-ZERO TRAP. This must be an explicit None check, never `a or b`.
    #
    # After sunset the EPA genuinely reports a UV index of 0 — a real, correct
    # measurement. But `0 or None` evaluates to None in Python, so the reading
    # was thrown away as "missing": the page showed an em dash, and the scorer
    # substituted DEFAULT_UV (6.0), scoring a pitch-dark beach as if the sun
    # were halfway up. UV 0 should score 10.
    uv_measured = _first_not_none(conditions.get("uv_index"), surf.get("uv_index"))
    rip_measured = surf.get("rip_risk")

    scorer = BeachScorer(
        name=cfg["name"],
        # Weather fields: if the API failed, pass neutral values rather than
        # crashing. The index will be less meaningful, but the app still works.
        temp_f=weather.temp_f if weather else 80.0,
        humidity_pct=weather.humidity_pct if weather else 60.0,
        precip_prob=weather.precip_prob if weather else 0.0,
        wind_mph=weather.wind_mph if weather else 0.0,
        # Again explicit: `uv_measured or DEFAULT_UV` would turn a real 0 into 6.
        uv_index=DEFAULT_UV if uv_measured is None else uv_measured,
        rip_risk=rip_measured or DEFAULT_RIP_RISK,   # a str, so `or` is safe here
        # ML signals — None if never ingested or beach unsupported
        sargassum_coverage_pct=coverage,
        crowd_count=crowd_count,
        water_severity=reading.get("water_severity") if reading else None,
    )

    scored = scorer.get_score()

    # Pull the next high/low from the tide data. TideData.next_event() handles
    # "which event is upcoming" — that logic lives on the dataclass in
    # sources.py because it's a property of the tide data, not a score.
    tides = conditions.get("tides")
    nxt = tides.next_event() if tides else None

    summary = BeachSummary(
        id=beach_id,
        name=cfg["name"],
        lat=cfg["lat"],
        lon=cfg["lon"],
        index=scored["index"],
        subscores=SubScores(**scored["subscores"]),
        temp_f=weather.temp_f if weather else None,
        water_temp_f=conditions.get("water_temp_f"),
        sargassum_label=scored["sargassum_label"],
        crowd_label=scored["crowd_label"],
        supported=supported,
        short_forecast=weather.short_forecast if weather else None,
        updated_at=reading["taken_at"] if reading else None,
        next_tide=(f"{'High' if nxt.kind == 'H' else 'Low'} at {nxt.time:%H:%M}"
                   if nxt else None),
        next_tide_height_ft=nxt.height_ft if nxt else None,

        # Raw measurements for the detail page. See the BeachSummary docstring
        # for why the UV index cannot be derived from subscores.uv.
        uv_index=uv_measured,
        rip_risk=rip_measured,
        coverage_pct=coverage,
        crowd_count=crowd_count,
        humidity_pct=weather.humidity_pct if weather else None,
        precip_prob=weather.precip_prob if weather else None,
        wind_mph=weather.wind_mph if weather else None,

        # Cam feed. The sargassum overlay is gated on `supported` for the same
        # reason coverage_pct is: a mask may exist on a beta beach from a test
        # ingest, but showing it would imply a signal we don't trust there.
        frame_url=path_to_media_url(reading.get("image_path") if reading else None),
        sargassum_mask_url=path_to_media_url(
            reading.get("mask_path") if (reading and supported) else None),
        crowd_overlay_url=path_to_media_url(
            reading.get("crowd_overlay_path") if reading else None),
    )

    # Generated last, from the finished summary, so the paragraph describes
    # exactly what the rest of the page shows.
    summary.plain_summary = cached_summary(beach_id, summary.model_dump())
    return summary

# ─────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    """Health check. Also reports which models actually loaded — genuinely
    useful in deployment, where a missing weights file otherwise fails
    silently until the first ingest."""
    return {
        "status": "ok",
        "models_loaded": sorted(ml.keys()),
        "beaches": list(BEACHES.keys()),
    }


@app.get("/beaches", response_model=list[BeachSummary])
async def list_beaches():
    """All beaches with current index — backs the Home page cards + map.

    async def + await because assemble_beach calls into sources.py, which is
    async all the way down. Calling an async function WITHOUT await returns a
    coroutine object rather than data — a confusing failure that looks like
    'my endpoint returns gibberish'.
    """
    # DESIGN CHOICE: sequential rather than asyncio.gather across beaches.
    # sources.py ALREADY parallelizes the 5 API calls within each beach, and
    # its 15-min cache means beaches 2-4 mostly hit warm data anyway. Adding a
    # second layer of concurrency here would multiply outbound requests to NWS
    # for little gain — and being a polite API citizen matters when you're
    # using a free government service with no key.
    return [await assemble_beach(bid) for bid in BEACHES]


@app.get("/beach/{beach_id}", response_model=BeachSummary)
async def get_beach(beach_id: str):
    """One beach's detail — backs the /beach/:id React route."""
    if beach_id not in BEACHES:
        # 404 with a proper status code, NOT {"error": ...} with a 200.
        # Your frontend's fetch() checks response.ok, which relies on status.
        raise HTTPException(status_code=404,
                            detail=f"Unknown beach '{beach_id}'")
    return await assemble_beach(beach_id)


class IngestError(ValueError):
    """Raised by run_ingest for input the caller got wrong (e.g. bytes that
    aren't a decodable image). The HTTP layer maps it to a 400; poller.py just
    logs it. Kept distinct from unexpected exceptions so a genuine bug doesn't
    get reported to the user as "bad image"."""


def run_ingest(beach_id: str, raw: bytes,
               source_stem: Optional[str] = None) -> dict:
    """Run all three models over one frame and store the result. Returns the
    `result` dict that was persisted.

    THE ONLY PLACE THE ML MODELS ARE INVOKED. Both entry points — the manual
    /ingest upload and poller.py's hourly scrape — call this, which is what
    keeps "how the image arrived" from leaking into model or scoring code.

    `source_stem` identifies the frame at its source (the cam's filename stem)
    so the poller can tell "already processed this one" from "genuinely new".
    None for manual uploads, which have no such identifier.

    Raises IngestError if `raw` isn't a decodable image. Individual MODEL
    failures are swallowed and logged, not raised: a crowd counter that blows
    up shouldn't discard a perfectly good sargassum reading from the same frame.
    """
    # imdecode returns None (not an exception) on non-image bytes — check it,
    # or you get a confusing crash deep inside the model instead of a clear 400.
    bgr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise IngestError("Could not decode image")

    # NORMALIZE RESOLUTION — this is a correctness step, not an optimization.
    #
    # CrowdCounter slices with SAHI at 512px, so the tile count (and therefore
    # how large a person appears WITHIN a tile) scales with the input. Feeding
    # different sizes makes crowd_count incomparable, and the whole product
    # ranks beaches against each other.
    #
    # The cameras really do disagree: Lake Worth's published frame is 1920x1280
    # (~20 tiles) while Boynton's is 1200x800 (~6), and the full-resolution
    # originals are 5184x3456 (~117 tiles — minutes on CPU). A manual upload
    # could be any of those.
    #
    # Normalizing HERE rather than in poller.py means every reading is measured
    # at one scale no matter how the image arrived. Only ever downscales, so a
    # small frame is never upsampled into false detail.
    h, w = bgr.shape[:2]
    if w > INGEST_WIDTH:
        scale = INGEST_WIDTH / w
        bgr = cv2.resize(bgr, (INGEST_WIDTH, round(h * scale)),
                         interpolation=cv2.INTER_AREA)   # INTER_AREA: correct for downscaling
        print(f"[ingest] normalized {w}x{h} -> {bgr.shape[1]}x{bgr.shape[0]}")

    # cv2 gives BGR; every model expects RGB. Convert ONCE here at the
    # boundary rather than inside each predict() — same discipline as training.
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    taken_at = datetime.now(timezone.utc).isoformat()
    # Timestamped filename so uploads never overwrite each other and the
    # archive stays aligned with the readings table.
    stamp = taken_at.replace(":", "").replace("-", "")[:15]
    image_path = str(UPLOAD_DIR / f"{beach_id}_{stamp}.jpg")
    cv2.imwrite(image_path, bgr)   # save BGR — imwrite expects it

    result = {"taken_at": taken_at, "image_path": image_path,
              "source_stem": source_stem}

    # Each model in its own try/except: crowd counting failing shouldn't
    # discard a perfectly good sargassum reading from the same frame.
    if "sand" in ml and beach_id in SARGASSUM_SUPPORTED:
        try:
            mask_path = str(UPLOAD_DIR / f"{beach_id}_{stamp}_mask.png")
            out = ml["sand"].predict(rgb, save_mask_to=mask_path)
            result["coverage_pct"] = out["coverage_pct"]
            result["sarg_score"] = out["score"]
            result["sarg_label"] = out["label"]
            # BUG FIX: the overlay PNG was being written to disk on every
            # ingest and then orphaned — the returned path was never copied
            # into `result`, so it never reached save_reading and the cam feed
            # had no way to find it.
            result["mask_path"] = out["mask_path"]
        except Exception as e:
            print(f"[ingest] sargassum failed: {e}")

    if "water" in ml:
        try:
            result["water_severity"] = ml["water"].predict(rgb)["severity"]
        except Exception as e:
            print(f"[ingest] water failed: {e}")

    if "crowd" in ml:
        try:
            # Takes a PATH, not an array — SAHI reads the file itself, which
            # is why we saved the image above before this call.
            boxes_path = str(UPLOAD_DIR / f"{beach_id}_{stamp}_boxes.png")
            out = ml["crowd"].count(image_path, save_boxes_to=boxes_path)
            result["crowd_count"] = out["people_count"]
            result["crowd_tier"] = out["tier"]
            result["crowd_overlay_path"] = out["boxes_path"]
        except Exception as e:
            print(f"[ingest] crowd failed: {e}")

    result["reading_id"] = save_reading(beach_id, result)
    return result


async def snapshot_index(beach_id: str, reading_id: int) -> Optional[float]:
    """Record the blended index onto the reading row just stored.

    WHY THIS EXISTS. The index is a blend of live weather and the latest stored
    ML reading, and it was only ever computed inside assemble_beach() to answer
    a request — nothing wrote it down. So /history could return coverage and
    crowd counts, but never the number the whole dashboard is built around, and
    a timeline chart had nothing to plot.

    Called right after a successful ingest so each stored reading carries the
    index as it stood at that moment. Best-effort: a failure here must not
    undo an ingest that already succeeded, so the reading simply keeps a NULL
    index and the chart skips that point.
    """
    try:
        summary = await assemble_beach(beach_id)
        if summary.index is None:
            return None
        # Store the NUMBER only, never a band label. The 0-10 -> label/color
        # thresholds live in exactly one place (frontend/src/score.js) and the
        # frontend derives the label from this number. Persisting a label here
        # would fork that table into Python, where it would silently drift the
        # next time the bands are retuned.
        with get_db() as conn:
            conn.execute(
                "UPDATE readings SET index_at_ingest = ? WHERE id = ?",
                (summary.index, reading_id),
            )
        return summary.index
    except Exception as e:
        print(f"[ingest] index snapshot failed for {beach_id}: {e}")
        return None


@app.post("/ingest/{beach_id}", response_model=IngestResult)
async def ingest(beach_id: str, file: UploadFile = File(...)):
    """Upload a beach cam frame -> run all three models -> store results.

    A THIN WRAPPER over run_ingest(). This function owns only the HTTP concerns
    — routing, reading the upload, mapping errors to status codes. All the
    actual work lives in run_ingest so poller.py can reuse it verbatim.

    SYNCHRONOUS (Option A): the client waits for inference to finish.
    CrowdCounter's sliced inference takes SEVERAL SECONDS, so this endpoint is
    genuinely slow. That's an accepted tradeoff because:
      - you are the only uploader, roughly once a day
      - you see failures immediately instead of hunting through logs
    ALTERNATIVE when this becomes user-facing: FastAPI's BackgroundTasks —
    return 202 Accepted straight away and process afterwards.
    """
    if beach_id not in BEACHES:
        raise HTTPException(status_code=404, detail=f"Unknown beach '{beach_id}'")

    raw = await file.read()   # await: reading an upload is I/O

    try:
        result = run_ingest(beach_id, raw)
        # Same snapshot the poller takes — a manually uploaded frame should
        # appear on the forecast timeline exactly like a scraped one.
        await snapshot_index(beach_id, result["reading_id"])
    except IngestError as e:
        # Caller's fault (not an image) -> 400, with the reason intact. The
        # upload page prints this text verbatim.
        raise HTTPException(status_code=400, detail=str(e))

    return IngestResult(
        beach=beach_id,
        coverage_pct=result.get("coverage_pct"),
        sargassum_label=result.get("sarg_label"),
        crowd_count=result.get("crowd_count"),
        water_severity=result.get("water_severity"),
        stored_at=result["taken_at"],
    )


@app.post("/poll/{beach_id}")
async def poll_now(beach_id: str):
    """Scrape the newest cam frame for one beach, right now.

    WHY THIS EXISTS: the hourly scrape runs under launchd, and a broken
    scheduler is INVISIBLE from the dashboard — every endpoint keeps happily
    serving the last stored reading, so a stalled poller looks exactly like a
    working one until you notice the cam feed no longer matches the real
    beach. (It happened: the project directory was renamed, launchd kept
    reporting exit 78 to nobody, and the page served a 14-hour-old frame as
    though it were current.) This gives the UI a recovery path that isn't a
    terminal.

    Delegates to poller.poll_beach with NO changes to it, so a manual refresh
    and a cron refresh scrape, dedup, ingest and snapshot along the exact same
    code path. Two implementations of "get the newest frame" would drift.

    force=True is hardcoded rather than exposed as a query parameter: a person
    pressing a refresh link has explicitly asked for a scrape, so neither the
    daylight-window guard nor the "same stem as last time" skip should turn it
    into a silent no-op.

    SYNCHRONOUS, 10-15s, for the same reason /ingest is — see that docstring.

    Returns poll_beach's verdict, with ONE substitution:
      "ingested"  — a genuinely newer frame was stored
      "unchanged" — the scrape worked, but the camera has published nothing
                    since the last reading
      "failed"    — the scrape or the models failed; the previous reading is
                    untouched, which is the whole degrade-to-stale contract

    "unchanged" exists because force=True bypasses poll_beach's own dedup, so
    it reports "ingested" for a re-run of the SAME frame. That is true but
    useless to a caller: "your refresh worked and there is nothing new" and
    "your refresh worked and here is a new frame" have to be distinguishable,
    or the UI has to lie about one of them. Comparing source_stem across the
    call is the honest way to tell them apart without touching poll_beach.

    poll_beach never raises, so there is nothing to catch here.
    """
    # LOCAL IMPORT, DELIBERATELY. poller.py imports run_ingest/get_db/
    # snapshot_index from this module at import time, so `import poller` at the
    # top of main.py is a circular import that fails at startup.
    from poller import CAMS, USER_AGENT, HTTP_TIMEOUT, poll_beach

    if beach_id not in CAMS:
        # Not a transient failure — Jupiter and Boca have no camera at all.
        raise HTTPException(
            status_code=404,
            detail=f"No camera to poll for '{beach_id}' — cameras: {sorted(CAMS)}",
        )

    before = latest_reading(beach_id)
    before_stem = before.get("source_stem") if before else None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                 headers={"User-Agent": USER_AGENT},
                                 follow_redirects=True) as client:
        status = await poll_beach(client, beach_id, force=True)

    reading = latest_reading(beach_id)
    stem = reading.get("source_stem") if reading else None
    if status == "ingested" and stem is not None and stem == before_stem:
        status = "unchanged"

    return {
        "beach_id": beach_id,
        "status": status,
        # Post-poll timestamp, so the client can confirm the frame actually
        # moved rather than trusting the status string alone.
        "updated_at": reading["taken_at"] if reading else None,
        "source_stem": stem,
    }


@app.get("/history/{beach_id}")
def get_history(beach_id: str, limit: int = 30):
    """Past readings — backs the forecast timeline scrubber.

    Plain `def`, not `async def`: this only touches SQLite (which is sync),
    never sources.py. FastAPI runs sync endpoints in a threadpool, so this
    doesn't block the event loop.

    `limit` is a QUERY parameter (not in the path), so it arrives as
    /history/lake-worth?limit=50 and defaults to 30 when omitted.
    """
    if beach_id not in BEACHES:
        raise HTTPException(status_code=404, detail=f"Unknown beach '{beach_id}'")

    with get_db() as conn:
        rows = conn.execute("""
            SELECT taken_at, coverage_pct, sarg_label, crowd_count, crowd_tier,
                   water_severity, index_at_ingest
            FROM readings WHERE beach_id = ?
            ORDER BY taken_at DESC LIMIT ?
        """, (beach_id, limit)).fetchall()

    # Oldest-first: a timeline chart reads left to right, and reversing here
    # means every consumer doesn't have to. The DESC + LIMIT above still gets
    # the most RECENT n rows, which is the point of the query.
    return {"beach_id": beach_id,
            "readings": [dict(r) for r in reversed(rows)]}