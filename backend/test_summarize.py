"""
test_summarize.py — the validator that stands between a small LLM and the user.

Run:  .venv/bin/python test_summarize.py

No model is loaded here. Every case below is a REAL string produced by a real
local model against this app's own data, kept as a regression corpus. If the
validator ever stops rejecting these, unsafe text reaches the page.
"""

import sys

from summarize import (
    _trim_to_sentences,
    build_prompt,
    fallback_summary,
    fingerprint,
    retrieve,
    uv_band,
    validate,
)

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(name)


BEACH = dict(
    name="Lake Worth Inlet", index=8.2, temp_f=88.0, water_temp_f=86.9,
    short_forecast="Mostly Clear", wind_mph=12.0, uv_index=2.0,
    next_tide="Low at 21:54", rip_risk="low", sargassum_label="Mild",
    crowd_label="Moderate", water_severity="clear", supported=True,
)


def test_rejects_real_hallucinations():
    print("Rejects text real models actually produced")

    # Qwen2.5-0.5B, verbatim. "low risk, stay careful" -> "no risk".
    bad = ("Today's weather is mostly clear with temperatures of 88 degrees. "
           "There is no visible rip current risk, but some sargassum seaweed "
           "may be present on the sand.")
    ok, why = validate(bad, BEACH)
    check("rejects 'no visible rip current risk'", ok, False)

    # SmolLM2-360M, verbatim. 90 appears nowhere in the inputs.
    bad2 = ("Lake Worth Inlet is a beautiful destination. The weather is perfect, "
            "with temperatures ranging from 88 to 90 degrees throughout the day, "
            "making it ideal for swimming and sunbathing.")
    ok2, why2 = validate(bad2, BEACH)
    check("rejects invented temperature 90", ok2, False)
    check("  and names the number", "90" in why2, True)

    # flan-t5-base, verbatim. Inverts a crowd note into a recommendation.
    bad3 = ("If you're looking for a beach with a lot of people, this is a good "
            "place to go. There is no risk today and it is completely safe.")
    check("rejects 'no risk' / 'completely safe'", validate(bad3, BEACH)[0], False)

    # Invented causation between unrelated readings, observed on Jupiter.
    bad4 = ("Overall rating 8.2 out of 10, with calm waters. Wind at 12 mph "
            "suggests minimal rip currents today.")
    check("rejects wind->rip-current causal claim", validate(bad4, BEACH)[0], False)

    # Downgrading sun protection beyond what the knowledge base says.
    bad5 = ("It is 88 degrees and mostly clear at Lake Worth Inlet. UV levels are "
            "low so sun protection is unnecessary for your visit today.")
    check("rejects 'sun protection is unnecessary'", validate(bad5, BEACH)[0], False)

    # Qwen2.5-0.5B on Jupiter, verbatim. Caught only AFTER it had rendered on
    # the page — the narrower "sun protection is unnecessary" rule missed it,
    # which is why the word "safe" is now banned outright.
    bad6 = ("Jupiter Inlet Beach: Overall rating 9.0 out of 10, with calm winds and "
            "clear skies. The UV index is 2, making it safe for extended periods "
            "without sunscreen. However, swim near a lifeguard.")
    check("rejects 'safe for extended periods without sunscreen'",
          validate(bad6, BEACH)[0], False)
    check("rejects any bare claim of safety",
          validate("The beach is 88 degrees and mostly clear today, and it is safe "
                   "for swimming right now near the lifeguard tower.", BEACH)[0], False)

    # Qwen2.5-0.5B on Boca, verbatim. Reached the page despite a "no danger"
    # rule already existing — "immediate" sat between the two words, which is
    # why the negation patterns now tolerate intervening words.
    bad7 = ("Boca Raton - The overall beach rating is 8.2 out of 10. The air "
            "temperature is 88 degrees and the UV index is 2. There is no "
            "immediate danger from rip currents near the jetties and piers.")
    check("rejects 'no immediate danger from rip currents'",
          validate(bad7, BEACH)[0], False)
    for phrase in ["there is no real risk today at this beach right now",
                   "conditions pose little danger to swimmers this afternoon",
                   "the water is not particularly dangerous this evening",
                   "there is minimal risk of rip currents near the pier"]:
        body = ("It is 88 degrees and mostly clear with wind at 12 miles per hour. "
                + phrase + ".")
        check(f"rejects: {phrase[:44]}…", validate(body, BEACH)[0], False)

    # Prompt echo.
    check("rejects an echoed prompt",
          validate("Beach: Lake Worth Inlet. Facts: 88 degrees, 12 miles per hour, "
                   "UV index 2, water 87 degrees.", BEACH)[0], False)

    # Degenerate lengths.
    check("rejects too-short output", validate("Nice beach.", BEACH)[0], False)
    check("rejects empty output", validate("", BEACH)[0], False)


