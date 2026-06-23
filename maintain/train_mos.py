"""Phase 3 — the GENERATIVE MOS: a quantile gradient-boosting model that, given (NWP forecast peak + the
forecast's own met@peak + regime + season + station + lead), GENERATES the daily-max distribution. Trained
ONCE on the pre-cutoff history, then frozen and scored on a held-out future window against the live-style
baselines (rolling-30d deterministic calibration + linear MOS) on the SAME test days.

This is the go/no-go: the model must beat `deterministic` / `linear_mos` on bucketLL (the trade target)
out-of-sample. NOTE: HGBT is FROZEN at the cutoff (no retrain) while the baselines adapt every day — so this
is a PESSIMISTIC test (staleness handicaps the model); production would retrain periodically.

  uv run python maintain/train_mos.py [--cutoff 2026-01-01]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

sys.path.insert(0, "/Users/wekoiuk/polyweather/maintain")
import eval_leadtime as E  # noqa: E402  reuse metrics + baseline predictors
from polyweather.realized.feature_store import DATA_DIR  # noqa: E402
from polyweather.realized.residuals import SIGMA_FLOOR  # noqa: E402

REGIMES = {"clear": 0, "cloudy": 1, "rainy": 2, "mixed": 3, None: 4}
MET = ["cloud_pct", "precip_mm", "wind_kmh", "rh_pct", "shortwave_wm2"]  # gust absent in ECMWF prev-runs
# layout: peak(0) | 5×met(1-5) | sin(6) cos(7) | lead(8) | recent_bias(9) recent_sigma(10) | regime(11) station(12)
CAT_IDX = [11, 12]


def attach_recency(rows: list[dict], window: int = 30, min_n: int = 5) -> None:
    """Causal trailing residual mean/std per (station, lead) — the SAME recency signal the rolling
    baseline gets, so the model isn't handicapped. Uses only PAST settled days (known at predict time)."""
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r["station"], r["lead_days"])].append(r)
    for v in by.values():
        v.sort(key=lambda r: r["date"])
        for i, r in enumerate(v):
            prior = [float(p["residual"]) for p in v[max(0, i - window):i]]
            r["_rbias"] = st.mean(prior) if len(prior) >= min_n else float("nan")
            r["_rsigma"] = st.pstdev(prior) if len(prior) >= min_n else float("nan")


