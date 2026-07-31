#!/usr/bin/env python3
"""
Robinhood Chain (ETH L2) → Notion daily balance tracker.
Zero external dependencies — pure Python stdlib.

Mirrors sol_multi_to_notion.py but reads native ETH balances on
Robinhood Chain (an Ethereum Layer-2, chain id 4663) instead of SOL on
Solana. Optionally also tracks an ERC-20 stablecoin (defaults to
Tether USD / USDT on Robinhood Chain).

All wallet addresses are masked in logs (0x1234...abcd) so this script
is safe to run on a public GitHub repository.

Writes to the SAME Notion databases as the SOL tracker:
  End Balance / Delta           <- native ETH
  USDC End Balance / USDC Delta <- stablecoin (USDT by default)

Required secrets:
  NOTION_TOKEN, NOTION_DB_PERWALLET, NOTION_DB_DAILYTOTAL,
  ETH_WALLETS_CSV, TITLE_PROP_PERWALLET

Optional (have working defaults for Robinhood Chain):
  RH_PRIMARY_RPC, RH_FALLBACK_RPC,
  STABLE_CONTRACT, STABLE_DECIMALS, STABLE_SYMBOL

RPC notes:
  The public Robinhood Chain RPC rejects requests without a User-Agent
  header (returns 403), so every request sends one. Native balance comes
  from eth_getBalance; the stablecoin comes from an eth_call to the
  ERC-20 balanceOf(address) selector.
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error
from datetime import datetime, timezone

import aud_prices

# ── Config ──────────────────────────────────────────────────────────────────────────────
NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET  = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV          = os.environ["ETH_WALLETS_CSV"]
TITLE_PROP           = os.environ.get("TITLE_PROP_PERWALLET", "Wallet").strip()
NOTION_VERSION       = "2022-06-28"

# AUD price columns. PRICE_COIN_ID is the CoinGecko id of the native asset.
PRICE_COIN_ID   = os.environ.get("PRICE_COIN_ID", "ethereum").strip()
PRICE_PROP      = os.environ.get("PRICE_PROP", "Price AUD").strip()
AUD_DELTA_PROP  = os.environ.get("AUD_DELTA_PROP", "AUD Delta").strip()

# The daily-total DB is shared with the SOL tracker. ASSET_LABEL isolates
# this tracker's rows so the day-over-day delta is computed against the
# previous ETH row only. Must appear in the daily-total row title.
ASSET_LABEL          = os.environ.get("ASSET_LABEL", "ETH").strip()
DAILYTOTAL_TITLE     = os.environ.get("TITLE_PROP_DAILYTOTAL", "Name").strip()

# Stablecoin (ERC-20) — defaults to Tether USD (USDT) on Robinhood Chain.
STABLE_CONTRACT = os.environ.get("STABLE_CONTRACT", "0xE246BC49b0598d7Cd9f0eAD48B885034f1254380").strip()
STABLE_DECIMALS = int(os.environ.get("STABLE_DECIMALS", "6"))
STABLE_SYMBOL   = os.environ.get("STABLE_SYMBOL", "USDT").strip()

RPC_TIMEOUT      = int(os.environ.get("RPC_TIMEOUT",       "30"))
RPC_RETRIES      = int(os.environ.get("RPC_RETRIES",       "5"))
RPC_BACKOFF_CAP  = float(os.environ.get("RPC_BACKOFF_CAP",  "30.0"))
BATCH_SIZE       = int(os.environ.get("BATCH_SIZE",         "10"))
BATCH_PAUSE      = float(os.environ.get("BATCH_PAUSE",      "1.0"))
CALL_DELAY       = float(os.environ.get("CALL_DELAY",       "0.5"))

_rpc_primary  = os.environ.get("RH_PRIMARY_RPC",  "https://rpc.mainnet.chain.robinhood.com").strip()
_rpc_fallback = os.environ.get("RH_FALLBACK_RPC", "").strip()
RPC_URLS = list(dict.fromkeys([u for u in (_rpc_primary, _rpc_fallback) if u]))

USER_AGENT = os.environ.get("RPC_USER_AGENT", "notion-tracker/1.0 (+https://github.com)").strip()

# EVM address: 0x followed by 40 hex chars.
ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")

WEI_PER_ETH = 10 ** 18
# keccak256("balanceOf(address)")[:4]
BALANCEOF_SELECTOR = "0x70a08231"


# ── Helpers ─────────────────────────────────────────────────────────────────────────────
def fail(msg):  print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):   print(msg, flush=True)
def r2(x):      return None if x is None else round(float(x), 2)
def r6(x):      return None if x is None else round(float(x), 6)
def mask(addr): return f"{addr[:6]}...{addr[-4:]}" if len(addr) >= 10 else addr


def parse_wallets(raw):
    seen, out = set(), []
    for w in ADDR_RE.findall(raw or ""):
        key = w.lower()
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def report_wallet_parse(raw, accepted):
    """Surface silently-dropped wallets — the usual cause of a total being
    'off by a few ETH' after the wallet list is edited."""
    chunks = [c for c in re.split(r"[\s,;]+", (raw or "").strip()) if c]
    addr_like = [c for c in chunks if "0x" in c.lower()]
    rejected = [c for c in addr_like if not re.fullmatch(r"0x[0-9a-fA-F]{40}", c)]
    total_matches = len(ADDR_RE.findall(raw or ""))
    dupes = total_matches - len(accepted)
    log(f"  Wallet parse: {len(chunks)} entries in secret -> {len(accepted)} valid 0x addresses accepted")
    if dupes > 0:
        log(f"  note: {dupes} duplicate address(es) collapsed")
    if rejected:
        log(f"  WARNING: {len(rejected)} entr(y/ies) look like an address but were REJECTED as malformed:")
        for c in rejected[:20]:
            log(f"    rejected: {mask(c)}  (length {len(c)}, expected 42)")
        log("  -> fix these in the ETH_WALLETS_CSV secret; their ETH is NOT being counted.")


def backoff(attempt):
    d = min(2 ** attempt + random.uniform(0, 0.8), RPC_BACKOFF_CAP)
    log(f"  [backoff] {d:.1f}s")
    time.sleep(d)


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def hex_to_int(h):
    if h is None:
        return 0
    if isinstance(h, str):
        return int(h, 16) if h.startswith("0x") else int(h)
    return int(h)


# ── RPC ────────────────────────────────────────────────────────────────────────────────
_RETRY_CODES    = {408, 425, 429, 500, 502, 503, 504}
_NEXT_URL_CODES = {401, 403}


def _http_post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace") or "{}")


def rpc_call(payload):
    last_err = None
    for url_idx, url in enumerate(RPC_URLS):
        if url_idx > 0:
            log(f"  [fallback] switching to {url}")
        skip_to_next = False
        for attempt in range(RPC_RETRIES):
            try:
                data = _http_post(url, payload)
                if isinstance(data, dict) and data.get("error"):
                    raise Exception(f"RPC error: {data['error']}")
                return data
            except urllib.error.HTTPError as e:
                try:    detail = e.read().decode()
                except: detail = ""
                last_err = f"HTTP {e.code}: {detail}"
                if e.code in _NEXT_URL_CODES:
                    log(f"  [{e.code}] blocked on {url}, trying next endpoint")
                    skip_to_next = True
                    break
                elif e.code in _RETRY_CODES:
                    log(f"  [{e.code}] retrying {url}")
                    backoff(attempt)
                else:
                    raise Exception(last_err)
            except Exception as ex:
                last_err = str(ex)
                log(f"  [rpc] {last_err}")
                backoff(attempt)
        if skip_to_next:
            continue
    raise Exception(f"All RPC endpoints failed. Last error: {last_err}")


def _resolve_batch(resp):
    """Turn a JSON-RPC batch response (list) into an id->result dict."""
    if not isinstance(resp, list):
        raise Exception(f"Expected list from batch RPC, got: {type(resp)}")
    out = {}
    for item in resp:
        if item.get("error"):
            raise Exception(f"batch error #{item.get('id')}: {item['error']}")
        out[item["id"]] = item.get("result")
    return out


def get_eth_balances(wallets):
    """Native ETH balance for each wallet (in ETH)."""
    results = {}
    indexed = list(enumerate(wallets))
    for i, chunk in enumerate(chunks(indexed, BATCH_SIZE)):
        if i > 0:
            time.sleep(BATCH_PAUSE)
        batch = [
            {"jsonrpc": "2.0", "id": idx, "method": "eth_getBalance",
             "params": [w, "latest"]}
            for idx, w in chunk
        ]
        resolved = _resolve_batch(rpc_call(batch))
        for idx, _ in chunk:
            if idx not in resolved:
                raise Exception(f"eth_getBalance missing id {idx}")
            results[idx] = hex_to_int(resolved[idx]) / WEI_PER_ETH
    return [results[i] for i in range(len(wallets))]


def _balanceof_data(wallet):
    return BALANCEOF_SELECTOR + wallet.lower().replace("0x", "").rjust(64, "0")


def get_stable_balances(wallets):
    """ERC-20 stablecoin balance for each wallet (in token units)."""
    if not STABLE_CONTRACT:
        return [0.0 for _ in wallets]
    scale = 10 ** STABLE_DECIMALS
    results = {}
    indexed = list(enumerate(wallets))
    for i, chunk in enumerate(chunks(indexed, BATCH_SIZE)):
        if i > 0:
            time.sleep(BATCH_PAUSE)
        batch = [
            {"jsonrpc": "2.0", "id": idx, "method": "eth_call",
             "params": [{"to": STABLE_CONTRACT, "data": _balanceof_data(w)}, "latest"]}
            for idx, w in chunk
        ]
        resolved = _resolve_batch(rpc_call(batch))
        for idx, _ in chunk:
            raw = resolved.get(idx)
            # Empty result (0x) means no token account / zero balance.
            results[idx] = hex_to_int(raw) / scale if raw and raw != "0x" else 0.0
    return [results[i] for i in range(len(wallets))]


# ── Notion ─────────────────────────────────────────────────────────────────────────────
def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


NOTION_TIMEOUT = int(os.environ.get("NOTION_TIMEOUT", "30"))
NOTION_RETRIES = int(os.environ.get("NOTION_RETRIES", "5"))
_NOTION_RETRY_HTTP = {429, 500, 502, 503, 504}


def _notion_http(url, data, method):
    """Send a Notion request with retries on timeouts / transient errors."""
    last_err = None
    for attempt in range(NOTION_RETRIES):
        try:
            req = urllib.request.Request(url, data=data, headers=notion_headers(), method=method)
            with urllib.request.urlopen(req, timeout=NOTION_TIMEOUT) as r:
                res = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
                if isinstance(res, dict) and res.get("object") == "error":
                    raise Exception(f"Notion error: {res}")
                return res
        except urllib.error.HTTPError as e:
            try:    detail = e.read().decode()
            except: detail = ""
            if e.code in _NOTION_RETRY_HTTP:
                last_err = f"HTTP {e.code}"
                log(f"  [notion] {last_err}, retrying")
                backoff(attempt)
            else:
                raise Exception(f"Notion HTTP {e.code}: {detail}")
        except Exception as ex:  # URLError, socket timeout, TimeoutError, etc.
            last_err = str(ex)
            log(f"  [notion] {last_err}, retrying")
            backoff(attempt)
    raise Exception(f"Notion request failed after {NOTION_RETRIES} tries: {last_err}")


def notion_req(url, body, method="POST"):
    return _notion_http(url, json.dumps(body).encode(), method)


def notion_get(url):
    return _notion_http(url, None, "GET")


def ensure_number_props(db_id, names, number_format="australian_dollar"):
    """Create any missing number properties on a database (idempotent)."""
    db = notion_get(f"https://api.notion.com/v1/databases/{db_id}")
    existing = set(db.get("properties", {}).keys())
    missing = [n for n in names if n not in existing]
    if not missing:
        return
    log(f"  Adding Notion columns to {db_id[:8]}...: {missing}")
    props = {n: {"number": {"format": number_format}} for n in missing}
    notion_req(f"https://api.notion.com/v1/databases/{db_id}", {"properties": props}, method="PATCH")


def notion_query_paginated(db_id, body):
    rows, cursor = [], None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def get_prev_perwallet_rows(today, wallets=None):
    """Latest row before `today` for each wallet. Stops paginating as soon as
    every wallet in `wallets` has been found — so it reads ~1-2 pages in the
    normal case instead of the entire (now huge) history."""
    want = set(wallets) if wallets else None
    lookup, cursor = {}, None
    while True:
        body = {
            "filter": {"property": "Date", "date": {"before": today}},
            "sorts":  [{"property": "Date", "direction": "descending"}],
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        res = notion_req(f"https://api.notion.com/v1/databases/{NOTION_DB_PERWALLET}/query", body)
        for page in res.get("results", []):
            try:
                w = page["properties"][TITLE_PROP]["title"][0]["plain_text"]
                if w not in lookup:
                    lookup[w] = page
            except (KeyError, IndexError):
                continue
        if want and want.issubset(lookup.keys()):
            break
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return lookup


def get_prev_total_row(today):
    res = notion_req(
        f"https://api.notion.com/v1/databases/{NOTION_DB_DAILYTOTAL}/query",
        {
            "filter": {"and": [
                {"property": "Date", "date": {"before": today}},
                {"property": DAILYTOTAL_TITLE, "title": {"contains": ASSET_LABEL}},
            ]},
            "sorts":  [{"property": "Date", "direction": "descending"}],
            "page_size": 1,
        },
    )
    return (res.get("results") or [None])[0]


def get_num(page, prop):
    if page is None:
        return 0.0
    try:
        v = page["properties"][prop]["number"]
        return float(v) if v is not None else 0.0
    except (KeyError, TypeError):
        return 0.0


def create_page(db_id, props):
    notion_req(
        "https://api.notion.com/v1/pages",
        {"parent": {"database_id": db_id}, "properties": props},
    )


# ── Main ──────────────────────────────────────────────────────────────────────────────
def main():
    log("=" * 60)
    log("Robinhood Chain (ETH L2) → Notion tracker")
    log(f"RPC: {RPC_URLS}")
    log(f"Stablecoin: {STABLE_SYMBOL} {mask(STABLE_CONTRACT)} (decimals={STABLE_DECIMALS})")

    wallets = parse_wallets(WALLETS_CSV)
    report_wallet_parse(WALLETS_CSV, wallets)
    if not wallets:
        fail("No valid 0x EVM addresses found in ETH_WALLETS_CSV")

    today = datetime.now(timezone.utc).date().isoformat()
    log(f"Date: {today}  |  Wallets: {len(wallets)}")

    log(f"\n--- Fetching native ETH balances ({len(wallets)} wallets) ---")
    eth_list = get_eth_balances(wallets)
    total_eth = r6(sum(eth_list))
    log(f"Total ETH: {total_eth}")

    time.sleep(CALL_DELAY)

    log(f"\n--- Fetching {STABLE_SYMBOL} balances ---")
    stable_list = get_stable_balances(wallets)
    total_stable = r2(sum(stable_list))
    log(f"Total {STABLE_SYMBOL}: {total_stable}")

    log(f"\n--- Fetching ETH price (AUD) ---")
    eth_price = r2(aud_prices.spot_aud([PRICE_COIN_ID]).get(PRICE_COIN_ID))
    log(f"  ETH price: {eth_price} AUD")

    log(f"\nSummary: Total ETH={total_eth}  Total {STABLE_SYMBOL}={total_stable}  ETH/AUD={eth_price}")

    log(f"\n--- Ensuring Notion AUD columns exist ---")
    ensure_number_props(NOTION_DB_PERWALLET,  [PRICE_PROP, AUD_DELTA_PROP])
    ensure_number_props(NOTION_DB_DAILYTOTAL, [PRICE_PROP, AUD_DELTA_PROP])

    log(f"\n--- Fetching previous Notion rows ---")
    prev_lookup = get_prev_perwallet_rows(today, wallets)
    prev_total  = get_prev_total_row(today)
    log(f"  {len(prev_lookup)} previous per-wallet rows found")

    def aud_delta(delta):
        return r2(delta * eth_price) if (delta is not None and eth_price is not None) else None

    log(f"\n--- Writing {len(wallets)} per-wallet rows to Notion ---")
    for i, (w, eth, stable) in enumerate(zip(wallets, eth_list, stable_list), 1):
        eth    = r6(eth)
        stable = r2(stable)
        prev     = prev_lookup.get(w)
        d_eth    = r6(eth    - get_num(prev, "End Balance"))      if prev else None
        d_stable = r2(stable - get_num(prev, "USDC End Balance")) if prev else None
        d_aud    = aud_delta(d_eth)
        log(f"  [{i:02d}/{len(wallets)}] {mask(w)}  ETH={eth} Δ{d_eth}  {STABLE_SYMBOL}={stable} Δ{d_stable}  AUDΔ{d_aud}")
        create_page(NOTION_DB_PERWALLET, {
            TITLE_PROP:         {"title": [{"text": {"content": w}}]},
            "Date":             {"date":  {"start": today}},
            "End Balance":      {"number": eth},
            "Delta":            {"number": d_eth},
            "USDC End Balance": {"number": stable},
            "USDC Delta":       {"number": d_stable},
            PRICE_PROP:         {"number": eth_price},
            AUD_DELTA_PROP:     {"number": d_aud},
        })

    log(f"\n--- Writing daily total row ---")
    p_eth_t    = get_num(prev_total, "End Balance")
    p_stable_t = get_num(prev_total, "USDC End Balance")
    d_eth_t    = r6(total_eth    - p_eth_t)    if prev_total else None
    d_stable_t = r2(total_stable - p_stable_t) if prev_total else None
    d_aud_t    = aud_delta(d_eth_t)
    log(f"  ETH={total_eth} Δ{d_eth_t}  {STABLE_SYMBOL}={total_stable} Δ{d_stable_t}  AUDΔ{d_aud_t}")
    create_page(NOTION_DB_DAILYTOTAL, {
        DAILYTOTAL_TITLE:   {"title": [{"text": {"content": f"{total_eth:.4f} {ASSET_LABEL}"}}]},
        "Date":             {"date":  {"start": today}},
        "End Balance":      {"number": total_eth},
        "Delta":            {"number": d_eth_t},
        "USDC End Balance": {"number": total_stable},
        "USDC Delta":       {"number": d_stable_t},
        PRICE_PROP:         {"number": eth_price},
        AUD_DELTA_PROP:     {"number": d_aud_t},
    })
    log("\nDone.")


if __name__ == "__main__":
    main()
