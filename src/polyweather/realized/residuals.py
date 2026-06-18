"""Read the rolling forecast residuals (data/forecast_residuals.jsonl, written by maintain/build_residuals.py)
→ per-city RECENT forecast bias over 3/7/14/30-day windows, handed to the decision agent as a soft, seasonally
-adaptive reference. `residual = forecast_peak − actual_max`; a NEGATIVE mean = the forecast has been running
LOW (the real max came in higher) → lean a higher bucket. Graceful: no file / no rows → None."""
from __future__ import annotations

import json
import math
import statistics

from .feature_store import DATA_DIR

RESIDUALS = DATA_DIR / "forecast_residuals.jsonl"
WINDOWS = (3, 7, 14, 30)
POOLED_SIGMA = 2.0     # fallback forecast-error std (°C) when a station has too few residuals for its own
FAIR_MIN_N = 8         # need this many residuals for a per-station sigma; else fall back to POOLED_SIGMA
SIGMA_FLOOR = 0.5      # never trust a tighter error than this — a small sample looks falsely over-confident
FAIR_WINDOW = 30       # use the last N residuals for the fair-value sigma/bias (recent ≈ seasonal)
REGIME_MIN_N = 4       # fewer matching days are shown to the AI but never drive the farmer's hard fair value
REGIME_PRIOR_N = 6     # shrink conditional bias toward the all-weather station mean


def _load(station: str) -> list[dict]:
    """This station's residual rows, oldest→newest. Corrupt/foreign/malformed lines are skipped."""
    if not RESIDUALS.exists():
        return []
    rows = []
    for line in RESIDUALS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (r.get("station") == station and r.get("date")
                and isinstance(r.get("residual"), (int, float))):
            rows.append(r)
    rows.sort(key=lambda r: r["date"])
    return rows


def _stats(rows: list[dict]) -> dict | None:
    """Detailed, compact residual summary suitable for logs and an LLM fact packet."""
    xs = [float(r["residual"]) for r in rows]
    if not xs:
        return None
    variance = statistics.variance(xs) if len(xs) > 1 else None
    return {
        "n": len(xs), "mean": round(statistics.mean(xs), 2),
        "median": round(statistics.median(xs), 2),
        "variance": (round(variance, 3) if variance is not None else None),
        "sd": (round(math.sqrt(variance), 3) if variance is not None else None),
        "mae": round(statistics.mean(abs(x) for x in xs), 2),
        "rmse": round(math.sqrt(statistics.mean(x * x for x in xs)), 2),
        "under_rate": round(sum(x < 0 for x in xs) / len(xs), 3),
        "over_rate": round(sum(x > 0 for x in xs) / len(xs), 3),
        "lo": min(xs), "hi": max(xs),
    }


def recent_bias(station: str, *, regime: str | None = None) -> dict | None:
    """Detailed rolling residual statistics, including peak-weather-regime conditioning.

    `residual = forecast - actual`: negative means Open-Meteo ran low. Regime rows are reported even
    when thin so the AI can see the evidence count; the farmer applies a stricter shrinkage rule below.
    """
    rows = _load(station)
    if not rows:
        return None
    out = {}
    for w in WINDOWS:
        stats = _stats(rows[-w:])
        if stats:
            out[f"d{w}"] = stats
    recent = rows[-FAIR_WINDOW:]
    grouped: dict[str, list[dict]] = {}
    for row in recent:
        name = row.get("regime")
        if isinstance(name, str) and name:
            grouped.setdefault(name, []).append(row)
    out["by_regime"] = {name: stats for name, rs in sorted(grouped.items())
                        if (stats := _stats(rs)) is not None}
    out["current_regime"] = regime
    out["matched_regime"] = _stats(grouped.get(regime, [])) if regime else None
    return out or None


def fair_calibration(station: str, *, regime: str | None = None,
                     window: int = FAIR_WINDOW) -> dict | None:
    """Conservative station calibration, optionally conditioned on the forecast peak regime.

    A matching regime needs at least REGIME_MIN_N observations. Its mean is then partially pooled with
    the all-weather station mean, preventing a handful of clear/rainy days from producing a large hard
    correction. Sigma remains the all-weather station error scale until regime samples are much deeper.
    """
    rows = _load(station)[-window:]
    if not rows:
        return None
    xs = [float(r["residual"]) for r in rows]
    station_bias = statistics.mean(xs)
    if len(xs) >= FAIR_MIN_N:
        sigma = max(statistics.stdev(xs), SIGMA_FLOOR)
    else:
        sigma = POOLED_SIGMA

    matched = [r for r in rows if regime and r.get("regime") == regime]
    bias, source, weight = station_bias, "station", 0.0
    regime_bias = statistics.mean(float(r["residual"]) for r in matched) if matched else None
    if len(matched) >= REGIME_MIN_N and regime_bias is not None:
        weight = len(matched) / (len(matched) + REGIME_PRIOR_N)
        bias = weight * regime_bias + (1.0 - weight) * station_bias
        source = "regime_shrunk"
    return {"sigma": round(sigma, 3), "bias": round(bias, 3), "source": source,
            "station_n": len(rows), "station_bias": round(station_bias, 3),
            "regime": regime, "regime_n": len(matched),
            "regime_bias": (round(regime_bias, 3) if regime_bias is not None else None),
            "regime_weight": round(weight, 3)}


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bucket_fair_yes(comparator: str, threshold: int, mu: float, sigma: float) -> float:
    """Calibrated P(this temperature bucket resolves YES) given the bias-corrected expected daily max `mu` and the
    forecast-error std `sigma`. The high rounds to an integer °C, so the bucket boundaries are half-degree:
    ==N ⟺ high∈[N−0.5, N+0.5); >=N ⟺ high ≥ N−0.5; <=N ⟺ high < N+0.5."""
    sigma = max(sigma, 1e-6)
    lo = _norm_cdf((threshold - 0.5 - mu) / sigma)     # P(high < N−0.5)
    hi = _norm_cdf((threshold + 0.5 - mu) / sigma)     # P(high < N+0.5)
    if comparator == "==":
        return hi - lo
    if comparator == ">=":
        return 1.0 - lo
    return hi                                          # "<="
