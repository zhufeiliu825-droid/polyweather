"""READ-ONLY CLOB v2 auth/wallet pre-flight — NO order, NO money movement.

Builds the v2 client via the SAME path the live executor uses (`clob_executor._live_client`,
so it reads POLY_SIGNATURE_TYPE / POLY_FUNDER / POLY_PRIVATE_KEY / CLOB_API_* from env and
derives L2 creds if CLOB_API_* are unset), then asks the CLOB two read-only questions:
  1. get_address()          → the signer EOA the client authenticates as
  2. get_balance_allowance  → does the CLOB SEE our USDC collateral under this wallet config?

If (2) returns ~our real cash → the funder/sig_type/creds are recognized and a maker order
should be accepted. If it returns 0 (while the web UI shows funds) → that's the known
"maker address not allowed / deposit wallet" block (the funder isn't recognized for CLOB
order placement under this config).

SAFETY: prints ONLY public data (chain addresses, balances, sig type). Private key, API
secret, and passphrase are NEVER printed; all free-form error text is run through `_redact`
which strips long hex (keys/addresses) and long base64-ish tokens (secrets) so a traceback
can't leak a credential. Run by the user with `.env` sourced; places nothing.

    set -a; . ./.env; set +a; uv run python maintain/check_clob_auth.py
"""
from __future__ import annotations

import os
import re

from polyweather.clob_executor import _live_client

_HEX = re.compile(r"0x[0-9a-fA-F]{40,}|\b[0-9a-fA-F]{64}\b")
_B64 = re.compile(r"[A-Za-z0-9_\-+/=]{30,}")


def _redact(s: object) -> str:
    """Strip anything that could be a private key / api secret from free-form text."""
    t = _HEX.sub("«hex-redacted»", str(s))
    t = _B64.sub("«token-redacted»", t)
    return t


def _balance(client, sig: int) -> str:
    """Read USDC (COLLATERAL) balance/allowance the CLOB attributes to this wallet config."""
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams
    from py_clob_client_v2.exceptions import PolyApiException
    try:
        ba = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig))
    except PolyApiException as e:
        st = getattr(e, "status_code", None)
        return f"PolyApiException status={st}: {_redact(getattr(e, 'error_msg', e))}"
    except Exception as e:  # noqa: BLE001 — diagnostic: report any failure, never raise
        return f"{type(e).__name__}: {_redact(e)}"
    # USDC has 6 decimals; the API returns raw integer strings.
    try:
        if isinstance(ba, dict):
            bal_raw = ba.get("balance")
            alw_raw = ba.get("allowance") or ba.get("allowances")
        else:
            bal_raw = getattr(ba, "balance", None)
            alw_raw = getattr(ba, "allowance", None)
        bal = f"{int(bal_raw) / 1e6:,.2f} USDC" if bal_raw not in (None, "") else f"raw={bal_raw!r}"
    except (TypeError, ValueError):
        bal, alw_raw = f"raw={ba!r}", None
    return f"balance={bal}  allowance_raw={alw_raw!r}"


def main() -> int:
    sig_env = os.environ.get("POLY_SIGNATURE_TYPE", "0")
    funder = (os.environ.get("POLY_FUNDER") or "").strip()
    env_creds_set = all((os.environ.get("CLOB_API_KEY"),
                         os.environ.get("CLOB_API_SECRET"),
                         os.environ.get("CLOB_API_PASSPHRASE")))
    names = {0: "EOA", 1: "POLY_PROXY(email/Magic)", 2: "POLY_GNOSIS_SAFE", 3: "POLY_1271(deposit)"}
    try:
        sig = int(sig_env)
    except ValueError:
        sig = -1
    print("── CLOB v2 auth pre-flight (READ-ONLY, no order) ──")
    print(f"POLY_SIGNATURE_TYPE = {sig_env}  ({names.get(sig, '??')})")
    print(f"POLY_FUNDER         = {funder or '(unset)'}")
    print(f"CLOB_API_* in env   = {env_creds_set}  ({'using env creds' if env_creds_set else 'will DERIVE creds from key'})")
    print("building client …")

    client, err = _live_client()
    if client is None:
        print(f"❌ client build failed: {_redact(err)}")
        return 1
    try:
        signer = client.get_address()
    except Exception as e:  # noqa: BLE001
        signer = f"<get_address failed: {type(e).__name__}>"
    print(f"signer EOA (get_address) = {signer}")
    if funder:
        same = isinstance(signer, str) and signer.lower() == funder.lower()
        print(f"funder == signer?        = {same}  "
              f"({'EOA self-funded' if same else 'proxy/funder differs from signer — expected for type 1/2/3'})")
    print("\nreading balance/allowance the CLOB attributes to this config …")
    print("  " + _balance(client, sig))
    print("\nInterpretation:")
    print("  • balance ≈ your real cash  → funder/sig/creds RECOGNIZED → a maker order should post ✅")
    print("  • balance = 0.00 / error    → CLOB doesn't see funds under this config (the 'deposit")
    print("                                 wallet / maker not allowed' block) → try sig_type=3 +")
    print("                                 funder=proxy, or the new-deposit-wallet path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
