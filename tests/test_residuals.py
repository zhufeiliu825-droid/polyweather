"""Recent forecast-bias reader — residual = forecast − actual over 3/7/14/30d, fed to the decision agent."""
import json

from polyweather.realized import residuals as R


def _write(tmp_path, monkeypatch, rows):
    p = tmp_path / "forecast_residuals.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in rows))
    monkeypatch.setattr(R, "RESIDUALS", p)


def _write_leadtime(tmp_path, monkeypatch, rows):
    p = tmp_path / "leadtime_residuals.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in rows))
    monkeypatch.setattr(R, "LEADTIME", p)
    R._leadtime_index.cache_clear()   # cache is keyed on file mtime — drop it so the temp file is read


def test_no_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RESIDUALS", tmp_path / "nope.jsonl")
    assert R.recent_bias("EGLC") is None


def test_windows_mean_n_lohi(tmp_path, monkeypatch):
    # 10 days, forecast has been running LOW (negative residual = under-call) recently
    ks = [0, 0, 1, 1, 2, 1, 2, 1, 2, 2]
    rows = [{"station": "EGLC", "date": f"2026-06-{d:02d}", "forecast_peak": 25.0,
             "actual_max": 25 + k, "residual": -float(k)} for d, k in zip(range(7, 17), ks)]
    _write(tmp_path, monkeypatch, rows)
    b = R.recent_bias("EGLC")
    assert b["d3"]["n"] == 3 and b["d7"]["n"] == 7         # full short windows
    assert b["d14"]["n"] == 10 and b["d30"]["n"] == 10     # only 10 days available → report n=10 (fills forward)
    assert b["d3"]["mean"] < 0                             # recent residuals negative → forecast under-calls
    assert b["d3"]["lo"] <= b["d3"]["mean"] <= b["d3"]["hi"]
    assert b["d30"]["mean"] == round(-sum(ks) / 10, 2)     # mean of all 10
    assert b["d30"]["variance"] is not None and b["d30"]["sd"] is not None
    assert b["d30"]["under_rate"] == 0.8


def test_regime_breakdown_and_match(tmp_path, monkeypatch):
    rows = [
        {"station": "RKSI", "date": f"2026-06-{d:02d}", "residual": residual, "regime": regime}
        for d, residual, regime in [
            (1, -4.0, "clear"), (2, -5.0, "clear"), (3, -3.0, "clear"),
            (4, -2.0, "rainy"), (5, -1.0, "rainy")]]
    _write(tmp_path, monkeypatch, rows)
    b = R.recent_bias("RKSI", regime="clear")
    assert b["current_regime"] == "clear"
    assert b["matched_regime"]["n"] == 3 and b["matched_regime"]["mean"] == -4.0
    assert b["matched_regime"]["variance"] == 1.0
    assert b["by_regime"]["rainy"]["mean"] == -1.5


def test_only_this_station(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        {"station": "EGLC", "date": "2026-06-16", "residual": -1.0},
        {"station": "LEMD", "date": "2026-06-16", "residual": 3.0}])
    assert R.recent_bias("EGLC")["d3"]["mean"] == -1.0     # LEMD's +3 not mixed in
    assert R.recent_bias("LEMD")["d3"]["mean"] == 3.0


def test_corrupt_and_malformed_lines_skipped(tmp_path, monkeypatch):
    p = tmp_path / "forecast_residuals.jsonl"
    p.write_text('{"station":"EGLC","date":"2026-06-16","residual":-1.0}\n'
                 'NOT JSON\n'
                 '{"station":"EGLC","date":"2026-06-15"}\n')          # no residual → skipped
    monkeypatch.setattr(R, "RESIDUALS", p)
    assert R.recent_bias("EGLC")["d3"]["n"] == 1


# ---- fair-value model (EV gate for the reward farmer) ----

def test_fair_calibration_none_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RESIDUALS", tmp_path / "nope.jsonl")
    assert R.fair_calibration("EGLC") is None


