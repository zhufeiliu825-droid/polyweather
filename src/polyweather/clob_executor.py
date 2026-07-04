"""Deterministic CLOB order executor — NOT the LLM.

Takes a `Decision` (action=BET) and places ONE Polymarket **maker** limit order: a BUY YES bid
(market−0.1¢, tick-snapped DOWN) or — for the reward farmer's Stage-2 flatten — a SELL YES ask
(tick-snapped UP); GTD (auto-expire) or GTC (ttl_min=None, rests until cancel/fill). Run by the daemon
under the user's control. Credentials + wallet key come from ENV (a gitignored `.env`); never in code,
never seen by the decision agent.

ORDER-MANAGEMENT POLICY — PHASE 2 (user-set 2026-06-17): place a resting maker bid at market−0.1¢
(MAKER_UNDERCUT, tick-snapped) as a **GTD that AUTO-EXPIRES after MAKER_TTL_MIN (default 5 min)** —
the EXCHANGE removes it at expiry, we NEVER call cancel (so there is no cancel/replace double-bet
window, the old audit's CRITICAL). The daemon polls `order_status`: a fill → held to settlement; a
confirmed expiry (unfilled) → re-evaluate + re-place a fresh GTD, until filled or the peak cutoff
passes. EVERY order is a resting maker — even a cheap (<10¢) bucket is sized UP to clear the $1 min
(never a taker: makers earn liquidity rewards, pay 0 fee, never cross). One LIVE order per (city, event)
at a time (re-place only after order_status authoritatively confirms the prior one terminal — EXPIRED/
CANCELLED/GONE — never OPEN or matched).

SAFETY (execution):
  - `POLY_DRY_RUN` defaults "1" (TRUE): logs intended place/cancel WITHOUT
    submitting. Set `POLY_DRY_RUN=0` to go live (the user's explicit action).
  - MAKER (GTD) limit ONLY — never market orders.
  - Validates side / token / price (< 0.85) / size (hard cap).
  - `py-clob-client-v2` (the CLOB **v2** client) is imported lazily, only on the
    live path — DRY_RUN needs nothing installed. v1 (`py-clob-client`) was retired:
    its order version is rejected by CLOB v2 with "invalid order version".

The LLM never submits a live order in a chat session; THIS module does, when the
user runs it with `POLY_DRY_RUN=0` and creds in env.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass

from .models import Decision
from .params import MAKER_FLOOR as PRICE_FLOOR  # tunable: min maker price = exchange tick
from .params import (MAKER_TTL_MIN, MAX_SIZE_USD, MIN_ORDER_SHARES,  # tunable: maker GTD TTL, $ notional cap/floor,
                     MIN_ORDER_USD, PRICE_CEILING)                  # share min, price ceiling
from .realized.feature_store import DATA_DIR

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137                       # Polygon
ORDER_LOG = DATA_DIR / "orders.jsonl"
POSITIONS_URL = "https://data-api.polymarket.com/positions"   # public Data API — ground-truth holdings by address


def _dry_run() -> bool:
    return os.environ.get("POLY_DRY_RUN", "1") != "0"


@dataclass
class OrderResult:
    ok: bool
    dry_run: bool
    detail: str
    order_id: str | None = None
    ambiguous: bool = False          # True = the order MAY be live despite ok=False (lost response /
    #                                  unrecognized id) → the daemon must NOT silently re-place (audit H2).
    gone: bool = False               # (cancel only) ok=True because the order was ALREADY TERMINAL (matched / already
    #                                  cancelled), NOT a clean cancel of a live OPEN order → the caller must NOT act on
    #                                  a possibly-filled order with stale positions (no resell/replace; re-confirm next cycle).
    filled: float | None = None      # (taker FAK only) SHARES matched, parsed from the post response — None when the
    #                                  response didn't carry a parsable amount (callers treat ok-but-None as AMBIGUOUS,
    #                                  never as a confirmed fill). run_cluster._taker_result reads this.


def _snap_maker(price: float, tick: float = 0.01) -> float:
    """Snap a BUY maker limit DOWN to the exchange tick grid. The order-builder's rounding can push
    an off-grid price UP (e.g. 0.835→0.84), eroding the 1¢ undercut or crossing the spread to fill
    as a taker. Flooring toward 0 keeps it a strict maker at ≤ the intended level (audit M2)."""
    if tick <= 0:
        return round(price, 4)
    return round(math.floor(price / tick + 1e-9) * tick, 6)


def _snap_ask(price: float, tick: float = 0.01) -> float:
    """Snap a SELL maker limit UP to the tick grid — the mirror of _snap_maker. Rounding could push an ask
    DOWN across the spread to fill as a taker; ceiling toward 1 keeps it a strict maker at ≥ the intended ask."""
    if tick <= 0:
        return round(price, 4)
    return round(math.ceil(price / tick - 1e-9) * tick, 6)


def _log(rec: dict) -> None:
    ORDER_LOG.parent.mkdir(parents=True, exist_ok=True)     # fresh clone has no data/ yet — never lose the audit row
    with ORDER_LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


_CLIENT = None     # process-level cache: build + AUTHENTICATE once, not per order
_TICK_CACHE: dict[str, str] = {}     # token → tick grid ("0.01"); a token's grid is constant → fetch once, not per order/reprice


def _tick_size(client, token: str) -> tuple[str, float]:
    """The price grid for `token` as (grid_str, grid_float), CACHED per token (it never changes). Avoids a
    get_tick_size round-trip on every order and every reprice of the same bucket. Safe default 0.01 on lookup
    failure (NOT cached → retried next time)."""
    cached = _TICK_CACHE.get(token)
    if cached is not None:
        return cached, float(cached)
    try:
        tick_str = client.get_tick_size(token)             # v2 returns the grid as a STRING ("0.01")
        float(tick_str)                                    # validate before caching
    except Exception:
        return "0.01", 0.01                                # transient → don't cache, retry next call
    _TICK_CACHE[token] = tick_str
    return tick_str, float(tick_str)


def _live_client():
    """Build (once, cached) a CLOB-**v2** ClobClient from ENV creds (live path only). Returns (client, None)
    or (None, error_message). LLM never reaches here in a chat session. The client + its derived L2 api creds
    are CACHED at module level so every order in this process reuses ONE authed client — placing 17 reward bids
    must NOT re-derive the api key 17× (that spams POST /auth/api-key → rate-limit → "Could not create api
    key"). v2 negotiates the order version automatically (v1's was rejected)."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT, None
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient
    except ImportError:
        return None, "py-clob-client-v2 not installed (`uv add py-clob-client-v2`)"
    key = os.environ.get("POLY_PRIVATE_KEY")
    if not key:
        return None, "missing POLY_PRIVATE_KEY in env"
    try:
        sig = int(os.environ.get("POLY_SIGNATURE_TYPE", "0"))
    except ValueError:
        return None, f"POLY_SIGNATURE_TYPE={os.environ.get('POLY_SIGNATURE_TYPE')!r} not an int"
    funder = (os.environ.get("POLY_FUNDER") or "").strip()
    if sig in (1, 2, 3) and not funder:
        return None, "POLY_SIGNATURE_TYPE 1/2/3 (proxy / deposit wallet) requires a non-empty POLY_FUNDER"
    env_creds = (os.environ.get("CLOB_API_KEY"), os.environ.get("CLOB_API_SECRET"),
                 os.environ.get("CLOB_API_PASSPHRASE"))
    try:
        client = ClobClient(CLOB_HOST, CHAIN_ID, key=key,
                            signature_type=sig,           # 0=EOA, 1/2=proxy/email wallet
                            funder=funder or None)         # proxy/funder addr (only for 1/2)
        # L2 API creds: env CLOB_API_* if fully set; else DERIVE the existing key (deterministic, no spam). Only
        # if derive fails (a brand-new wallet, no key yet) fall back to create_or_derive. Set before any signed call.
        if all(env_creds):
            client.set_api_creds(ApiCreds(*env_creds))
        else:
            try:
                client.set_api_creds(client.derive_api_key())
            except Exception:
                client.set_api_creds(client.create_or_derive_api_key())
    except Exception as e:
        return None, f"clob v2 client init failed: {e!r}"
    _CLIENT = client
    return client, None


def _order_id(resp) -> str | None:
    """Extract the order id from a post_order response, tolerant of key naming.
    VERIFY the real key against the installed py-clob-client on the first live post."""
    if not isinstance(resp, dict):
        return None
    return resp.get("orderID") or resp.get("order_id") or resp.get("orderId") or resp.get("id")


def _taker_filled(resp, is_sell: bool) -> float | None:
    """SHARES matched by a FAK, from the post response. CLOB semantics: a BUY makes collateral and TAKES
    outcome tokens (shares = takingAmount); a SELL makes outcome tokens (shares = makingAmount). Tolerant of
    key naming; None when no parsable amount — callers must treat that as UNCONFIRMED (ambiguous), never as
    a fill. VERIFY the real keys against the installed client on the first live FAK."""
    if not isinstance(resp, dict):
        return None
    keys = (("makingAmount", "making_amount") if is_sell else ("takingAmount", "taking_amount"))
    for k in (*keys, "sizeMatched", "size_matched", "matchedAmount"):
        v = resp.get(k)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f >= 0:
            return f
    return None


def _validate(d: Decision) -> str | None:
    if d.action != "BET":
        return f"action={d.action} (only BET is placed)"
    if d.side not in ("BUY_YES", "SELL_YES"):
        return f"side={d.side} (only BUY_YES / SELL_YES)"
    if d.order_type not in ("maker", "taker"):
        return f"order_type={d.order_type} (only maker / taker)"
    # taker is allowed BOTH ways: SELL = the farmer's 关门时间 force-flatten; BUY = the cluster/snipe FAK entry
    # (the BUY price ceiling below still applies — a taker BUY crosses at the ask, so the ask must clear it).
    if not d.token_yes:
        return "missing token_yes"
    # a resting maker is ≥ the tick floor; a BUY bid stays under the 0.85 ceiling, a SELL ask may price high
    # (we sell dear) but stay postable (< 1).
    ceiling = PRICE_CEILING if d.side == "BUY_YES" else 1.0
    if d.maker_price is None or not (PRICE_FLOOR <= d.maker_price < ceiling):
        return f"{d.side} price {d.maker_price} outside [{PRICE_FLOOR}, {ceiling})"
    if d.size_shares < MIN_ORDER_SHARES:                   # 5-SHARE exchange minimum
        return f"size_shares {d.size_shares} < {MIN_ORDER_SHARES}-share exchange minimum"
    notional = d.size_shares * d.maker_price
    if notional > MAX_SIZE_USD:                            # $ notional safety ceiling (shares × price)
        return f"notional {d.size_shares}×{d.maker_price} = ${notional:.2f} > ${MAX_SIZE_USD} cap"
    if notional < MIN_ORDER_USD - 1e-9:                    # $1 platform minimum (the maker is sized to clear it)
        return f"notional {d.size_shares}×{d.maker_price} = ${notional:.2f} < ${MIN_ORDER_USD} platform min"
    return None


def place_from_decision(d: Decision, *, now_iso: str, ttl_min: int | None = MAKER_TTL_MIN) -> OrderResult:
    """Place (or DRY_RUN-log) the order a BET Decision describes: a resting maker bid. `ttl_min` an int → a
    GTD that AUTO-EXPIRES after that many minutes (the directional bot; the exchange removes it, we never
    cancel). `ttl_min=None` → a GTC that rests until filled or cancelled (the reward FARMER — max in-band time
    = max reward; it tracks the moving band via cancel+confirm+replace, not expiry). The poller / positions
    detect fill. (Always a maker — earns rewards, pays 0 fee, never crosses; cheap buckets sized up to $1.)"""
    shares = d.size_shares                               # FIXED share count — Polymarket sizes orders in shares
    is_sell = d.side == "SELL_YES"                        # SELL = flatten a held YES position (ask); BUY = a reward bid
    is_taker = d.order_type == "taker"                    # 关门时间 SELL force-flatten (FAK, CROSSES the bid to guarantee a fill)
    otype = "FAK-taker" if is_taker else ("GTD-maker" if ttl_min is not None else "GTC-maker")
    side_label = "SELL" if is_sell else "BUY"
    base = {"ts": now_iso, "city": d.city, "station": d.station_id,
            "market_date": str(d.local_date), "bucket": d.label, "token": d.token_yes,
            "side": side_label, "price": d.maker_price, "shares": shares, "type": otype}

    err = _validate(d)
    if err:
        _log({**base, "status": "REJECTED", "reason": err})
        return OrderResult(False, _dry_run(), f"rejected: {err}")

    # Snap direction: a MAKER never crosses (BUY bid DOWN, SELL ask UP); a TAKER must stay marketable
    # (BUY limit UP ≥ the ask, SELL limit DOWN ≤ the bid).
    snap_up = (is_sell != is_taker)                       # maker SELL or taker BUY → UP; maker BUY or taker SELL → DOWN

    if _dry_run():
        grid = 0.001 if (d.maker_price or 1.0) < 0.04 else 0.01   # DRY estimate grid (sub-0.04 buckets ≈ 0.001 tick)
        price = (_snap_ask if snap_up else _snap_maker)(d.maker_price, grid)
        notional = round(shares * price, 2)              # $ value = shares × price (varies with price)
        _log({**base, "price": price, "notional_usd": notional, "status": "DRY_RUN"})
        return OrderResult(True, True,
                           f"DRY_RUN: {side_label} {d.label} ({d.token_yes[:8]}…) @ {price} ×{shares} sh (${notional}) [{otype}]")

    # ---- LIVE submit (only when POLY_DRY_RUN=0). ----
    client, cerr = _live_client()
    if client is None:
        _log({**base, "status": "ERROR", "reason": cerr})
        return OrderResult(False, False, f"{cerr} — no order")        # creds missing → definitely no order
    from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
    from py_clob_client_v2.exceptions import PolyApiException, PolyException
    tick_str, tick = _tick_size(client, d.token_yes)      # cached per token (constant grid) → no round-trip per order/reprice
    # Snap to the grid: a BUY bid DOWN (≤ intended, below mid), a SELL ask UP (≥ intended, above mid).
    # post_only also enforces maker — never crosses.
    side_enum = Side.SELL if is_sell else Side.BUY
    price = (_snap_ask if snap_up else _snap_maker)(d.maker_price, tick)   # maker: never crosses; taker: stays marketable
    if not (PRICE_FLOOR <= price < 1.0):                 # snapped off the postable range → no order
        _log({**base, "price": price, "status": "REJECTED", "reason": f"snapped {price} off [{PRICE_FLOOR}, 1)"})
        return OrderResult(False, False, f"snapped {price} off postable range — no order")
    # ONE combined create+post, post_only=True (never crosses; v2 negotiates the order version internally,
    # fixing v1's "invalid order version"). GTD (ttl_min min, AUTO-EXPIRES exchange-side — directional bot,
    # never cancels) OR GTC (ttl_min=None, expiration 0, rests until fill/cancel — the farmer, which tracks the
    # band via the cancel+confirm+replace path, the existing cancel_order() below).
    # Taker (FORCE flatten): FAK, post_only OFF → crosses the bid and fills now (kills any unfilled remainder).
    # Maker: GTD (ttl_min min, auto-expires) or GTC (rests till fill/cancel); post_only ON → never crosses.
    expiration = 0 if (ttl_min is None or is_taker) else int(time.time()) + ttl_min * 60
    otype_enum = OrderType.FAK if is_taker else (OrderType.GTC if ttl_min is None else OrderType.GTD)
    try:
        resp = client.create_and_post_order(
            OrderArgs(token_id=d.token_yes, price=price, size=shares, side=side_enum, expiration=expiration),
            options=PartialCreateOrderOptions(tick_size=tick_str),
            order_type=otype_enum, post_only=not is_taker)
    except PolyApiException as e:                          # HTTP reject; a 4xx is pre-acceptance → order NOT created
        st = getattr(e, "status_code", None)               # PolyApiException stores status_code directly
        if st is not None and 400 <= st < 500:
            _log({**base, "price": price, "shares": shares, "status": "REJECTED", "reason": f"{st}: {e!r}"})
            return OrderResult(False, False, f"order rejected ({st}): {e!r}")     # ambiguous=False → not UNRESOLVED
        # 5xx / no-status → the order MAY have landed → AMBIGUOUS, never silently re-place (audit H2).
        _log({**base, "price": price, "shares": shares, "status": "ERROR", "reason": repr(e)})
        return OrderResult(False, False, f"order submit error: {e!r}", ambiguous=True)
    except PolyException as e:                             # local pre-submit validation (e.g. invalid price) → no order sent
        _log({**base, "price": price, "shares": shares, "status": "REJECTED", "reason": repr(e)})
        return OrderResult(False, False, f"order rejected (local): {e!r}")
    except Exception as e:                                 # unknown (possibly mid-post) → AMBIGUOUS, never silently re-place
        _log({**base, "price": price, "shares": shares, "status": "ERROR", "reason": repr(e)})
        return OrderResult(False, False, f"order submit exception: {e!r}", ambiguous=True)
    oid = _order_id(resp)
    resp_err = isinstance(resp, dict) and (resp.get("error") or resp.get("errorMsg"))
    if is_taker:
        # A FAK doesn't rest → it may fill (fully/partially) or kill with NO order id. Any non-error response =
        # accepted/processed; the farmer re-checks positions_held (ground truth) to confirm the flatten and
        # re-forces the remainder if needed (size re-clamps to held → never double-sells). Only an error fails.
        if not resp or resp_err:
            _log({**base, "price": price, "shares": shares, "status": "ERROR", "reason": f"taker resp: {resp!r}"})
            return OrderResult(False, False, f"taker FAK failed: {resp!r}", ambiguous=True)
        matched = _taker_filled(resp, is_sell)
        _log({**base, "price": price, "shares": shares, "status": "LIVE", "resp": resp, "order_id": oid,
              "filled": matched})
        return OrderResult(True, False, f"posted {otype} {oid or '(filled, no-rest)'}", order_id=oid,
                           filled=matched)
    if not resp or resp_err or not oid:
        # NEVER claim success without a confirmed order id — but the order MAY be live (unrecognized
        # id key / partial response), so flag AMBIGUOUS so the daemon blocks re-placement (audit H2).
        _log({**base, "price": price, "shares": shares, "status": "ERROR", "reason": f"no order id in resp: {resp!r}"})
        return OrderResult(False, False, f"create_and_post failed / no order id: {resp!r}", ambiguous=True)
    # a resting GTD maker bid (auto-expires after MAKER_TTL_MIN); the daemon polls order_status for fill/expiry.
    _log({**base, "price": price, "shares": shares, "status": "LIVE", "resp": resp, "order_id": oid})
    return OrderResult(True, False, f"posted {otype} {oid}", order_id=oid)


def cancel_order(order_id: str, *, now_iso: str, reason: str = "") -> OrderResult:
    """Cancel a resting (unfilled) maker order. The daemon calls this when our_top
    goes invalid before the order fills (dead bucket / fav changed / price ≥0.85 /
    unhealthy market). We CANCEL rather than chase — see the module policy."""
    rec = {"ts": now_iso, "order_id": order_id, "type": "CANCEL", "reason": reason}
    if not order_id:
        return OrderResult(False, _dry_run(), "no order_id to cancel")
    if _dry_run():
        _log({**rec, "status": "DRY_RUN"})
        return OrderResult(True, True, f"DRY_RUN: would cancel {order_id} ({reason})", order_id=order_id)

    client, cerr = _live_client()
    if client is None:
        _log({**rec, "status": "ERROR", "reason": cerr})
        return OrderResult(False, False, f"{cerr} — cannot cancel")
    from py_clob_client_v2.clob_types import OrderPayload
    try:
        resp = client.cancel_order(OrderPayload(orderID=order_id))   # v2 takes an OrderPayload (→ body {"orderID": id}), NOT a bare str
    except Exception as e:
        _log({**rec, "status": "ERROR", "reason": repr(e)})
        return OrderResult(False, False, f"cancel exception: {e!r}", order_id=order_id)
    # FAIL-SAFE: claim success only when the order is OFF THE BOOK. The v2 API lists already-terminal orders in
    # not_canceled with a reason — "matched orders can't be canceled" / "order can't be found - already canceled
    # or matched" mean the order is GONE (filled/cancelled/expired), so ok=True (safe to clear our state; the
    # caller re-checks positions for a fill). Keep ok=False ONLY for a genuinely-unknown/transient not_canceled
    # (don't drop a maybe-still-live order). (Without this, a matched order loops forever — audit 2026-06-18.)
    ok = bool(resp) and not (isinstance(resp, dict) and resp.get("error"))
    gone = False
    if isinstance(resp, dict) and order_id in (resp.get("not_canceled") or {}):
        msg = str((resp["not_canceled"] or {}).get(order_id, "")).lower()
        gone = any(k in msg for k in ("not be found", "not found", "already cancel", "matched"))   # already TERMINAL
        ok = gone                                                                # GONE → ok=True; transient → ok=False
    # gone=True (terminal: matched/already-cancelled) is NOT a clean cancel of a live OPEN order → the caller must
    # not resell/replace on a possibly-FILLED order with stale positions (it re-confirms next cycle). gone stays
    # False on a clean cancel (the order was open → it did NOT fill → safe to act).
    _log({**rec, "status": ("LIVE" if ok else "ERROR"), "resp": resp})
    return OrderResult(ok, False, f"cancel {order_id}: {resp!r}", order_id=order_id, gone=gone)


def filled_size(order_id: str) -> float | None:
    """How many shares of `order_id` have MATCHED (its size_matched) — the ORDER-level fill, which (unlike
    positions_held) is NOT confounded by a shared-funder co-hold. The reward farmer reads this when it cancels a
    resting maker to reprice/force-flatten, to shrink its private ceiling by what the maker actually sold (so a
    partially-filled maker can never be re-sold for its full original size). None on DRY / no-id / any error →
    the caller falls back to its existing (cur-based) clamp."""
    if not order_id or _dry_run():
        return None
    client, _ = _live_client()
    if client is None:
        return None
    try:
        o = client.get_order(order_id)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    try:
        return float(o.get("size_matched") or 0)
    except (TypeError, ValueError):
        return None


def order_status(order_id: str) -> str:
    """Status of a resting order → 'FILLED' | 'PARTIAL' | 'OPEN' | 'CANCELLED' | 'EXPIRED' | 'GONE' | 'DRY'
    | 'ERROR'. The daemon HOLDS on FILLED/PARTIAL; re-places only on an affirmatively-terminal
    (CANCELLED/EXPIRED/GONE) once the GTD TTL has elapsed; and NEVER re-places on OPEN (still live on the
    book) or ERROR (can't confirm) so it never opens a second order against a live/unknown one.

    GONE = the order isn't on the book (a 404 / empty response) → it expired/cancelled UNFILLED (a fill
    persists, so a filled order never 404s). STATUS-AUTHORITATIVE: only the explicit status decides
    FILLED/CANCELLED — we never fake a FILLED from size math (a contradictory matched≥size with a
    non-terminal status is surfaced as PARTIAL + logged). Verify the field names (status / size_matched /
    original_size) + the not-found shape (404 vs empty) against the installed py-clob-client on first live."""
    if not order_id:
        return "ERROR"
    if _dry_run():
        return "DRY"
    client, cerr = _live_client()
    if client is None:
        return "ERROR"
    from py_clob_client_v2.exceptions import PolyApiException
    try:
        o = client.get_order(order_id)
    except PolyApiException as e:
        if getattr(e, "status_code", None) == 404:    # not found = not on the book = expired/cancelled unfilled
            return "GONE"
        return "ERROR"
    except Exception:
        return "ERROR"
    if not o:                                          # empty response → order not on the book → gone (unfilled)
        return "GONE"
    o = o if isinstance(o, dict) else {}
    st = str(o.get("status", "")).upper()
    matched = float(o.get("size_matched") or 0)
    size = float(o.get("original_size") or o.get("size") or 0)
    if st in ("MATCHED", "FILLED"):
        return "FILLED"
    if st in ("CANCELED", "CANCELLED"):
        return "CANCELLED"
    if st == "EXPIRED":                             # GTD lapsed (the exchange reports it explicitly) — unfilled
        return "EXPIRED"
    if size and matched >= size:                    # status disagrees with fill math — surface, don't fake FILLED
        _log({"type": "STATUS_CONTRADICTION", "order_id": order_id, "status": st,
              "matched": matched, "size": size})
        return "PARTIAL"
    if matched > 0:
        return "PARTIAL"
    return "OPEN"


def positions_held() -> dict | None:
    """{token_id(asset): {"size": float, "title": str, "redeemable": bool}} for our funder's OPEN positions
    (Polymarket Data API — PUBLIC, keyed by POLY_FUNDER). This is the GROUND-TRUTH fill check: `get_order`
    returns GONE (404) for a filled-then-off-book maker (verified live 2026-06-17), so a FILL is confirmed by
    HOLDING the token, not by order status. Returns None when it can't be queried/parsed (DRY, no funder,
    HTTP/parse error) → callers treat None as 'unknown' and FAIL CLOSED (never re-place when not confirmable)."""
    if _dry_run():
        return None
    addr = (os.environ.get("POLY_FUNDER") or "").strip()
    if not addr:
        return None
    try:
        import httpx
        r = httpx.get(POSITIONS_URL, params={"user": addr, "sizeThreshold": "0.5"}, timeout=20)
        if r.status_code != 200:
            return None
        out = {}
        for p in r.json():
            a = p.get("asset")
            if a:
                out[str(a)] = {"size": float(p.get("size") or 0),
                               "title": str(p.get("title") or ""), "redeemable": bool(p.get("redeemable"))}
        return out
    except Exception:
        return None


def book_bid_ask(token: str) -> tuple[float | None, float | None] | None:
    """(best_bid, best_ask) for a token from the live CLOB order book (PUBLIC /book endpoint, no auth). EACH side
    is independently None when that side of the book is empty (a ONE-SIDED book — common near resolution); the
    whole result is None ONLY on an HTTP/parse failure. The reward farmer's Stage-2 needs only the best BID to
    price a maker ask above it OR a 关门时间 taker that sells INTO it, so a one-sided book with bids must NOT be
    discarded as if there were no book (audit 2026-06-18)."""
    try:
        import httpx
        r = httpx.get(f"{CLOB_HOST}/book", params={"token_id": str(token)}, timeout=15)
        if r.status_code != 200:
            return None
        b = r.json()
        bb = max((float(x["price"]) for x in (b.get("bids") or [])), default=None)
        ba = min((float(x["price"]) for x in (b.get("asks") or [])), default=None)
        return (bb, ba)                                  # either may be None (one-sided); None only on fetch failure
    except Exception:
        return None
