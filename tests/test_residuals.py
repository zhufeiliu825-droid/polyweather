"""Recent forecast-bias reader — residual = forecast − actual over 3/7/14/30d, fed to the decision agent."""
import json

from polyweather.realized import residuals as R


def _write(tmp_path, monkeypatch, rows):
    p = tmp_path / "forecast_residuals.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in rows))
    monkeypatch.setattr(R, "RESIDUALS", p)


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
    # < FAIR_MIN_N rows → bias from the mean, sigma = POOLED_SIGMA (don't trust a tiny-sample std)
    _write(tmp_path, monkeypatch, [{"station": "EGLC", "date": f"2026-06-1{d}", "residual": 1.0} for d in range(3)])
    calibration = R.fair_calibration("EGLC")
    assert calibration["sigma"] == R.POOLED_SIGMA and abs(calibration["bias"] - 1.0) < 1e-9


def test_fair_calibration_uses_station_std_when_enough(tmp_path, monkeypatch):
    # ≥ FAIR_MIN_N rows alternating ±1 → mean 0, std 1.0 (> SIGMA_FLOOR) → its own sigma
    rows = [{"station": "EGLC", "date": f"2026-06-{d:02d}", "residual": (1.0 if d % 2 else -1.0)} for d in range(1, 13)]
    _write(tmp_path, monkeypatch, rows)
    calibration = R.fair_calibration("EGLC")
    assert abs(calibration["bias"]) < 1e-9
    assert 1.0 < calibration["sigma"] < 1.1 and calibration["sigma"] != R.POOLED_SIGMA


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


def test_bucket_fair_yes_eq_is_low_at_peak():
    # ==N with mu exactly on N and a wide sigma=2 → P(round==N) is only the ±0.5 slice (~0.2), matching calibration
    p = R.bucket_fair_yes("==", 27, mu=27.0, sigma=2.0)
    assert 0.15 < p < 0.25


def test_bucket_fair_yes_ge_and_le_complement():
    # >=N and <=(N−1) partition the line → sum to 1 at any mu/sigma
    assert abs(R.bucket_fair_yes(">=", 27, 26.0, 1.5) + R.bucket_fair_yes("<=", 26, 26.0, 1.5) - 1.0) < 1e-9


def test_bucket_fair_yes_ge_high_when_mu_above():
    assert R.bucket_fair_yes(">=", 27, mu=30.0, sigma=1.0) > 0.99      # expected max well above the threshold → near-certain YES
