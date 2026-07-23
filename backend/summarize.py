"""
summarize.py — plain-language conditions summary from a local, free LLM.

MODEL: google/flan-t5-small (80M params, ~300MB, Apache 2.0). Runs on CPU in
about a second. Downloads once from the HF hub on first use, then works fully
offline from the local cache. No API key, no per-token cost, no data leaving
the machine.

WHY A SMALL INSTRUCTION-TUNED MODEL RATHER THAN A CHAT MODEL. Everything the
summary should say is already known — the numbers, the labels, the safety
guidance are all computed upstream. The model's only job is to phrase supplied
facts fluently. flan-t5 is trained for exactly that shape of task, and being
small and non-conversational makes it markedly less prone to volunteering
invented specifics than a general chat model of similar size.

RETRIEVAL. The "R" in RAG here is a lookup over a small curated knowledge base
keyed by the categories the backend ALREADY computed (rip risk, sargassum band,
UV band, crowd tier, water severity). This is deliberately not an embedding
search: the query isn't free text, it's a handful of enum values, so a dict
lookup is both exact and dependency-free. Embeddings would add a second model
and a similarity threshold to tune, in exchange for fuzzier matching of
something that was never fuzzy.

SAFETY POSTURE. The generated paragraph is supplementary. Authoritative safety
information (the rip current advisory) is rendered separately by the frontend
and does not depend on this module. If generation fails, the app shows
everything else and simply omits the paragraph.
"""

import hashlib
import json
from typing import Optional

MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"

# Generation is capped short: this is a caption, not an essay, and a small
# model rambles when given room.
MAX_NEW_TOKENS = 120

# ─────────────────────────────────────────────────────────────────────────
# WHY THERE IS A VALIDATOR BELOW  (measured, not hypothetical)
# ─────────────────────────────────────────────────────────────────────────
# Four local models were tried on real payloads from this app. Every one of
# them invented content, and three produced SAFETY-RELEVANT errors:
#
#   flan-t5-small   echoed the prompt back verbatim; no prose at all.
#   flan-t5-base    "If you're looking for a beach with a lot of people, this
#                   is a good place to go."  — inverted a "moderate crowd" note
#                   into a recommendation.
#   SmolLM2-360M    invented "temperatures ranging from 88F to 90F" (90 appears
#                   nowhere), called sargassum "an extra layer of charm", and
#                   dropped the rip current warning entirely.
#   Qwen2.5-0.5B    "There is no visible rip current risk" — the input said risk
#                   is LOW *and* that rip currents can still form near jetties.
#
# That last one is the whole problem in one sentence: a fluent model will
# happily turn "low risk, stay careful" into "no risk", directly beside safety
# information. Models this small cannot be trusted to paraphrase unsupervised.
#
# So generation is treated as UNTRUSTED OUTPUT and machine-checked before it is
# ever shown: every number must trace back to the input, and phrases that
# reverse a safety meaning are rejected. Anything failing falls back to a
# deterministic template built from the same curated snippets. The LLM can
# improve the prose; it cannot introduce a fact or downgrade a warning.

# Numbers are the easiest hallucination to catch mechanically: any digit in the
# output that wasn't in the input is invented (this also rejects unit
# conversions like "31C", which are plausible but unverifiable here).
_NUM_RE = None  # compiled lazily in _validate to keep import cheap

# Phrases that assert an absence of danger. None of these can be justified by
# our inputs, which only ever describe risk as low/moderate/high — never absent.
FORBIDDEN_PATTERNS = [
    # Absence-of-danger claims. Our inputs only ever say low/moderate/high.
    #
    # These allow up to three words between the negation and the noun, because
    # exact phrases keep getting outflanked. "no danger" was in this list and
    # still let through "There is no immediate danger from rip currents" —
    # observed live on Boca — since "immediate" sat in the gap.
    r"\bno\b(?:\W+\w+){0,3}?\W+(?:danger|risk|hazard|threat|rip current)",
    r"\bnot\b(?:\W+\w+){0,2}?\W+(?:dangerous|risky|hazardous|a concern)",
    r"\bno significant\b",
    r"\bminimal\b(?:\W+\w+){0,2}?\W+(?:rip current|risk|danger)",
    r"\blittle\b(?:\W+\w+){0,2}?\W+(?:risk|danger)",
    # Any assertion of safety at all. The knowledge base NEVER uses the word
    # "safe" — it only ever gives cautions — so any occurrence is the model
    # editorialising. Caught live on Jupiter: "The UV index is 2, making it
    # safe for extended periods without sunscreen", which slipped past the
    # narrower "sun protection is unnecessary" rule. A blanket ban is easier to
    # reason about than chasing each phrasing.
    r"\bsafe\b",
    r"\bsafely\b",
    r"\bnothing to worry about\b",
    r"without (?:sunscreen|sun protection)",
    # Downgrading sun protection. The KB's weakest wording is "not urgent";
    # "unnecessary" is a stronger claim than any input supports.
    r"\bsun ?(?:screen|protection) (?:is )?unnecessary\b",
    r"\bno need for (?:sunscreen|sun protection)\b",
    r"\bdon'?t need sun",
    # Invented causal links between unrelated readings. Observed live:
    # "Wind at 13 mph suggests minimal rip currents" and "wind ... resulting in
    # an average UV index of 11". Neither relationship exists in the data.
    r"wind[^.]{0,40}(?:suggest|result|mean)[^.]{0,30}(?:rip|uv)",
    r"(?:rip|uv)[^.]{0,40}(?:because|due to)[^.]{0,20}wind",
]


