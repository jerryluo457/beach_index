"""
test_sources.py — regression tests for the pure logic in sources.py.

Run:  .venv/bin/python test_sources.py

No pytest dependency and no network: every function tested here was
deliberately written as a pure function precisely so it could be exercised
against canned payloads. Each test below corresponds to a bug that actually
shipped and was caught by comparing the app against its own upstream sources.
"""

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sources import (
    BEACH_TZ,
    TideData,
    TideEvent,
    _current_period,
    _parse_srf_text,
    _parse_wind_mph,
    _pick_current_uv,
)

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(name)


# ─────────────────────────────────────────────────────────────────────────
# UV — the "Extreme at 6pm" bug
# ─────────────────────────────────────────────────────────────────────────
# The old code called getEnvirofactsUVDAILY, which returns the day's PEAK with
# no time component, so the app reported 12/Extreme at dusk and would have
# reported it at midnight too.

UV_ROWS = [
    {"DATE_TIME": "Jul/23/2026 12 PM", "UV_VALUE": 10},
    {"DATE_TIME": "Jul/23/2026 01 PM", "UV_VALUE": 12},
    {"DATE_TIME": "Jul/23/2026 05 PM", "UV_VALUE": 4},
    {"DATE_TIME": "Jul/23/2026 06 PM", "UV_VALUE": 2},
    {"DATE_TIME": "Jul/22/2026 08 PM", "UV_VALUE": 0},
]


def test_uv():
    print("UV hour selection")
    # Exact date+hour hit
    check("exact hour match",
          _pick_current_uv(UV_ROWS, datetime(2026, 7, 22, 20, 30)), 0)
    # Current hour absent from the window (observed live) -> same hour-of-day
    check("6pm falls back to same hour-of-day, not the daily peak",
          _pick_current_uv(UV_ROWS, datetime(2026, 7, 22, 18, 4)), 2)
    check("1pm peak still resolves to 12",
          _pick_current_uv(UV_ROWS, datetime(2026, 7, 23, 13, 15)), 12)
    # Robustness
    check("empty feed -> None", _pick_current_uv([], datetime(2026, 7, 22, 12)), None)
    check("malformed rows skipped",
          _pick_current_uv([{"DATE_TIME": "garbage", "UV_VALUE": 9},
                            {"DATE_TIME": "Jul/22/2026 03 PM", "UV_VALUE": 7}],
                           datetime(2026, 7, 22, 15, 0)), 7)
    check("no matching hour -> None",
          _pick_current_uv([{"DATE_TIME": "Jul/22/2026 03 PM", "UV_VALUE": 7}],
                           datetime(2026, 7, 22, 4, 0)), None)

    # AFTER SUNSET UV IS GENUINELY 0 — a reading, not an absence.
    night = [{"DATE_TIME": "Jul/22/2026 08 PM", "UV_VALUE": 0},
             {"DATE_TIME": "Jul/22/2026 09 PM", "UV_VALUE": 0}]
    got = _pick_current_uv(night, datetime(2026, 7, 22, 20, 30))
    check("night returns 0", got, 0)
    check("  and 0 is not None (they must stay distinguishable)", got is None, False)


def test_falsy_zero_trap():
    """The bug this guards: `0 or fallback` silently discards a real zero.

    Shipped live — after sunset the EPA reported UV 0, `uv_measured =
    a or b` turned it into None, the card showed an em dash, and the scorer
    substituted DEFAULT_UV (6.0), scoring a dark beach 6.7 instead of 10.
    """
    print("Falsy-zero handling")
    # Plain-Python demonstration of why `or` is the wrong operator here.
    check("`0 or None` collapses a real zero", (0 or None), None)

    from main import _first_not_none
    check("_first_not_none keeps a real 0", _first_not_none(0, 6.0), 0)
    check("_first_not_none falls through on None", _first_not_none(None, 6.0), 6.0)
    check("_first_not_none keeps 0 even in second place",
          _first_not_none(None, 0), 0)
    check("all None -> None", _first_not_none(None, None), None)

    from scoring import uv_score
    check("uv_score(0) is a perfect 10 (no burn risk at night)", uv_score(0), 10.0)
    check("uv_score(DEFAULT_UV=6) is NOT 10 — the wrong answer we were showing",
          round(uv_score(6.0), 1), 6.7)


# ─────────────────────────────────────────────────────────────────────────
# TIDES — the mislabelled-timezone bug
# ─────────────────────────────────────────────────────────────────────────
# NOAA returns local time (time_zone=lst_ldt). Stamping it timezone.utc made
# every tide in the next 4 hours look already-past, so the UI showed the tide
# AFTER the next one. Observed live: reported "High 03:40 tomorrow" when the
# true next event was "Low 21:54 tonight".

