"""
sources.py — External data fetchers (NWS weather, NOAA tides & water temp)

ARCHITECTURE NOTE
-----------------
This module's ONLY job is: talk to external APIs, parse their responses into
plain Python dataclasses, and hand them off. It knows nothing about scoring
formulas (scoring.py) or HTTP routing (main.py).

Why that separation matters: if NWS changes their JSON shape, or you swap to
a different weather provider entirely, ONLY this file changes. scoring.py
keeps consuming the same `WeatherData` dataclass. That boundary is the whole
point of having a `sources.py` at all.

GRACEFUL DEGRADATION
--------------------
Every fetcher returns `None` on failure rather than raising. This is deliberate
and pairs with scoring.py's weight renormalization: if the tide API is down,
the beach still gets an index computed from whatever signals DID arrive,
instead of the whole endpoint 500-ing. A beach app that shows partial data
beats one that shows an error page.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
import re


import httpx


# All four beaches are in Palm Beach County. NOAA returns tide times in LOCAL
# time (time_zone=lst_ldt) and EPA returns UV hours in LOCAL time, so "local"
# needs to be a real timezone rather than whatever the server happens to be set
# to — otherwise deploying this anywhere but Eastern silently shifts every
# tide time and UV lookup.
BEACH_TZ = ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

# NWS REQUIRES a User-Agent identifying your app. There's no API key — this
# string IS your identification. Include contact info so they can reach you
# if your requests ever cause a problem. (this may become a real
# API key in the future, so keep it easy to change.)
NWS_USER_AGENT = "(sargassum-beach-app, jerryluo457@gmail.com)"  

NWS_BASE = "https://api.weather.gov" #NWS API
NOAA_BASE = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

# Per-beach static config. Tide station IDs are NOAA CO-OPS station IDs.
#
# ⚠️ VERIFY THESE STATION IDs before trusting the output. Look them up at
# https://tidesandcurrents.noaa.gov/  (map view) and confirm each station
# actually reports `predictions`. Some nearby stations are "subordinate"
# stations that ONLY support high/low interval (which is all we request
# here, so that's fine) — but not every station supports water_temperature.
# WHY SEVERAL BEACHES SHARE A TIDE STATION — this is deliberate, not laziness.
#
# Surveyed every CO-OPS tide-prediction station within ~7 miles of each beach.
# Almost all of them sit on the INTRACOASTAL WATERWAY, behind the barrier
# island. ICWW tide lags the open ocean and has a much smaller range: measured
# live, Ocean Ridge ICWW read low tide at 23:59 while the ocean pier read 21:54
# — two hours apart. These beaches face the ocean, so an ICWW station would
# produce numbers that LOOK more local while describing the wrong body of water.
#
# Ocean-coupled stations on this stretch, with their measured low tide on the
# evening this was verified (open ocean = 21:54):
#     8722670  Lake Worth Pier (ocean)          21:54   reference
#     8722495  Jupiter Inlet, south jetty       21:55   +1 min
#     8722588  Port of Palm Beach (in inlet)    22:06   +12 min
# versus the Intracoastal stations nearby:
#     8722718  Ocean Ridge ICWW                 23:59   +2h05
#     8722802  Lake Wyman ICWW                  00:08   +2h14
#
# So ocean beaches showing near-identical TIMES is physically correct — the
# tide wave arrives along a straight coastline almost simultaneously. HEIGHTS
# still differ (0.62 / 0.81 / 0.66 ft), which is why each beach uses the
# closest ocean-coupled station rather than one shared value.
#
# Boynton and Boca keep the Lake Worth pier as an ocean proxy: every station
# closer to them lags by ~2 hours (ICWW), so the more distant ocean station is
# the more accurate choice for surf conditions.
#
# If you compare against tidesandcurrents.noaa.gov, CHECK THE STATION ID. The
# ICW and ocean stations have confusingly similar names (8722669 "Lake Worth
# ICW" vs 8722670 "Lake Worth Pier"), and they differ by about 90 minutes.
#
# temp_station is SEPARATE because water temperature is a physical sensor, not
# a prediction, and 8722670 is the only station here that has one. Verified:
# 8722495 / 8722718 / 8722802 / 8722816 all return
# "No data ... not offered at this station" for water_temperature.
BEACHES = {
    "lake-worth": {
        "name": "Lake Worth Inlet",
        "lat": 26.767776, "lon": -80.035963,
        # Port of Palm Beach, 0.9 mi — inside Lake Worth Inlet itself.
        # BEWARE THE NAMES HERE. "Lake Worth Pier" (8722670) is NOT at Lake
        # Worth Inlet: it is in the city of Lake Worth Beach, 10.7 mi south.
        # This beach was pointed at it purely because of the shared name.
        # 8722588 lags the open ocean by only 12 minutes (22:06 vs 21:54
        # measured), so it stays surf-representative while actually being local.
        "tide_station": "8722588",
        "temp_station": "8722670",     # 8722588 has no thermometer
        "zip_code": "33404",
    },
    "boynton": {
        "name": "Boynton Inlet",
        "lat": 26.543465, "lon": -80.043413,
        "tide_station": "8722670",     # nearest OCEAN station (4.8 mi); all
                                       # closer options are ICWW
        "temp_station": "8722670",
        "zip_code": "33435",
    },
    "jupiter": {
        "name": "Jupiter Inlet",
        "lat": 26.941915, "lon": -80.071898,
        "tide_station": "8722495",     # Jupiter Inlet, south jetty — ocean,
                                       # 0.1 mi. Its own station at last.
        "temp_station": "8722670",     # 8722495 has no temperature sensor
        "zip_code": "33469",
    },
    "boca": {
        "name": "Boca Raton — Spanish River Park",
        "lat": 26.379405, "lon": -80.067170,
        "tide_station": "8722670",     # every Boca-area station is ICWW/lake
        "temp_station": "8722670",
        "zip_code": "33431",
    },
}


# ─────────────────────────────────────────────────────────────────────────
# DATA SHAPES
# ─────────────────────────────────────────────────────────────────────────
# These dataclasses are the CONTRACT between sources.py and scoring.py.
# scoring.py's Beach() takes exactly these fields. If you add a field here,
# you're deciding to add a signal to the product — a design decision, not
# just plumbing.

@dataclass
class WeatherData:
    temp_f: float
    humidity_pct: float
    precip_prob: float          # 0–100
    wind_mph: float
    short_forecast: str         # e.g. "Partly Sunny" — nice for the LLM summary layer


@dataclass
class TideEvent:
    time: datetime
    height_ft: float
    kind: str                   # "H" (high) or "L" (low)


@dataclass
class TideData:
    events: list[TideEvent]

    def next_event(self, now: Optional[datetime] = None) -> Optional[TideEvent]:
        """The next upcoming high/low — this is what the UI card actually shows
        ("High tide in 2h"). Kept as a method on the dataclass rather than in
        scoring.py because it's a property OF the tide data, not a score.

        `now` defaults to real UTC now. Events carry BEACH_TZ, so the
        comparison is between two aware datetimes and Python handles the
        offset — which is exactly what the old timezone.utc mislabelling
        broke."""
        now = now or datetime.now(timezone.utc)
        upcoming = [e for e in self.events if e.time > now]
        return min(upcoming, key=lambda e: e.time) if upcoming else None


# ─────────────────────────────────────────────────────────────────────────
# CACHING
# ─────────────────────────────────────────────────────────────────────────
# DESIGN CHOICE: a plain in-memory dict with TTLs. Simple, zero dependencies,
# resets on restart (which is fine — it just re-fetches).
#
# WHY CACHE AT ALL: the /points → grid lookup is effectively static per beach,
# and re-requesting it on every page load wastes NWS's rate limit for no
# benefit. Forecasts update roughly hourly, so a 15-min TTL is plenty fresh.
#
# ALTERNATIVES when you outgrow this: Redis (survives restarts, shared across
# multiple server workers — matters once you run uvicorn with --workers > 1,
# since each worker has its OWN copy of this dict), or functools.lru_cache
# for the pure-static grid lookup only.
_cache: dict[str, tuple[datetime, object]] = {}

GRID_TTL = timedelta(days=7)      # grid coords rarely change, but DO re-check occasionally
FORECAST_TTL = timedelta(minutes=15)
TIDE_TTL = timedelta(hours=6)     # tide predictions are astronomical — very stable


def _cache_get(key: str, ttl: timedelta):
    """Return cached value if it exists and hasn't expired, else None."""
    hit = _cache.get(key)
    if hit is None:
        return None
    stored_at, value = hit
    if datetime.now(timezone.utc) - stored_at > ttl:
        return None
    return value