def test_accepts_good_text():
    print("Accepts faithful text")
    good = ("Lake Worth Inlet is mostly clear and 88 degrees, with the water at 87 "
            "degrees and wind at 12 miles per hour. Rip current risk is low, but "
            "rip currents can still form near jetties, so swim near a lifeguard. "
            "Some sargassum is on the sand.")
    ok, why = validate(good, BEACH)
    check("accepts an accurate paragraph", ok, True)
    check("  reason is ok", why, "ok")

    # The index itself must be quotable.
    check("accepts the rating number",
          validate("Lake Worth Inlet is rated 8.2 out of 10 right now. It is mostly "
                   "clear and 88 degrees with light wind. Swim near a lifeguard.",
                   BEACH)[0], True)


def test_trimming():
    print("Sentence trimming")
    check("drops a mid-clause fragment",
          _trim_to_sentences("One good sentence. Another one. And this one was cut"),
          "One good sentence. Another one.")
    # This was a real defect: splitting on bare "." broke "8.2" into "8." + "2",
    # which then failed the invented-number check on a correctly-copied number.
    check("does NOT split decimals",
          _trim_to_sentences("The rating is 8.2 out of 10 today."),
          "The rating is 8.2 out of 10 today.")
    check("strips markdown headings",
          _trim_to_sentences("**Beach Conditions Today**\n\nIt is warm and clear."),
          "It is warm and clear.")
    check("caps sentence count",
          _trim_to_sentences("A one. B two. C three. D four. E five.", max_sentences=2),
          "A one. B two.")


def test_retrieval():
    print("Knowledge-base retrieval")
    snips = retrieve(BEACH)
    check("supported beach retrieves 5 snippets", len(snips), 5)
    check("rip guidance comes first (safety before comfort)",
          snips[0].startswith("Rip current risk is low"), True)

    # Unsupported cameras must not get sargassum/water claims.
    unsupported = dict(BEACH, supported=False, sargassum_label=None, water_severity=None)
    text = " ".join(retrieve(unsupported))
    check("no sargassum claim on an unsupported beach", "sargassum" in text.lower(), False)

    high = dict(BEACH, rip_risk="high")
    check("high risk retrieves the strongest wording",
          "not advised" in " ".join(retrieve(high)), True)


def test_uv_band():
    print("UV banding (must mirror score.js uvIndexToLabel)")
    for uv, want in [(0, "Low"), (2, "Low"), (3, "Moderate"), (5, "Moderate"),
                     (6, "High"), (7, "High"), (8, "Very High"), (10, "Very High"),
                     (11, "Extreme"), (12, "Extreme")]:
        check(f"UV {uv} -> {want}", uv_band(uv), want)
    check("None -> None", uv_band(None), None)


def test_fingerprint():
    print("Cache fingerprint")
    check("stable for identical input", fingerprint(BEACH), fingerprint(dict(BEACH)))
    check("ignores sub-degree noise (would thrash the cache)",
          fingerprint(BEACH), fingerprint(dict(BEACH, temp_f=88.04)))
    check("changes when the rating changes",
          fingerprint(BEACH) != fingerprint(dict(BEACH, index=6.1)), True)
    check("changes when rip risk changes",
          fingerprint(BEACH) != fingerprint(dict(BEACH, rip_risk="high")), True)


def test_fallback_is_always_safe():
    print("Deterministic fallback")
    text = fallback_summary(BEACH)
    check("fallback passes its own validator", validate(text, BEACH)[0], True)
    check("fallback mentions the lifeguard guidance", "lifeguard" in text, True)

    high = dict(BEACH, rip_risk="high")
    check("fallback carries the high-risk warning",
          "not advised" in fallback_summary(high), True)

    # A beach with almost nothing known must still produce something sane.
    sparse = dict(name="Somewhere", index=None, supported=False, rip_risk="low")
    check("sparse beach still yields text", len(fallback_summary(sparse)) > 20, True)


def test_prompt_contains_facts():
    print("Prompt construction")
    p = build_prompt(BEACH)
    check("prompt carries the rating", "8.2" in p, True)
    check("prompt carries the temperature", "88" in p, True)
    check("prompt forbids inventing numbers", "Never state a number" in p, True)
    check("prompt forbids safety downgrades", "no risk" in p, True)


if __name__ == "__main__":
    for t in (test_rejects_real_hallucinations, test_accepts_good_text, test_trimming,
              test_retrieval, test_uv_band, test_fingerprint,
              test_fallback_is_always_safe, test_prompt_contains_facts):
        t()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("all summarize.py tests passed")
