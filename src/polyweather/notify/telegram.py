"""Telegram transport — hourly digest + escalations + two-way replies.

Bot token + chat_id come from ENV (gitignored `.env`): `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. NEVER in code/committed. If either is unset → DRY mode: the
message is logged to `data/telegram.jsonl` and NOT sent, so the daemon runs
end-to-end without a token (parallels clob_executor's DRY_RUN).

This module is TRANSPORT ONLY. The hourly digest text is rendered by
`run/telegram_digest.render_digest` (user-defined format) and passed here as a
string — keeping the format in one place. Escalations are formatted here.

Two-way: gate failures ESCALATE; the user replies «<city> bet [bucket]» / «<city>
skip». `poll_replies` reads getUpdates and parses those.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx

from ..models import Decision
from ..obslog import log_failure
from ..realized.feature_store import DATA_DIR

LOG = DATA_DIR / "telegram.jsonl"
API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str | None:
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def _chat() -> str | None:
    return os.environ.get("TELEGRAM_CHAT_ID")


def enabled() -> bool:
    return bool(_token() and _chat())


def _log(rec: dict) -> None:
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


async def send(client: httpx.AsyncClient, text: str, *, kind: str = "msg") -> bool:
    """Send a Telegram message. No token/chat in env → DRY (log only, returns False)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not enabled():
        _log({"ts": ts, "kind": kind, "status": "DRY(no token)", "text": text})
        return False
    r = await client.post(API.format(token=_token(), method="sendMessage"),
                          json={"chat_id": _chat(), "text": text})
    ok = r.status_code == 200 and bool(r.json().get("ok"))
    if not ok:                                 # a real send that failed — the operator may miss this message
        log_failure("telegram", kind="http_error", detail=f"sendMessage {r.status_code} ({kind})",
                    consequence="skipped")
    _log({"ts": ts, "kind": kind, "status": "SENT" if ok else f"ERR {r.status_code}", "text": text})
    return ok


def _slug(city: str) -> str:
    return city.lower().replace(" ", "")


def format_escalation(d: Decision) -> str:
    s = _slug(d.city)
    alts = "\n".join(f"  {a['label']:<5} mkt {a['mkt']}{'' if a.get('alive', True) else ' (dead)'}"
                     for a in (d.alts or []))
    alts_block = f"top buckets by market:\n{alts}\n" if alts else ""
    return (f"⚠️ ESCALATE {d.city} ({d.station_id}) {d.local_date}\n"
            f"bucket {d.label}  conf={d.confidence}\n"
            f"market {d.market_yes}  maker {d.maker_price}  health={d.health_ok}\n"
            f"{alts_block}"
            f"{d.reason}\n"
            f"reply: «{s} bet [{d.threshold}]»  或  «{s} skip»")


async def send_escalation(client: httpx.AsyncClient, d: Decision) -> bool:
    return await send(client, format_escalation(d), kind="escalation")


def _as_int(s: str) -> int | None:
    """int() or None — the authoritative 'is this a real integer token' test. `str.isdigit()` accepts
    unicode superscripts ('³') and '-'-prefixed junk ('--3') that then CRASH int() downstream."""
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _parse_reply(text: str) -> dict | None:
    """«london bet 23» / «mexico city skip» → {city(slug), action, bucket?}.
    Everything before the bet/skip keyword is the city, spaces removed → matches the
    daemon's slug (city.lower().replace(' ','')), so multi-word cities work."""
    parts = (text or "").lower().split()
    action = next((a for a in ("bet", "skip") if a in parts), None)
    if action is None:
        return None
    i = parts.index(action)
    city = "".join(parts[:i])                      # multi-word → slug (e.g. "mexico city" → "mexicocity")
    if not city:
        return None
    out = {"city": city, "action": action}
    if action == "bet":
        # only a token that ACTUALLY parses as int (so the daemon's int(rp["bucket"]) can never crash on
        # '--3'/'³' and abort the rest of an already-ACKed reply batch — audit r5)
        out["bucket"] = next((p for p in parts[i + 1:] if _as_int(p) is not None), None)
    return out


async def poll_replies(client: httpx.AsyncClient, offset: int | None = None):
    """Poll getUpdates → (list of parsed {city,action,bucket?}, new_offset). Disabled → ([], offset)."""
    if not enabled():
        return [], offset
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    r = await client.get(API.format(token=_token(), method="getUpdates"), params=params)
    if r.status_code != 200:
        log_failure("telegram", kind="http_error", detail=f"getUpdates {r.status_code}", consequence="skipped")
        return [], offset
    replies, new_offset = [], offset
    for u in r.json().get("result", []):
        new_offset = u["update_id"] + 1
        msg = u.get("message") or {}
        parsed = _parse_reply(msg.get("text", ""))
        if parsed:
            parsed["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            parsed["msg_epoch"] = msg.get("date")   # Telegram send-time (epoch s) — the daemon rejects a
            _log({**parsed, "kind": "reply"})        # «bet» sent BEFORE its escalation. Unlike poll-time ts,
            replies.append(parsed)                   # this survives a re-poll, so a replay can't masquerade as fresh (H1)
    return replies, new_offset