# ─────────────────────────────────────────────────────────────────────────
# KNOWLEDGE BASE
# ─────────────────────────────────────────────────────────────────────────
# Short, factual, human-written snippets. Retrieved by category, injected into
# the prompt as context. Keep these accurate and conservative — they are the
# only domain knowledge the model has, and anything here can end up in front of
# a user deciding whether to swim.

KNOWLEDGE = {
    "rip": {
        "low": "Rip current risk is low today, but rip currents can form at any "
               "time near jetties and piers. Swim near a lifeguard.",
        "moderate": "Moderate rip current risk means channels of fast-moving water "
                    "can pull swimmers away from shore. If caught, swim parallel "
                    "to the beach to escape the current.",
        "high": "High rip current risk is dangerous even for strong swimmers. "
                "Entering the water is not advised.",
    },
    "sargassum": {
        "Light": "Only traces of sargassum seaweed are on the sand.",
        "Mild": "There is some sargassum seaweed on the sand, but most of the "
                "beach is clear.",
        "Moderate": "A noticeable amount of sargassum seaweed is on the beach. "
                    "Decomposing sargassum can smell of sulphur and may irritate "
                    "the eyes and throat.",
        "Heavy": "Heavy sargassum seaweed covers much of the beach. Decomposing "
                 "sargassum smells strongly and can irritate the airways, "
                 "especially for people with asthma.",
    },
    "uv": {
        "Low": "UV levels are low, so sun protection is not urgent.",
        "Moderate": "UV levels are moderate. Sunscreen is sensible for a long stay.",
        "High": "UV is high. Use sunscreen and seek shade around midday.",
        "Very High": "UV is very high. Unprotected skin can burn quickly; cover up "
                     "and reapply sunscreen often.",
        "Extreme": "UV is extreme. Unprotected skin burns within minutes. Avoid "
                   "being outside in the middle of the day.",
    },
    "crowd": {
        "Not Busy": "The beach looks quiet, with plenty of open sand.",
        "Moderate": "The beach has a moderate number of people.",
        "Busy": "The beach is busy, so parking and space may be limited.",
        "Very Busy": "The beach is very crowded.",
    },
    "water": {
        "clear": "The water looks clear.",
        "light": "The water looks slightly discoloured near the shoreline.",
        "moderate": "The water looks noticeably discoloured or turbid.",
        "severe": "The water looks heavily discoloured.",
    },
}


def uv_band(uv_index: Optional[float]) -> Optional[str]:
    """EPA UV index -> category. MUST match uvIndexToLabel() in
    frontend/src/score.js, or the page will show one word and the paragraph
    another for the same reading."""
    if uv_index is None:
        return None
    if uv_index <= 2:  return "Low"
    if uv_index <= 5:  return "Moderate"
    if uv_index <= 7:  return "High"
    if uv_index <= 10: return "Very High"
    return "Extreme"