def test_tides():
    print("Tide next_event")
    events = [
        TideEvent(datetime(2026, 7, 22, 15, 45, tzinfo=BEACH_TZ), 2.588, "H"),
        TideEvent(datetime(2026, 7, 22, 21, 54, tzinfo=BEACH_TZ), 0.621, "L"),
        TideEvent(datetime(2026, 7, 23, 3, 40, tzinfo=BEACH_TZ), 2.181, "H"),
    ]
    td = TideData(events=events)

    # 18:04 EDT == 22:04 UTC. The old code compared 21:54 (mislabelled UTC)
    # against 22:04 UTC and wrongly dropped it.
    now = datetime(2026, 7, 22, 18, 4, tzinfo=BEACH_TZ)
    nxt = td.next_event(now)
    check("evening: next tide is tonight's low, not tomorrow's high",
          (nxt.kind, nxt.time.hour, nxt.time.minute), ("L", 21, 54))

    nxt2 = td.next_event(datetime(2026, 7, 22, 10, 0, tzinfo=BEACH_TZ))
    check("morning: next tide is the afternoon high",
          (nxt2.kind, nxt2.time.hour), ("H", 15))

    check("all events past -> None",
          td.next_event(datetime(2026, 7, 24, 0, 0, tzinfo=BEACH_TZ)), None)

    # The comparison must work against a real UTC 'now', since that is what
    # next_event() defaults to in production.
    nxt3 = td.next_event(datetime(2026, 7, 22, 22, 4, tzinfo=timezone.utc))
    check("aware UTC now compares correctly across the offset",
          (nxt3.kind, nxt3.time.hour), ("L", 21))


# ─────────────────────────────────────────────────────────────────────────
# WEATHER — the stale-period bug
# ─────────────────────────────────────────────────────────────────────────
# periods[0] was assumed to be the current hour. Measured live at 18:04 EDT it
# still started at 17:00, so the app showed the previous hour's conditions.

def test_current_period():
    print("NWS period selection")
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def period(offset_h, label):
        return {
            "startTime": (base + timedelta(hours=offset_h)).isoformat(),
            "endTime": (base + timedelta(hours=offset_h + 1)).isoformat(),
            "shortForecast": label,
        }

    # Feed lagging by an hour — exactly what NWS actually returned.
    periods = [period(-1, "stale"), period(0, "current"), period(1, "next")]
    check("picks the period containing now, not index 0",
          _current_period(periods)["shortForecast"], "current")

    # Feed entirely in the future -> fall back rather than crash
    check("no containing period -> falls back to periods[0]",
          _current_period([period(5, "future"), period(6, "later")])["shortForecast"],
          "future")

    check("malformed period skipped",
          _current_period([{"startTime": "nope"}, period(0, "current")])["shortForecast"],
          "current")


# ─────────────────────────────────────────────────────────────────────────
# Pre-existing pure helpers — guard against regressions
# ─────────────────────────────────────────────────────────────────────────

def test_stations():
    """Station config, including the ocean-vs-ICWW choice.

    Every station closer to Boynton/Boca than 8722670 is on the Intracoastal,
    which measured 2 hours off the ocean. Sharing the ocean pier is the
    deliberate, more accurate option for beaches that face the surf.
    """
    print("Tide / temperature station config")
    from sources import BEACHES

    check("Jupiter uses its own ocean jetty station",
          BEACHES["jupiter"]["tide_station"], "8722495")
    # NOT 8722670: despite the name, "Lake Worth Pier" is 10.7 mi south of
    # Lake Worth Inlet, in the city of Lake Worth Beach.
    check("Lake Worth uses the station inside its own inlet",
          BEACHES["lake-worth"]["tide_station"], "8722588")
    # 8722669 is "Lake Worth ICW" — an Intracoastal station ~90 min off the
    # ocean. Easy to reach for by name; wrong for a surf beach.
    check("no beach uses the Intracoastal look-alike station",
          any(b["tide_station"] == "8722669" for b in BEACHES.values()), False)

    # Water temperature is a physical sensor; only 8722670 has one here.
    check("every beach has a temp_station",
          all("temp_station" in b for b in BEACHES.values()), True)
    check("temp always resolves to the station that actually has a thermometer",
          {b["temp_station"] for b in BEACHES.values()}, {"8722670"})
    check("Jupiter's tide and temp stations are deliberately different",
          BEACHES["jupiter"]["tide_station"] != BEACHES["jupiter"]["temp_station"],
          True)

    # ZIP is no longer the primary UV key (Open-Meteo uses lat/lon) but is
    # still needed for the EPA fallback.
    check("every beach still has a zip for the EPA UV fallback",
          all(b.get("zip_code") for b in BEACHES.values()), True)
    check("every beach has coordinates for the Open-Meteo UV lookup",
          all(b.get("lat") and b.get("lon") for b in BEACHES.values()), True)


def test_wind_and_srf():
    print("Wind parsing")
    check("'13 mph'", _parse_wind_mph("13 mph"), 13.0)
    # Documented design choice: the HIGHER end of a range, because the gustier
    # value is what you'd actually feel on the beach.
    check("range '10 to 15 mph' takes the gustier end",
          _parse_wind_mph("10 to 15 mph"), 15.0)
    check("empty -> 0", _parse_wind_mph(""), 0.0)
    check("garbage -> 0", _parse_wind_mph("breezy"), 0.0)

    print("Surf Zone Forecast parsing")
    txt = ("...SURF ZONE FORECAST...\n"
           "Rip Current Risk.......... Moderate.\n"
           "UV Index.................. Very High.\n")
    got = _parse_srf_text(txt)
    check("rip risk parsed", got["rip_risk"], "moderate")
    check("uv label parsed", got["uv_label"], "Very High")
    check("uv prose mapped to index", got["uv_index"], 9)

    check("unrecognised rip category rejected rather than passed through",
          _parse_srf_text("Rip Current Risk.......... Catastrophic.")["rip_risk"], None)
    check("absent fields -> None",
          _parse_srf_text("nothing useful here")["rip_risk"], None)


if __name__ == "__main__":
    for t in (test_uv, test_falsy_zero_trap, test_tides, test_current_period,
              test_stations, test_wind_and_srf):
        t()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("all sources.py tests passed")