def test_fair_calibration_thin_sample_uses_pooled_sigma(tmp_path, monkeypatch):
    # < FAIR_MIN_N rows → bias from the mean, sigma = POOLED_SIGMA × the global inflation (don't trust a tiny-sample std)
    _write(tmp_path, monkeypatch, [{"station": "EGLC", "date": f"2026-06-1{d}", "residual": 1.0} for d in range(3)])
    calibration = R.fair_calibration("EGLC")
    assert abs(calibration["sigma"] - R.POOLED_SIGMA * R.SIGMA_INFLATE) < 1e-9 and abs(calibration["bias"] - 1.0) < 1e-9


def test_fair_calibration_uses_station_std_when_enough(tmp_path, monkeypatch):
    # ≥ FAIR_MIN_N rows alternating ±1 → mean 0, std ~1.04 → its own sigma × the global ~1.10 inflation
    rows = [{"station": "EGLC", "date": f"2026-06-{d:02d}", "residual": (1.0 if d % 2 else -1.0)} for d in range(1, 13)]
    _write(tmp_path, monkeypatch, rows)
    calibration = R.fair_calibration("EGLC")
    assert abs(calibration["bias"]) < 1e-9
    assert 1.10 < calibration["sigma"] < 1.20 and calibration["sigma"] != R.POOLED_SIGMA


def test_regime_calibration_is_shrunk_and_requires_minimum_sample(tmp_path, monkeypatch):
    rows = ([{"station": "RKSI", "date": f"2026-06-{d:02d}", "residual": -4.0, "regime": "clear"}
             for d in range(1, 5)]
            + [{"station": "RKSI", "date": f"2026-06-{d:02d}", "residual": 0.0, "regime": "rainy"}
               for d in range(5, 13)])
    _write(tmp_path, monkeypatch, rows)
    clear = R.fair_calibration("RKSI", regime="clear")
    assert clear["source"] == "regime_shrunk" and clear["regime_n"] == 4
    assert -4.0 < clear["bias"] < clear["station_bias"]       # conditional mean is partially pooled

    thin = R.fair_calibration("RKSI", regime="mixed")
    assert thin["source"] == "station" and thin["regime_n"] == 0


def test_regime_sigma_widens_a_noisy_regime(tmp_path, monkeypatch):
    # ≥ REGIME_SIGMA_MIN_N spread-out 'rainy' rows + tight all-station context → the rainy σ is conditioned
    # (shrunk toward the station σ) and reflects rainy's bigger spread, not just the all-weather number.
    rows = ([{"station": "RKSI", "date": f"2026-05-{d:02d}", "residual": (0.2 if d % 2 else -0.2), "regime": "clear"}
             for d in range(1, 13)]
            + [{"station": "RKSI", "date": f"2026-06-{d:02d}", "residual": (3.0 if d % 2 else -3.0), "regime": "rainy"}
               for d in range(1, 9)])
    _write(tmp_path, monkeypatch, rows)
    rainy = R.fair_calibration("RKSI", regime="rainy")
    assert rainy["regime_sigma"] is not None and rainy["regime_n"] == 8
    assert rainy["sigma"] > rainy["station_sigma"]            # the noisy regime widens σ above the all-weather scale


def test_bucket_fair_yes_eq_is_low_at_peak():
    # ==N with mu exactly on N and a wide sigma=2 → P(round==N) is only the ±0.5 slice (~0.2), matching calibration
    p = R.bucket_fair_yes("==", 27, mu=27.0, sigma=2.0)
    assert 0.15 < p < 0.25


def test_bucket_fair_yes_ge_and_le_complement():
    # >=N and <=(N−1) partition the line → sum to 1 at any mu/sigma
    assert abs(R.bucket_fair_yes(">=", 27, 26.0, 1.5) + R.bucket_fair_yes("<=", 26, 26.0, 1.5) - 1.0) < 1e-9