def retrieve(beach: dict) -> list[str]:
    """Snippets relevant to THIS beach's current categories.

    Order matters: safety first, then comfort. The prompt is short enough that
    what comes first meaningfully shapes what the model leads with.
    """
    out = []
    if (rip := beach.get("rip_risk")) and rip in KNOWLEDGE["rip"]:
        out.append(KNOWLEDGE["rip"][rip])
    if (uvb := uv_band(beach.get("uv_index"))) and uvb in KNOWLEDGE["uv"]:
        out.append(KNOWLEDGE["uv"][uvb])
    # Sargassum and water only mean something on the validated cameras.
    if beach.get("supported"):
        if (s := beach.get("sargassum_label")) and s in KNOWLEDGE["sargassum"]:
            out.append(KNOWLEDGE["sargassum"][s])
        if (w := beach.get("water_severity")) and w in KNOWLEDGE["water"]:
            out.append(KNOWLEDGE["water"][w])
    if (c := beach.get("crowd_label")) and c in KNOWLEDGE["crowd"]:
        out.append(KNOWLEDGE["crowd"][c])
    return out


# ─────────────────────────────────────────────────────────────────────────
# PROMPT + FINGERPRINT
# ─────────────────────────────────────────────────────────────────────────

def _facts(beach: dict) -> list[str]:
    f = []
    if beach.get("temp_f") is not None:
        f.append(f"air temperature {round(beach['temp_f'])} degrees Fahrenheit")
    if beach.get("water_temp_f") is not None:
        f.append(f"water temperature {round(beach['water_temp_f'])} degrees")
    if beach.get("short_forecast"):
        f.append(f"sky {beach['short_forecast'].lower()}")
    if beach.get("wind_mph") is not None:
        f.append(f"wind {round(beach['wind_mph'])} miles per hour")
    if beach.get("uv_index") is not None:
        f.append(f"UV index {round(beach['uv_index'])}")
    if beach.get("next_tide"):
        f.append(f"next tide {beach['next_tide'].lower()}")
    return f


def build_prompt(beach: dict) -> str:
    snippets = retrieve(beach)
    facts = _facts(beach)
    rating = beach.get("index")
    rating_txt = f"The overall beach rating is {rating} out of 10." if rating is not None else ""

    # Constraints are stated as hard rules because the validator enforces them
    # anyway — every rule the model follows is one fewer rejection and one more
    # chance the nicer prose actually ships.
    return (
        "You are writing a short beach conditions note for visitors.\n"
        "Rules:\n"
        "- Two or three sentences only.\n"
        "- Use ONLY the facts below. Never state a number that is not listed.\n"
        "- Do not convert units.\n"
        "- Never say conditions are safe, or that there is no risk.\n"
        "- Keep any warning as strong as it is written below.\n\n"
        f"Beach: {beach.get('name', 'this beach')}. {rating_txt}\n"
        f"Facts: {', '.join(facts)}.\n"
        f"Guidance: {' '.join(snippets)}\n\n"
        "Now write the note:"
    )


