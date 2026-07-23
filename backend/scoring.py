from dataclasses import dataclass
from typing import Optional
import numpy as np

# index math: sub-scores + geometric-mean aggregate
"""
The parameters we have:

Temperature
Beach Sargassum Quality (only for Lake Worth and Boynton)
Crowd Size
Water Quality (Beta)
Suggest more features?
Card is colored by aggregate beach quality index

We get: Beach Quality Index (an index formulated from all the other parameters)
"""

WEIGHTS = {
    "weather":   0.28,   # temp/humidity/rain/wind combined comfort
    "sargassum": 0.20,   # not supported for every beach
    "rip":       0.16,   # safety 
    "water":     0.14,   # beta AI water-quality (weighed less due to it being a beta model)
    "crowd":     0.12,   # beta CV people count
    "uv":        0.10,   # sunburn risk
}


def sargassum_score(coverage_pct: float) -> tuple[float, str]:
    """
    Piecewise-linear interpolation over hand-anchored bands.
    ALTERNATIVE: hardcoded if/elif thresholds — simpler but stair-steps
    the score, which looks bad on a continuously-colored UI card."""
    anchors_x = [0.0, 0.3, 1.0, 1.5, 3.0, 100.0] #coverage percentages (more than 0.3% is considered mild)
    anchors_y = [10.0, 8.0, 5.5, 3.0, 0.0, 0.0] #relates to the scores between 0 and 10
    score = float(np.interp(coverage_pct, anchors_x, anchors_y))
    if coverage_pct < 0.3:  label = "Light"
    elif coverage_pct < 1.0: label = "Mild"
    elif coverage_pct < 1.5: label = "Moderate"
    else:                    label = "Heavy"
    return score, label

def weather_score(temp_f: float, humidity_pct: float, precip_prob: float,
                   wind_mph: float) -> float:
    """
    Comfort score. Note that rain is the dominant penalty (heavy rain alone should significantly lower the index).
    Humidity is included as a MINOR penalty on top of temp discomfort —
    high humidity makes heat feel worse (a crude heat-index proxy),
    not a separate independent factor.
    """
    s = 10.0
    s -= abs(np.clip(temp_f, 55, 100) - 81) * 0.12          # comfort peaks at 81F
    s -= max(0, humidity_pct - 60) * 0.03                    # sticky above 60%
    s -= (precip_prob / 100.0) * 6.0                         # rain (large weight)
    s -= max(0, wind_mph - 12) * 0.20                        # wind above breezy
    return float(np.clip(s, 0, 10))

def water_quality_score(severity_label: str) -> float:
    """
    Maps the beta water-severity classifier's label to a score. Still beta though.
    """
    mapping = {"light": 9.0, "mild": 7.0, "moderate": 4.5, "heavy": 2.0}
    return mapping.get(severity_label.lower(), 5.0)   # unknown label -> neutral

def crowd_score(people_count: int) -> tuple[float, str]:
    """
    fewer people leads to higher score as not many wants to go to a very crowded beach.
    """
    if people_count < 5:   return 9.0, "Not Busy"
    if people_count < 20:  return 7.0, "Moderate"
    if people_count < 50:  return 5.0, "Busy"
    return 3.0, "Very Busy"

def uv_score(uv_index: float) -> float:
    """EPA UV scale: 0-2 low, 3-5 moderate, 6-7 high, 8-10 very high, 11+ extreme.
    penalize above moderate; don't reward low UV heavily
    (a cloudy-and-miserable day shouldn't score well just because UV is low —
    weather_score already penalizes clouds/rain separately)."""
    return float(np.clip(10 - max(0, uv_index - 3) * 1.1, 0, 10))


def rip_current_score(risk_level: str) -> float:
    """risk_level from NWS surf-zone forecast: 'low' | 'moderate' | 'high'.
    'high' is penalized very harshly as it is a high safety risk.
    The aggregate weight (0.16) plus this steep
    curve should make risky-water days visibly bad even if sunny."""
    return {"low": 9.0, "moderate": 5.0, "high": 1.5}.get(risk_level.lower(), 6.0)

def wind_score(wind_mph: float) -> float:
    """Currently unused as it is already factored in the weather score"""
    return float(np.clip(10 - max(0, wind_mph - 10) * 0.25, 0, 10))


@dataclass
class Beach:
    #class vars
    name: str
    temp_f: float
    humidity_pct: float
    uv_index: float
    rip_risk: str                              # low, moderate, high
    precip_prob: float = 0.0
    wind_mph: float = 0.0
    sargassum_coverage_pct: Optional[float] = None   # None = beta/unsupported beach
    crowd_count: Optional[int] = None
    water_severity: Optional[str] = None

    def get_score(self) -> dict:
        """returns the full breakdown in a dictionary: aggregate index + every sub-score,
        which we will feed into the frontend. Missing inputs simply produce None sub-scores which the frontend will know,
        and aggregate_index renormalizes weights around what's present."""
        w = weather_score(self.temp_f, self.humidity_pct,
                          self.precip_prob, self.wind_mph)
        rip = rip_current_score(self.rip_risk)
        uv = uv_score(self.uv_index)

        sarg_score = sarg_label = None
        if self.sargassum_coverage_pct is not None:
            sarg_score, sarg_label = sargassum_score(self.sargassum_coverage_pct)

        water_score = None
        if self.water_severity is not None:
            water_score = water_quality_score(self.water_severity)

        crowd_s = crowd_label = None
        if self.crowd_count is not None:
            crowd_s, crowd_label = crowd_score(self.crowd_count)

        subscores = {
            "weather": w, "sargassum": sarg_score, "water": water_score,
            "crowd": crowd_s, "uv": uv, "rip": rip,
        }
        index = aggregate_index(subscores)

        return {
            "index": round(index, 1) if index is not None else None,
            "subscores": {k: (round(v, 1) if v is not None else None) 
                          for k, v in subscores.items()}, #note that subscores is a dictionary nested within the return dict
            "sargassum_label": sarg_label,
            "crowd_label": crowd_label,
        }


def aggregate_index(subscores: dict) -> Optional[float]:
    """Weighted GEOMETRIC mean over AVAILABLE signals only.
    DESIGN CHOICE: geometric (not arithmetic) mean — this was tested against
    3 candidate formulas. Arithmetic mean lets good signals prop up one bad
    one (heavy rain only dropped a perfect day to 6.8/10 in testing);
    geometric mean lets ANY weak signal drag the whole product down, which
    matches the stated requirement ('bad weather should tank the score even
    if everything else is perfect'). A weather-GATED multiplier (Option C
    from earlier testing) is a stronger alternative if you want weather to
    dominate even more aggressively than the natural geometric-mean effect.

    RENORMALIZATION: weights of missing (None) signals are excluded and the
    rest are rescaled to sum to 1 — otherwise a beta beach with 3 of 6
    signals would be unfairly penalized just for having less data, rather
    than being judged fairly on what IS known."""
    present = {k: v for k, v in subscores.items() if v is not None}
    if not present:
        return None
    total_w = sum(WEIGHTS[k] for k in present)
    prod = 1.0
    for k, v in present.items():
        w_norm = WEIGHTS[k] / total_w
        prod *= max(v, 0.5) ** w_norm     # floor at 0.5 avoids a single 0 collapsing the product to exactly 0
    return prod