def _cache_set(key: str, value):
    _cache[key] = (datetime.now(timezone.utc), value)


# ─────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────

def _parse_wind_mph(wind_speed: str) -> float:
    """NWS returns windSpeed as a HUMAN STRING like "10 mph" or "5 to 10 mph",
    not a number. This is the kind of real-world API messiness worth knowing
    about: you can't just float() it.

    We take the HIGHER number in a range — design choice: for a comfort score,
    the gustier end is what you'd actually feel on the beach.

    ALTERNATIVE: enable NWS's `forecast_wind_speed_qv` feature flag (via a
    Feature-Flags header) to get a structured QuantitativeValue instead of
    parsing prose. Cleaner, but ties you to a flag that may change defaults.
    """
    if not wind_speed:
        return 0.0
    nums = re.findall(r"\d+", wind_speed)
    return float(max(int(n) for n in nums)) if nums else 0.0


def _qv(node, default=0.0) -> float:
    """NWS wraps many values as QuantitativeValue: {"value": 12, "unitCode": ...}.
    Sometimes `value` is null (data genuinely unavailable). One tiny helper
    beats null-checking at every call site."""
    if isinstance(node, dict):
        v = node.get("value")
        return float(v) if v is not None else default
    return float(node) if node is not None else default


# ─────────────────────────────────────────────────────────────────────────
# NWS WEATHER
# ─────────────────────────────────────────────────────────────────────────