def fingerprint(beach: dict) -> str:
    """Stable hash of everything the summary depends on.

    Only the inputs that can CHANGE THE TEXT are included, and floats are
    rounded — otherwise a 0.01-degree temperature wobble on every weather
    refresh would invalidate the cache and re-run the model constantly, which
    is precisely the cost this cache exists to avoid.
    """
    material = {
        "name": beach.get("name"),
        "index": round(beach["index"], 1) if beach.get("index") is not None else None,
        "temp": round(beach["temp_f"]) if beach.get("temp_f") is not None else None,
        "water_temp": round(beach["water_temp_f"]) if beach.get("water_temp_f") is not None else None,
        "wind": round(beach["wind_mph"]) if beach.get("wind_mph") is not None else None,
        "uv": round(beach["uv_index"]) if beach.get("uv_index") is not None else None,
        "forecast": beach.get("short_forecast"),
        "rip": beach.get("rip_risk"),
        "sarg": beach.get("sargassum_label"),
        "crowd": beach.get("crowd_label"),
        "water": beach.get("water_severity"),
        "tide": beach.get("next_tide"),
    }
    blob = json.dumps(material, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────
# MODEL
# ─────────────────────────────────────────────────────────────────────────

def load_summarizer():
    """Load tokenizer + model as a plain (tok, model) pair.

    NOT transformers.pipeline(...): the "text2text-generation" task was removed
    from the pipeline registry in transformers 5.x, so that call raises
    KeyError on a current install. Driving the model directly is a couple more
    lines and is stable across both 4.x and 5.x.

    Raises on failure — the caller decides whether that's fatal (it isn't;
    main.py logs it and the app serves everything else, falling back to the
    deterministic template).
    """
    from transformers import AutoTokenizer, AutoModelForCausalLM
    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForCausalLM.from_pretrained(MODEL_NAME)
    model.eval()
    return (tok, model)


def _numbers_in(text: str) -> set:
    global _NUM_RE
    import re
    if _NUM_RE is None:
        _NUM_RE = re.compile(r"\d+(?:\.\d+)?")
    return {n.rstrip(".").rstrip("0").rstrip(".") if "." in n else n
            for n in _NUM_RE.findall(text)}


def _trim_to_sentences(text: str, max_sentences: int = 4) -> str:
    """Cut to whole sentences.

    The token budget reliably lands mid-word ("...can irritate"), and a summary
    that stops in the middle of a clause looks broken next to safety text.
    Dropping the trailing fragment is always safe: it can only remove
    information, never add a claim.
    """
    import re
    # Chat-tuned models like to answer with a markdown heading
    # ("**Beach Conditions Today**\n\n..."). The frontend renders plain text, so
    # that would show up as literal asterisks. Strip emphasis markers and drop
    # a leading heading line before doing anything else.
    text = re.sub(r"[*_`#]+", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1 and not lines[0].rstrip().endswith((".", "!", "?")):
        lines = lines[1:]          # a title, not a sentence
    text = " ".join(lines)

    # Split only where punctuation is FOLLOWED BY WHITESPACE. Splitting on the
    # bare "." also splits decimals: "rating of 8.2" became "8." + "2", which
    # then tripped the invented-number check on a number the model had actually
    # copied correctly. The validator was right; the trimmer was wrong.
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    # Drop a trailing fragment with no terminal punctuation — that's the
    # token budget cutting mid-clause.
    while parts and not parts[-1].rstrip().endswith((".", "!", "?")):
        parts.pop()
    if not parts:
        return text.strip()
    return " ".join(parts[:max_sentences]).strip()


def validate(text: str, beach: dict) -> tuple[bool, str]:
    """Is this generated text safe to show? Returns (ok, reason).

    Pure function — unit-tested in test_summarize.py against the exact
    hallucinations observed from real models.
    """
    import re

    if not text or len(text) < 40:
        return False, "too short"
    if len(text) > 700:
        return False, "too long"

    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return False, f"forbidden safety phrase: {pat}"

    # Every number in the output must appear in the facts we supplied.
    allowed = _numbers_in(build_prompt(beach))
    for n in _numbers_in(text):
        if n not in allowed:
            return False, f"invented number: {n}"

    # Guard against the model simply echoing our scaffolding back.
    if text.lower().startswith(("beach:", "conditions:", "notes:", "paragraph:")):
        return False, "echoed the prompt"

    return True, "ok"


def generate_summary(beach: dict, summarizer) -> Optional[str]:
    """A validated paragraph, or the deterministic fallback.

    Never returns unvalidated model output — see the block comment at the top
    of this module for the measured reasons why.
    """
    import torch

    try:
        tok, model = summarizer
        messages = [{"role": "user", "content": build_prompt(beach)}]
        prompt = tok.apply_chat_template(messages, tokenize=False,
                                         add_generation_prompt=True)
        inputs = tok(prompt, return_tensors="pt", truncation=True, max_length=1024)
        with torch.inference_mode():
            ids = model.generate(
                **inputs,
                max_new_tokens=MAX_NEW_TOKENS,
                do_sample=False,           # deterministic: same inputs -> same
                                           # text, which is what makes the
                                           # fingerprint cache coherent
                repetition_penalty=1.1,
                pad_token_id=tok.eos_token_id,
            )
        text = tok.decode(ids[0][inputs["input_ids"].shape[1]:],
                          skip_special_tokens=True).strip()
        text = _trim_to_sentences(text)
    except Exception as e:
        print(f"[summarize] generation failed: {e}")
        return fallback_summary(beach)

    ok, reason = validate(text, beach)
    if not ok:
        print(f"[summarize] rejected model output ({reason}) — using template")
        return fallback_summary(beach)
    return text


def fallback_summary(beach: dict) -> str:
    """Deterministic template used when the model is unavailable or its output
    is unusable. Guarantees the section is never blank AND never wrong — it is
    assembled from the same retrieved snippets, just without the paraphrasing.
    """
    bits = []
    if beach.get("short_forecast") and beach.get("temp_f") is not None:
        bits.append(f"{beach['short_forecast']} and {round(beach['temp_f'])}°F")
    if beach.get("water_temp_f") is not None:
        bits.append(f"water around {round(beach['water_temp_f'])}°F")
    lead = f"{beach.get('name', 'This beach')}: {', '.join(bits)}." if bits else ""
    return " ".join([lead] + retrieve(beach)).strip()
