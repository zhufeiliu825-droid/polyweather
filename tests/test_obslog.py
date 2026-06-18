import json
from datetime import datetime, timedelta, timezone

from polyweather import obslog


def test_log_failure_writes_record(tmp_path, monkeypatch):
    p = tmp_path / "f.jsonl"
    monkeypatch.setattr(obslog, "FAILURE_LOG", p)
    obslog.log_failure("clob", kind="fetch_failed", detail="boom",
                       station="EGLC", city="London", consequence="escalated")
    rec = json.loads(p.read_text().strip())
    assert rec["source"] == "clob" and rec["kind"] == "fetch_failed"
    assert rec["station"] == "EGLC" and rec["consequence"] == "escalated" and "boom" in rec["detail"]
    assert "ts" in rec


def test_log_failure_never_raises_on_bad_path(tmp_path, monkeypatch):
    # best-effort: a write error must be swallowed — observability can NEVER break a live tick
    monkeypatch.setattr(obslog, "FAILURE_LOG", tmp_path / "nodir" / "x.jsonl")   # missing parent → open() fails
    obslog.log_failure("awc", kind="fetch_failed")                              # must NOT raise


def test_recent_failure_summary_counts_recent_only(tmp_path, monkeypatch):
    p = tmp_path / "f.jsonl"
    monkeypatch.setattr(obslog, "FAILURE_LOG", p)
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text("\n".join(json.dumps(r) for r in [
        {"ts": old, "source": "awc"},          # too old → excluded
        {"ts": recent, "source": "awc"},
        {"ts": recent, "source": "clob"},
    ]) + "\n")
    s = obslog.recent_failure_summary(60, now=now)
    assert "awc×1" in s and "clob×1" in s and "数据源失效" in s


def test_recent_failure_summary_empty_when_no_log(tmp_path, monkeypatch):
    monkeypatch.setattr(obslog, "FAILURE_LOG", tmp_path / "none.jsonl")
    assert obslog.recent_failure_summary(60) == ""