def features(rows: list[dict], scode: dict[str, int]) -> np.ndarray:
    X = []
    for r in rows:
        m = r.get("regime_met") or {}
        doy = date.fromisoformat(r["date"]).timetuple().tm_yday
        X.append([
            r["forecast_peak"],
            *[float(m[k]) if m.get(k) is not None else np.nan for k in MET],
            math.sin(2 * math.pi * doy / 365.25), math.cos(2 * math.pi * doy / 365.25),
            float(r["lead_days"]),
            r.get("_rbias", np.nan), r.get("_rsigma", np.nan),
            REGIMES.get(r.get("regime"), 4),
            scode[r["station"]],
        ])
    return np.array(X, dtype=float)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DATA_DIR / "leadtime_residuals.jsonl"))
    ap.add_argument("--cutoff", default="2026-01-01", help="train < cutoff, test >= cutoff")
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--min-train", type=int, default=14)
    ap.add_argument("--val-days", type=int, default=90, help="validation tail of train for σ-calibration")
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(args.data)]
    attach_recency(rows, window=args.window)
    scode = {s: i for i, s in enumerate(sorted({r["station"] for r in rows}))}
    train = [r for r in rows if r["date"] < args.cutoff]
    test = [r for r in rows if r["date"] >= args.cutoff]

    def fit_quantiles(fit_rows: list[dict]) -> dict:
        X = features(fit_rows, scode)
        y = np.array([float(r["actual_max"]) for r in fit_rows])
        ms = {}
        for q in (0.1, 0.5, 0.9):
            ms[q] = HistGradientBoostingRegressor(
                loss="quantile", quantile=q, max_iter=400, learning_rate=0.05, max_leaf_nodes=31,
                min_samples_leaf=40, categorical_features=CAT_IDX, random_state=0).fit(X, y)
        return ms

    def mu_sigma(ms: dict, rows_: list[dict], scale: float = 1.0) -> list[tuple[float, float]]:
        X = features(rows_, scode)
        p10, p50, p90 = ms[0.1].predict(X), ms[0.5].predict(X), ms[0.9].predict(X)
        return [(float(p50[i]), max((float(p90[i]) - float(p10[i])) / 2.563 * scale, SIGMA_FLOOR))
                for i in range(len(rows_))]  # q90−q10 = 2·1.2816·σ (Gaussian)

    # honest σ-calibration: carve a validation tail from TRAIN, learn the σ-scale there (never the test)
    val_cut = (date.fromisoformat(args.cutoff) - timedelta(days=args.val_days)).isoformat()
    fit_rows = [r for r in train if r["date"] < val_cut]
    val_rows = [r for r in train if r["date"] >= val_cut]
    vms = mu_sigma(fit_quantiles(fit_rows), val_rows)
    zs = [(float(r["actual_max"]) - mu) / sig for r, (mu, sig) in zip(val_rows, vms)]
    sigma_scale = st.pstdev(zs) if len(zs) > 1 else 1.0

    # refit on full train for the test μ, apply the validation-learned σ-scale
    test_ms = mu_sigma(fit_quantiles(train), test, scale=sigma_scale)
    hgbt: dict[tuple, tuple[float, float]] = {
        (r["station"], r["date"], r["lead_days"]): ms for r, ms in zip(test, test_ms)}
    print(f"σ-calibration scale = {sigma_scale:.3f} (learned on {len(val_rows)} val rows ≥ {val_cut})")

    by_sl: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_sl[(r["station"], r["lead_days"])].append(r)
    for v in by_sl.values():
        v.sort(key=lambda r: r["date"])

    acc: dict[int, dict[str, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list)))
    METHODS = ["deterministic", "linear_mos", "generative_mos"]
    for (station, lead), srows in by_sl.items():
        for i in range(len(srows)):
            t = srows[i]
            if t["date"] < args.cutoff:
                continue
            train_w = srows[max(0, i - args.window):i]
            if len(train_w) < args.min_train:
                continue
            fp, y, regime = t["forecast_peak"], float(t["actual_max"]), t.get("regime")
            preds = {
                "deterministic": E.predict_deterministic(train_w, fp, regime),
                "linear_mos": E.predict_linear_mos(train_w, fp, regime),
                "generative_mos": hgbt.get((station, t["date"], lead)),
            }
            for mth, pr in preds.items():
                if pr is None:
                    continue
                mu, sigma = pr
                sigma = max(sigma, 1e-6)
                a = acc[lead][mth]
                a["mae"].append(abs(mu - y))
                a["rmse2"].append((mu - y) ** 2)
                a["crps"].append(E._crps_gauss(mu, sigma, y))
                a["bll"].append(-math.log(max(E.bucket_fair_yes("==", round(y), mu, sigma), 1e-6)))
                a["z"].append((y - mu) / sigma)

    print(f"train<{args.cutoff} ({len(train)} rows) → frozen → test>={args.cutoff} ({len(test)} rows)")
    for lead in sorted(acc):
        n = len(acc[lead]["deterministic"]["mae"])
        print(f"\n=== lead {lead*24}h · {n} OOS holdout test-days ===")
        print(f"{'method':16}{'MAE':>7}{'RMSE':>7}{'CRPS':>7}{'bucketLL':>10}{'std_z':>8}")
        base = None
        for mth in METHODS:
            a = acc[lead][mth]
            if not a["mae"]:
                continue
            bll = st.mean(a["bll"])
            base = base or bll
            tag = "" if mth != "generative_mos" else f"   ({(base-bll)/base*100:+.1f}% bucketLL vs determ.)"
            print(f"{mth:16}{st.mean(a['mae']):>7.3f}{math.sqrt(st.mean(a['rmse2'])):>7.3f}"
                  f"{st.mean(a['crps']):>7.3f}{bll:>10.3f}{st.pstdev(a['z']):>8.3f}{tag}")


if __name__ == "__main__":
    main()