async def _get_grid_endpoint(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[str]:
    """Step 1 of NWS's two-step flow.

    NWS forecasts are published on ~2.5km grids owned by regional offices, so
    you can't request a forecast by lat/lon directly. You first hit /points to
    translate coordinates → the grid forecast URL, THEN fetch that URL.

    We cache the result for a week: NWS's docs say grid mappings rarely change
    but occasionally DO, so caching forever would eventually break silently.
    """
    key = f"grid:{lat},{lon}"
    if (cached := _cache_get(key, GRID_TTL)) is not None:
        return cached
    try:
        r = await client.get(f"{NWS_BASE}/points/{lat},{lon}")
        r.raise_for_status()
        # We want the HOURLY forecast: current-conditions accuracy for "right now"
        url = r.json()["properties"]["forecastHourly"]
        _cache_set(key, url)
        return url
    except Exception as e:
        print(f"[sources] NWS grid lookup failed for {lat},{lon}: {e}")
        return None


def _current_period(periods: list[dict]) -> dict:
    """The forecast period covering right now, else the first one.

    Pure function so the selection can be unit-tested against canned periods
    without an NWS round-trip.
    """
    now = datetime.now(timezone.utc)
    for p in periods:
        try:
            start = datetime.fromisoformat(p["startTime"])
            end = datetime.fromisoformat(p["endTime"])
        except (KeyError, ValueError):
            continue
        if start <= now < end:
            return p
    return periods[0]


async def fetch_weather(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[WeatherData]:
    """Current-hour weather for a coordinate. Returns None on any failure
    (see GRACEFUL DEGRADATION note at top)."""
    key = f"wx:{lat},{lon}"
    if (cached := _cache_get(key, FORECAST_TTL)) is not None:
        return cached

    grid_url = await _get_grid_endpoint(client, lat, lon)
    if grid_url is None:
        return None

    try:
        r = await client.get(grid_url)
        r.raise_for_status()
        periods = r.json()["properties"]["periods"]
        # periods[0] is NOT reliably the current hour. Measured live at 18:04
        # EDT, periods[0] still started at 17:00 — so trusting index 0 showed
        # the previous hour's conditions ("Sunny" at dusk, when the current
        # period said "Mostly Clear").
        #
        # Select the period whose [startTime, endTime) window actually contains
        # now, falling back to periods[0] when nothing matches (feed ahead of
        # or behind us — better slightly stale than a crash).
        now = _current_period(periods)

        data = WeatherData(
            temp_f=float(now["temperature"]),          # already F for US grids
            humidity_pct=_qv(now.get("relativeHumidity")),
            precip_prob=_qv(now.get("probabilityOfPrecipitation")),
            wind_mph=_parse_wind_mph(now.get("windSpeed", "")),
            short_forecast=now.get("shortForecast", ""),
        )
        _cache_set(key, data)
        return data
    except Exception as e:
        print(f"[sources] NWS forecast failed for {lat},{lon}: {e}")
        return None


async def fetch_alerts(client: httpx.AsyncClient, area: str = "FL") -> list[str]:
    """Active NWS alerts for a state. Cheap to add, genuinely useful for a
    beach app — a rip current statement or thunderstorm warning is exactly the
    kind of thing that should override a sunny-looking index.

    Returns headlines only; returns [] on failure so callers never need to
    null-check this one.
    """
    try:
        r = await client.get(f"{NWS_BASE}/alerts/active", params={"area": area})
        r.raise_for_status()
        return [f["properties"].get("headline", "")
                for f in r.json().get("features", [])]
    except Exception as e:
        print(f"[sources] NWS alerts failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────
# NOAA TIDES & WATER TEMPERATURE
# ─────────────────────────────────────────────────────────────────────────
# Unlike NWS, CO-OPS is ONE endpoint (`datagetter`) where the `product`
# parameter selects what you get. Same URL, different query params.

async def _noaa_get(client: httpx.AsyncClient, params: dict) -> Optional[dict]:
    """Shared CO-OPS request. Note `application=` — NOAA asks callers to
    identify themselves here (their equivalent of NWS's User-Agent)."""
    base_params = {
        "format": "json",
        "units": "english",           # feet + Fahrenheit (matches the rest of the app)
        "time_zone": "lst_ldt",       # local time WITH daylight saving — what users expect
        "application": "sargassum-beach-app",
    }
    try:
        r = await client.get(NOAA_BASE, params={**base_params, **params})
        r.raise_for_status()
        data = r.json()
        # CO-OPS returns HTTP 200 with an {"error": {...}} body on bad requests,
        # so raise_for_status() alone isn't enough — check the payload too.
        if "error" in data:
            print(f"[sources] NOAA error: {data['error']}")
            return None
        return data
    except Exception as e:
        print(f"[sources] NOAA request failed: {e}")
        return None


async def fetch_tides(client: httpx.AsyncClient, station: str,
                      days: int = 2) -> Optional[TideData]:
    """High/low tide predictions.

    `interval=hilo` returns ONLY the turning points (high & low) rather than
    a reading every 6 minutes — exactly what a UI card needs ("next high tide
    at 3:42pm") and far less data to move.

    ALTERNATIVE: drop `interval` to get 6-minute resolution if you later want
    a smooth tide curve on the forecast timeline scrubber.
    """
    key = f"tide:{station}:{days}"
    if (cached := _cache_get(key, TIDE_TTL)) is not None:
        return cached

    today = datetime.now()
    data = await _noaa_get(client, {
        "station": station,
        "product": "predictions",
        "datum": "MLLW",              # Mean Lower Low Water: the US tide-chart standard
        "interval": "hilo",
        "begin_date": today.strftime("%Y%m%d"),
        "end_date": (today + timedelta(days=days)).strftime("%Y%m%d"),
    })
    if data is None:
        return None

    try:
        events = [
            TideEvent(
                # CO-OPS returns "2026-07-20 15:42" with no tz marker, and
                # because _noaa_get sends time_zone=lst_ldt those times are
                # LOCAL (EDT/EST), not UTC.
                #
                # This used to attach timezone.utc "just to make comparisons
                # work". That silently broke next_event(): a 21:54 EDT low tide
                # became 21:54 UTC, which is 17:54 EDT — already in the past by
                # the real clock — so every tide within the next 4 hours got
                # filtered out and the UI showed the one AFTER the next one.
                # Verified against live data: at 18:04 EDT it reported
                # "High 03:40 tomorrow" when the true answer was
                # "Low 21:54 tonight".
                time=datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=BEACH_TZ),
                height_ft=float(p["v"]),
                kind=p["type"],       # "H" or "L"
            )
            for p in data.get("predictions", [])
        ]
        result = TideData(events=events)
        _cache_set(key, result)
        return result
    except Exception as e:
        print(f"[sources] tide parse failed for {station}: {e}")
        return None


async def fetch_water_temp(client: httpx.AsyncClient, station: str) -> Optional[float]:
    """Water temperature in °F — a free extra signal from the SAME API you're
    already calling for tides. Not every station has a temp sensor, so a None
    here is normal, not a bug (scoring.py just renormalizes around it).
    """
    key = f"wtemp:{station}"
    if (cached := _cache_get(key, FORECAST_TTL)) is not None:
        return cached

    data = await _noaa_get(client, {
        "station": station,
        "product": "water_temperature",
        "date": "latest",
    })
    if data is None:
        return None
    try:
        temp = float(data["data"][0]["v"])
        _cache_set(key, temp)
        return temp
    except Exception:
        return None   # station has no temp sensor — expected for many stations

# ─────────────────────────────────────────────────────────────────────────
# SURF ZONE FORECAST — rip current risk (+ UV, WHERE AVAILABLE)
# ─────────────────────────────────────────────────────────────────────────
# CONFIRMED WITH REAL DATA: whether UV appears in this bulletin is OFFICE-
# DEPENDENT. Miami (MFL) — which covers Palm Beach County — omits it
# entirely; its footnote just points to https://www.weather.gov/beach/mfl
# for UV *definitions*, not a value. Other offices (e.g. NY, NJ) DO print a
# UV Index line. So _parse_srf_text still extracts UV when present, but for
# THIS deployment (MFL), treat uv_index from this function as always None
# and get UV from fetch_uv_index() (EPA) instead — see below.
#
# WHY THIS IS FIDDLY: unlike /gridpoints, this is a human-readable TEXT
# bulletin, not structured JSON. Fields are fixed-width-labeled lines like:
#     Rip Current Risk*...........Low.
# We parse with regex rather than treating this as a stable schema, because
# it technically isn't one — NWS could reformat this bulletin's prose
# without notice, and offices already disagree on which fields they include.
# Treat parse failures (or absent fields) as "signal unavailable", never as
# a crash.

SRF_OFFICE = "MFL"   # Miami WFO — covers Palm Beach County's coastline

_RIP_RE = re.compile(r"Rip Current Risk\**\.+\s*([A-Za-z]+)\.")
_UV_RE  = re.compile(r"UV Index\**\.+\s*([A-Za-z ]+?)\.")

# UV comes back as PROSE ("Very High"), but scoring.py's uv_score() expects
# the numeric EPA scale. This maps prose -> a representative index value.
# APPROXIMATION: each category collapses a range (e.g. "High" = 6-7) to one
# number. Good enough for a comfort score; not a substitute for the real
# per-location EPA numeric index if you ever need exact values.
_UV_LABEL_TO_INDEX = {
    "low": 2, "moderate": 4, "high": 7, "very high": 9, "extreme": 11,
}


def _parse_srf_text(text: str) -> dict:
    """Pure parsing function — no I/O, so it's trivially unit-testable
    (verified against real NWS sample text before this was wired in)."""
    rip_match = _RIP_RE.search(text)
    uv_match = _UV_RE.search(text)

    rip_risk = rip_match.group(1).strip().lower() if rip_match else None
    # Guard against a value NWS might use that we don't recognize (e.g. a
    # typo, or a category we haven't seen) — better to return None than to
    # silently pass a bad string into scoring.py's dict lookup.
    if rip_risk not in ("low", "moderate", "high"):
        rip_risk = None

    uv_label = uv_match.group(1).strip() if uv_match else None
    uv_index = _UV_LABEL_TO_INDEX.get(uv_label.lower()) if uv_label else None

    return {"rip_risk": rip_risk, "uv_label": uv_label, "uv_index": uv_index}


async def fetch_surf_zone_forecast(client: httpx.AsyncClient,
                                   office: str = SRF_OFFICE) -> dict:
    """Rip current risk + UV index for a coastal office's latest Surf Zone
    Forecast bulletin.

    NOTE: this is issued per OFFICE, not per exact coordinate — Miami's SRF
    covers a stretch of coastline in one bulletin (occasionally split by
    county section within the text). For four beaches all in Palm Beach
    County under the same office, one fetch covers all of them; don't
    over-engineer per-beach granularity this text format doesn't actually
    offer.

    Returns {"rip_risk": None, "uv_label": None, "uv_index": None} on any
    failure — same graceful-degradation contract as every other fetcher here.
    """
    empty = {"rip_risk": None, "uv_label": None, "uv_index": None}
    key = f"srf:{office}"
    if (cached := _cache_get(key, FORECAST_TTL)) is not None:
        return cached

    try:
        # Step 1: list recent SRF products for this office, take the latest.
        r = await client.get(f"{NWS_BASE}/products/types/SRF/locations/{office}")
        r.raise_for_status()
        products = r.json().get("@graph", [])
        if not products:
            return empty
        latest_url = products[0]["@id"]     # products are newest-first

        # Step 2: fetch that specific product's full text.
        r2 = await client.get(latest_url)
        r2.raise_for_status()
        text = r2.json().get("productText", "")

        result = _parse_srf_text(text)
        _cache_set(key, result)
        return result
    except Exception as e:
        print(f"[sources] SRF fetch/parse failed for {office}: {e}")
        return empty
    
# ─────────────────────────────────────────────────────────────────────────
# ORCHESTRATION
# ─────────────────────────────────────────────────────────────────────────

async def fetch_beach_conditions(beach_id: str) -> dict:
    """Fetch every external signal for one beach. (See docstring above for
    the concurrency rationale — unchanged.)"""
    cfg = BEACHES.get(beach_id)
    if cfg is None:
        raise ValueError(f"Unknown beach: {beach_id}")

    async with httpx.AsyncClient(
        timeout=10.0,
        headers={"User-Agent": NWS_USER_AGENT},
        follow_redirects=True,
    ) as client:
        weather, tides, water_temp, surf, uv = await asyncio.gather(
            fetch_weather(client, cfg["lat"], cfg["lon"]),
            fetch_tides(client, cfg["tide_station"]),
            # Deliberately a DIFFERENT station id: the nearest tide-prediction
            # station often has no thermometer. Falls back to the tide station
            # for any beach that hasn't declared one.
            fetch_water_temp(client, cfg.get("temp_station", cfg["tide_station"])),
            fetch_surf_zone_forecast(client),
            # lat/lon, not the ZIP: the primary UV source is now Open-Meteo,
            # which is keyed by coordinate. cfg["zip_code"] is still used for
            # the EPA fallback inside this function.
            fetch_uv_index(client, cfg["lat"], cfg["lon"], cfg["zip_code"]),
            return_exceptions=True,
        )

    norm = lambda v: None if isinstance(v, Exception) else v

    return {
        "beach_id": beach_id,
        "name": cfg["name"],
        "lat": cfg["lat"],
        "lon": cfg["lon"],
        "weather": norm(weather),
        "tides": norm(tides),
        "water_temp_f": norm(water_temp),
        "surf": norm(surf),          # {"rip_risk", "uv_label", "uv_index"} — uv_index here is usually None (MFL)
        "uv_index": norm(uv),        # Open-Meteo, falling back to EPA
    }

async def fetch_all_beaches() -> dict[str, dict]:
    """All four beaches concurrently — this backs your GET /beaches endpoint."""
    results = await asyncio.gather(
        *(fetch_beach_conditions(bid) for bid in BEACHES),
        return_exceptions=True,
    )
    return {
        r["beach_id"]: r
        for r in results
        if not isinstance(r, Exception)
    }




# ─────────────────────────────────────────────────────────────────────────
# UV INDEX — EPA fallback for offices whose SRF omits it (confirmed: MFL does)
# ─────────────────────────────────────────────────────────────────────────
# HOURLY, not DAILY. This was a real bug worth spelling out:
#
#   getEnvirofactsUVDAILY returns ONE row per ZIP holding the day's PEAK UV
#   ("UV_INDEX": "12"), with no time component at all. Using it meant the app
#   reported "UV Index 12 — Extreme" at 6pm, when the sun was nearly down and
#   the true value was 2. It would have said 12 at midnight too.
#
#   getEnvirofactsUVHOURLY returns a row per hour ("DATE_TIME": "Jul/22/2026
#   06 PM", "UV_VALUE": 2), which is what "current UV" actually requires.
#
# Field names differ between the two endpoints (UV_INDEX vs UV_VALUE), so this
# is not a drop-in URL swap.
EPA_UV_BASE = "https://data.epa.gov/efservice/getEnvirofactsUVHOURLY/ZIP"

# EPA formats the hour as "Jul/22/2026 06 PM" — zero-padded 12-hour with an
# AM/PM marker.
_EPA_DT_FMT = "%b/%d/%Y %I %p"


def _pick_current_uv(rows: list[dict], now_local: datetime) -> Optional[int]:
    """Choose the row matching the current local hour.

    Pure function (no I/O) so the hour-matching logic is unit-testable without
    hitting EPA — same discipline as _parse_srf_text.

    TWO-TIER MATCH, because the feed is a FORWARD-LOOKING window that does not
    reliably include the current hour. Observed live at 18:04 EDT: the window
    ran 08 PM today through 07 PM tomorrow, with no 6 PM row at all.

      1. Exact date+hour match, when the current hour is in the window.
      2. Otherwise the same HOUR-OF-DAY from any day in the window. The UV
         diurnal curve barely moves between adjacent days, so "6 PM tomorrow"
         is a far better estimate of "6 PM today" than either the day's peak
         or nothing.
    """
    parsed = []
    for r in rows:
        try:
            parsed.append((datetime.strptime(r["DATE_TIME"].strip(), _EPA_DT_FMT),
                           int(r["UV_VALUE"])))
        except (ValueError, KeyError, TypeError):
            continue          # one malformed row shouldn't lose the whole feed
    if not parsed:
        return None

    # 1. exact hour
    for dt, uv in parsed:
        if (dt.year, dt.month, dt.day, dt.hour) == (
                now_local.year, now_local.month, now_local.day, now_local.hour):
            return uv

    # 2. same hour-of-day, nearest date
    same_hour = [(dt, uv) for dt, uv in parsed if dt.hour == now_local.hour]
    if same_hour:
        return min(same_hour, key=lambda p: abs((p[0].date() - now_local.date()).days))[1]

    return None


async def _fetch_uv_epa(client: httpx.AsyncClient, zip_code: str,
                        now_local: datetime) -> Optional[int]:
    """EPA hourly UV for a ZIP. Now the FALLBACK source — see fetch_uv_index."""
    try:
        r = await client.get(f"{EPA_UV_BASE}/{zip_code}/JSON")
        r.raise_for_status()
        return _pick_current_uv(r.json(), now_local)
    except Exception as e:
        print(f"[sources] EPA UV fetch failed for {zip_code}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
# Open-Meteo — PRIMARY UV source
# ─────────────────────────────────────────────────────────────────────────
# Free, no API key, keyed by lat/lon (so it needs no ZIP and works for any
# beach we add later).
#
# WHY IT REPLACED EPA AS PRIMARY — measured, on a real evening:
#
#   At 19:23 EDT, sunset 20:09, sun still 9.6 degrees above the horizon, the
#   app displayed "UV Index 0" and scored UV a perfect 10.
#
#   The cause was NOT a timezone bug — EPA's timestamps are genuinely local
#   (verified: its curve starts at sunrise and peaks either side of solar noon
#   at 13:26). Two separate weaknesses combined:
#     1. EPA's hourly feed is FORWARD-looking and does not contain the current
#        hour at all — at 19:23 the window began at 20:00 — so we were already
#        substituting the same hour from the following day.
#     2. EPA publishes a flat 0 for the 19:00 hour even though the sun is up,
#        i.e. its evening tail truncates early.
#
#   Open-Meteo returned 1.85 for that same hour, then 0.45 at 20:00 and 0.0 at
#   21:00 — which tracks the 20:09 sunset properly, and gives decimals instead
#   of integers that round genuine low readings to zero.
#
# KNOWN TRADEOFF: Open-Meteo's midday peak reads lower than EPA's (8.9 vs 12
# on the same clear-sky day). For 26.8N in July, EPA's peak is probably the
# more realistic figure, so this source may understate midday burn risk. It was
# still chosen because being wrong by a couple of points at noon is less
# misleading than reporting "0, perfectly safe" while the sun is still up.
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"


async def _fetch_uv_open_meteo(client: httpx.AsyncClient, lat: float, lon: float,
                               now_local: datetime) -> Optional[float]:
    """Current-hour UV index from Open-Meteo, or None."""
    try:
        r = await client.get(OPEN_METEO_BASE, params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "uv_index",
            "timezone": "America/New_York",
            "forecast_days": 1,
        })
        r.raise_for_status()
        hourly = r.json().get("hourly") or {}
        times = hourly.get("time") or []
        values = hourly.get("uv_index") or []
        stamp = f"{now_local:%Y-%m-%dT%H:00}"
        for t, v in zip(times, values):
            if t == stamp and v is not None:
                # Round to 1dp: the UI prints an integer, and pretending to
                # more precision than a forecast model has is false comfort.
                return round(float(v), 1)
        return None
    except Exception as e:
        print(f"[sources] Open-Meteo UV fetch failed for {lat},{lon}: {e}")
        return None


async def fetch_uv_index(client: httpx.AsyncClient, lat: float, lon: float,
                         zip_code: str) -> Optional[float]:
    """Current-hour UV index. Open-Meteo first, EPA second.

    Independent of NWS/SRF entirely — this is why UV isn't folded into
    fetch_surf_zone_forecast: that function's output is confirmed
    office-dependent, and UV needs a source that's consistent across all four
    beaches regardless of which NWS office covers them.

    Cached on the hour, not just by TTL: the underlying value is hourly, so a
    cache key that ignores the hour would keep serving noon's reading into the
    evening — reintroducing exactly the staleness this was fixed for.

    Returns None only if BOTH sources fail. A returned 0.0 is a real reading
    (night) and must not be confused with None — see main._first_not_none.
    """
    now_local = datetime.now(BEACH_TZ)
    key = f"uv:{lat},{lon}:{now_local:%Y%m%d%H}"
    if (cached := _cache_get(key, FORECAST_TTL)) is not None:
        return cached

    uv = await _fetch_uv_open_meteo(client, lat, lon, now_local)
    if uv is None:
        print(f"[sources] Open-Meteo UV unavailable for {lat},{lon} — trying EPA")
        uv = await _fetch_uv_epa(client, zip_code, now_local)

    # `is not None`, never truthiness: a nighttime 0.0 is a valid answer and
    # caching it is correct.
    if uv is not None:
        _cache_set(key, uv)
    return uv
    
if __name__ == "__main__":
    import sys

    async def _demo():
        beach_id = sys.argv[1] if len(sys.argv) > 1 else "lake-worth"
        debug_srf = "--debug-srf" in sys.argv

        if beach_id not in BEACHES:
            print(f"unknown beach '{beach_id}' — choices: {list(BEACHES)}")
            return

        data = await fetch_beach_conditions(beach_id)
        print(f"\n{data['name']}")

        w = data["weather"]
        print(f"  weather:    {w if w else 'unavailable'}")

        wt = data["water_temp_f"]
        print(f"  water temp: {wt if wt is not None else 'unavailable'}°F")

        tides = data["tides"]
        if tides and tides.events:
            nxt = tides.next_event()
            print(f"  next tide:  {nxt.kind} @ {nxt.time} ({nxt.height_ft} ft)")
            print(f"  events:     {len(tides.events)} over 2 days")
        else:
            print("  tides:      unavailable")

        surf = data["surf"]
        if surf:
            print(f"  rip risk:   {surf['rip_risk'] or 'unavailable'}")
        else:
            print("  rip risk:   unavailable")

        # NEW: the authoritative UV source. surf['uv_index'] stays in the dict
        # (still populated for offices that DO print it in their SRF text), but
        # for MFL it's always None — this line is what actually carries UV now.
        uv = data["uv_index"]
        if uv is not None:
            print(f"  UV (EPA):   {uv}")
        else:
            print("  UV (EPA):   unavailable — check EPA_UV_BASE / zip_code")

        print("  red tide:   not implemented yet (FL FWC — future work)")
        print("  wave height: not implemented yet (NWS forecastGridData — future work)")

        # Updated to reflect the real signal set now in play: weather, tides,
        # surf (rip risk), and uv are the four that can genuinely fail.
        all_none = all(v is None for v in (w, wt, tides, surf, uv))
        if all_none:
            print("\n  ⚠️  ALL signals failed — check network/User-Agent first.")

        if debug_srf:
            async with httpx.AsyncClient(
                timeout=10.0, headers={"User-Agent": NWS_USER_AGENT},
                follow_redirects=True,
            ) as client:
                r = await client.get(f"{NWS_BASE}/products/types/SRF/locations/{SRF_OFFICE}")
                products = r.json().get("@graph", [])
                if products:
                    r2 = await client.get(products[0]["@id"])
                    print("\n--- raw MFL SRF text ---")
                    print(r2.json().get("productText", "")[:2000])

    
    asyncio.run(_demo())