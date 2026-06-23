"""The RULER — walk-forward, out-of-sample scoreboard for daily-max forecast methods at a fixed lead.

Reads `data/leadtime_residuals.jsonl` (from build_leadtime_dataset.py) and, per lead, scores each method
on a ROLLING 30-day train → next-day test split PER STATION (mirroring the live FAIR_WINDOW=30 calibration),
then aggregates the test points. This is the go/no-go gate: a new method (e.g. the generative MOS in
eval — added as another `predict_*`) must BEAT these baselines on the BUCKET metric out-of-sample.

Methods (each yields a predictive (mu, sigma) for the day's integer °C max):
  raw            mu = forecast_peak,            sigma = train residual std        (no correction)
  deterministic  current live calibration: regime-shrunk bias + regime-σ ×1.10   (residuals.fair_calibration)
  linear_mos     OLS actual ~ forecast_peak on the window, sigma = fit resid std
  persistence    mu = yesterday's actual max,   sigma = train day-over-day std    (naive baseline)

Metrics (lower is better except std_z→1):
  MAE / RMSE   of the point prediction mu
  CRPS         Gaussian closed form — the proper score for the whole predictive distribution
  bucket_LL    −log P(realized integer bucket) under N(mu,sigma) via residuals.bucket_fair_yes (the trade target)
  std_z        std of (actual−mu)/sigma — calibration; 1.0 = honest, <1 over-spread, >1 OVERCONFIDENT

  uv run python maintain/eval_leadtime.py [--data PATH] [--window 30] [--min-train 14]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
from collections import defaultdict

from polyweather.realized.feature_store import DATA_DIR
from polyweather.realized.residuals import (
    FAIR_MIN_N, POOLED_SIGMA, REGIME_MIN_N, REGIME_PRIOR_N, REGIME_SIGMA_MIN_N,
    SIGMA_FLOOR, SIGMA_INFLATE, bucket_fair_yes,
)

DATA = DATA_DIR / "leadtime_residuals.jsonl"
SQRT2 = math.sqrt(2.0)
INV_SQRT_PI = 1.0 / math.sqrt(math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _crps_gauss(mu: float, sigma: float, y: float) -> float:
    """Closed-form CRPS of N(mu, sigma) against observation y."""
    sigma = max(sigma, 1e-6)
    z = (y - mu) / sigma
    return sigma * (z * (2.0 * _cdf(z) - 1.0) + 2.0 * _pdf(z) - INV_SQRT_PI)


# ---- methods: each returns (mu, sigma) from a train window + the test row's forecast_peak ----

def predict_raw(train: list[dict], fp: float, regime: str | None) -> tuple[float, float]:
    res = [r["residual"] for r in train]
    sigma = max(st.stdev(res), SIGMA_FLOOR) if len(res) >= FAIR_MIN_N else POOLED_SIGMA
    return fp, sigma * SIGMA_INFLATE


def predict_deterministic(train: list[dict], fp: float, regime: str | None) -> tuple[float, float]:
    """Replicate residuals.fair_calibration on the train slice (regime-shrunk bias + regime σ, ×1.10)."""
    res = [r["residual"] for r in train]
    s_bias = st.mean(res)
    s_sigma = max(st.stdev(res), SIGMA_FLOOR) if len(res) >= FAIR_MIN_N else POOLED_SIGMA
    bias, sigma = s_bias, s_sigma
    matched = [r["residual"] for r in train if regime and r.get("regime") == regime]
    if len(matched) >= REGIME_MIN_N:
        w = len(matched) / (len(matched) + REGIME_PRIOR_N)
        bias = w * st.mean(matched) + (1.0 - w) * s_bias
        if len(matched) >= REGIME_SIGMA_MIN_N:
            r_sigma = max(st.stdev(matched), SIGMA_FLOOR)
            sigma = w * r_sigma + (1.0 - w) * s_sigma
    return fp - bias, sigma * SIGMA_INFLATE


def predict_linear_mos(train: list[dict], fp: float, regime: str | None) -> tuple[float, float]:
    xs = [r["forecast_peak"] for r in train]
    ys = [float(r["actual_max"]) for r in train]
    if len(xs) < FAIR_MIN_N or st.pvariance(xs) < 1e-6:
        return predict_deterministic(train, fp, regime)
    mx, my = st.mean(xs), st.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
    slope = cov / st.pvariance(xs)
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    sigma = max(st.stdev(resid), SIGMA_FLOOR) if len(resid) >= FAIR_MIN_N else POOLED_SIGMA
    return intercept + slope * fp, sigma * SIGMA_INFLATE


def predict_persistence(train: list[dict], prev_actual: float | None,
                        fp: float, regime: str | None) -> tuple[float, float] | None:
    if prev_actual is None:
        return None
    diffs = [float(train[i]["actual_max"]) - float(train[i - 1]["actual_max"]) for i in range(1, len(train))]
    sigma = max(st.stdev(diffs), SIGMA_FLOOR) if len(diffs) >= FAIR_MIN_N else POOLED_SIGMA
    return prev_actual, sigma * SIGMA_INFLATE


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--window", type=int, default=30, help="rolling train window (days)")
    ap.add_argument("--min-train", type=int, default=14, help="min train rows before scoring a day")
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(args.data)]
    by_sl: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        by_sl[(r["station"], r["lead_days"])].append(r)
    for v in by_sl.values():
        v.sort(key=lambda r: r["date"])

    # metric accumulators: lead → method → metric → list
    acc: dict[int, dict[str, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list)))
    METHODS = ["raw", "deterministic", "linear_mos", "persistence"]

    for (station, lead), srows in by_sl.items():
        for i in range(len(srows)):
            train = srows[max(0, i - args.window):i]
            if len(train) < args.min_train:
                continue
            test = srows[i]
            fp, y, regime = test["forecast_peak"], float(test["actual_max"]), test.get("regime")
            prev_actual = float(srows[i - 1]["actual_max"]) if i > 0 else None
            preds = {
                "raw": predict_raw(train, fp, regime),
                "deterministic": predict_deterministic(train, fp, regime),
                "linear_mos": predict_linear_mos(train, fp, regime),
                "persistence": predict_persistence(train, prev_actual, fp, regime),
            }
            for m, pr in preds.items():
                if pr is None:
                    continue
                mu, sigma = pr
                sigma = max(sigma, 1e-6)
                a = acc[lead][m]
                a["mae"].append(abs(mu - y))
                a["rmse2"].append((mu - y) ** 2)
                a["crps"].append(_crps_gauss(mu, sigma, y))
                p_bucket = bucket_fair_yes("==", round(y), mu, sigma)
                a["bucket_ll"].append(-math.log(max(p_bucket, 1e-6)))
                a["z"].append((y - mu) / sigma)

    print(f"data={args.data}  rolling window={args.window}d  min_train={args.min_train}")
    for lead in sorted(acc):
        n = len(acc[lead]["deterministic"]["mae"])
        print(f"\n=== lead {lead*24}h · {n} OOS test-days ===")
        print(f"{'method':14} {'MAE':>6} {'RMSE':>6} {'CRPS':>6} {'bucketLL':>9} {'std_z':>7}")
        for m in METHODS:
            a = acc[lead][m]
            if not a["mae"]:
                continue
            mae = st.mean(a["mae"])
            rmse = math.sqrt(st.mean(a["rmse2"]))
            crps = st.mean(a["crps"])
            bll = st.mean(a["bucket_ll"])
            sz = st.pstdev(a["z"]) if len(a["z"]) > 1 else float("nan")
            print(f"{m:14} {mae:>6.3f} {rmse:>6.3f} {crps:>6.3f} {bll:>9.3f} {sz:>7.3f}")


if __name__ == "__main__":
    main()