def test_bucket_fair_yes_ge_high_when_mu_above():
    assert R.bucket_fair_yes(">=", 27, mu=30.0, sigma=1.0) > 0.99      # expected max well above the threshold → near-certain YES


# ---- change A: lead-matched σ (the farmer bets tomorrow+, where the near-analysis σ is far too tight) ----

def test_leadtime_sigma_overrides_near_analysis_for_farmer(tmp_path, monkeypatch):
    # near-analysis: TIGHT (±0.3) → small σ, bias 0; leadtime day1: WIDE (±1.5) → the honest 24h spread
    _write(tmp_path, monkeypatch, [
        {"station": "EGLC", "date": f"2026-06-{d:02d}", "residual": (0.3 if d % 2 else -0.3)} for d in range(1, 13)])
    _write_leadtime(tmp_path, monkeypatch, [
        {"station": "EGLC", "date": f"2026-05-{d:02d}", "lead_days": 1, "residual": (1.5 if d % 2 else -1.5)}
        for d in range(1, 13)])
    near = R.fair_calibration("EGLC")                       # no lead_days → near-analysis σ (main bot)
    lead = R.fair_calibration("EGLC", lead_days=1)          # farmer path → honest fixed-lead σ
    assert near["sigma_source"] == "near_analysis"
    assert lead["sigma_source"] == "leadtime_d1"
    assert lead["sigma"] > near["sigma"]                    # wider σ → fixes the overconfidence
    assert lead["bias"] == near["bias"]                     # bias unchanged (matches the live forecast source)
    assert lead["near_analysis_sigma"] == near["sigma"]     # kept for transparency


def test_leadtime_sigma_falls_back_when_missing(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        {"station": "EGLC", "date": f"2026-06-{d:02d}", "residual": (0.3 if d % 2 else -0.3)} for d in range(1, 13)])
    monkeypatch.setattr(R, "LEADTIME", tmp_path / "nope.jsonl")
    R._leadtime_index.cache_clear()
    cal = R.fair_calibration("EGLC", lead_days=1)           # graceful: no leadtime file → keep near-analysis σ
    assert cal is not None and cal["sigma_source"] == "near_analysis"


def test_leadtime_sigma_thin_station_falls_back(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        {"station": "EGLC", "date": f"2026-06-{d:02d}", "residual": (0.3 if d % 2 else -0.3)} for d in range(1, 13)])
    _write_leadtime(tmp_path, monkeypatch, [   # < FAIR_MIN_N leadtime rows → not enough to trust
        {"station": "EGLC", "date": f"2026-05-0{d}", "lead_days": 1, "residual": 1.5} for d in range(1, 4)])
    assert R.leadtime_sigma("EGLC", 1) is None
    assert R.fair_calibration("EGLC", lead_days=1)["sigma_source"] == "near_analysis"


def test_leadtime_sigma_keeps_wider_near_analysis(tmp_path, monkeypatch):
    # model-mismatch city (Seoul/Tokyo): near-analysis σ already WIDE (±2.0) > ECMWF leadtime σ (±0.5) → change A
    # must keep the wider σ, never make the farmer MORE overconfident than today.
    _write(tmp_path, monkeypatch, [
        {"station": "RKSI", "date": f"2026-06-{d:02d}", "residual": (2.0 if d % 2 else -2.0)} for d in range(1, 13)])
    _write_leadtime(tmp_path, monkeypatch, [
        {"station": "RKSI", "date": f"2026-05-{d:02d}", "lead_days": 1, "residual": (0.5 if d % 2 else -0.5)}
        for d in range(1, 13)])
    near = R.fair_calibration("RKSI")
    lead = R.fair_calibration("RKSI", lead_days=1)
    assert lead["sigma"] == near["sigma"] and lead["sigma_source"] == "near_analysis"   # wider σ kept (max)
    assert lead["leadtime_sigma"] < near["sigma"]          # narrower leadtime σ recorded but NOT used
