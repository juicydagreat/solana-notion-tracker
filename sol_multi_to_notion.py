#!/usr/bin/env python3
"""
Solana → Notion daily balance tracker.
Zero external dependencies — pure Python stdlib.

All wallet addresses are masked in logs (AbCd...XyZ1) so this script
is safe to run on a public GitHub repository.

Required secrets:
  NOTION_TOKEN, NOTION_DB_PERWALLET, NOTION_DB_DAILYTOTAL,
  WALLETS_CSV, USDC_WALLET, USDC_MINT, TITLE_PROP_PERWALLET

RPC strategy:
  1. Try batch JSON-RPC via SOLANA_PRIMARY_RPC / SOLANA_FALLBACK_RPC
  2. If batch is blocked (403), fall back to sequential individual calls
     on api.mainnet-beta.solana.com with INDIVIDUAL_DELAY between each
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error
from datetime import datetime, timezone

import aud_prices

# ── Config ──────────────────────────────────────────────────────────────────────────────
NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET  = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV          = os.environ["WALLETS_CSV"]
USDC_MINT            = os.environ["USDC_MINT"].strip()
USDC_WALLET          = os.environ["USDC_WALLET"].strip()
TITLE_PROP           = os.environ.get("TITLE_PROP_PERWALLET", "Wallet").strip()
NOTION_VERSION       = "2022-06-28"

# AUD price columns. PRICE_COIN_ID is the CoinGecko id of the native asset.
PRICE_COIN_ID   = os.environ.get("PRICE_COIN_ID", "solana").strip()
PRICE_PROP      = os.environ.get("PRICE_PROP", "Price AUD").strip()
AUD_DELTA_PROP  = os.environ.get("AUD_DELTA_PROP", "AUD Delta").strip()

# The daily-total DB is shared with other trackers (e.g. ETH on Robinhood
# Chain). ASSET_LABEL isolates this tracker's rows so the day-over-day delta
# is computed against the previous SOL row only — not an ETH row or a manual
# note. Must appear in the daily-total row title (see create_page below).
ASSET_LABEL          = os.environ.get("ASSET_LABEL", "SOL").strip()
DAILYTOTAL_TITLE     = os.environ.get("TITLE_PROP_DAILYTOTAL", "Name").strip()

RPC_TIMEOUT      = int(os.environ.get("RPC_TIMEOUT",       "30"))
RPC_RETRIES      = int(os.environ.get("RPC_RETRIES",       "5"))
RPC_BACKOFF_CAP  = float(os.environ.get("RPC_BACKOFF_CAP",  "30.0"))
BATCH_SIZE       = int(os.environ.get("BATCH_SIZE",         "10"))
BATCH_PAUSE      = float(os.environ.get("BATCH_PAUSE",      "2.0"))
INDIVIDUAL_DELAY = float(os.environ.get("INDIVIDUAL_DELAY", "2.0"))

_rpc_primary  = os.environ.get("SOLANA_PRIMARY_RPC",  "https://solana-rpc.publicnode.com").strip()
_rpc_fallback = os.environ.get("SOLANA_FALLBACK_RPC", "https://rpc.ankr.com/solana").strip()
RPC_URLS = list(dict.fromkeys([_rpc_primary, _rpc_fallback]))

INDIVIDUAL_RPC = os.environ.get("INDIVIDUAL_RPC", "https://api.mainnet-beta.solana.com").strip()

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


# ── Helpers ─────────────────────────────────────────────────────────────────────────────
def fail(msg):  print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):   print(msg, flush=True)
def r2(x):      return None if x is None else round(float(x), 2)
def mask(addr): return f"{addr[:4]}...{addr[-4:]}" if len(addr) >= 8 else addr


def parse_wallets(raw):
    seen, out = set(), []
    for w in PUBKEY_RE.findall(raw or ""):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def backoff(attempt):
    d = min(2 ** attempt + random.uniform(0, 0.8), RPC_BACKOFF_CAP)
    log(f"  [backoff] {d:.1f}s")
    time.sleep(d)


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ── RPC ────────────────────────────────────────────────────────────────────────────────
_RETRY_CODES    = {408, 425, 429, 500, 502, 503, 504}
_NEXT_URL_CODES = {401, 403}


def _http_post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
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
                    log(f"  [{e.code}] batch blocked on {url}, trying next endpoint")
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


def single_rpc_call(payload):
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            data = _http_post(INDIVIDUAL_RPC, payload)
            if isinstance(data, dict) and data.get("error"):
                raise Exception(f"RPC error: {data['error']}")
            return data
        except urllib.error.HTTPError as e:
            try:    detail = e.read().decode()
            except: detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in _RETRY_CODES:
                backoff(attempt)
            else:
                raise Exception(last_err)
        except Exception as ex:
            last_err = str(ex)
            backoff(attempt)
    raise Exception(f"Individual RPC call failed: {last_err}")


def batch_get_sol(wallets):
    # Fast path: batch RPC
    try:
        results = {}
        indexed = list(enumerate(wallets))
        for i, chunk in enumerate(chunks(indexed, BATCH_SIZE)):
            if i > 0:
                time.sleep(BATCH_PAUSE)
            batch = [
                {"jsonrpc": "2.0", "id": idx, "method": "getBalance", "params": [w]}
                for idx, w in chunk
            ]
            resp = rpc_call(batch)
            if not isinstance(resp, list):
                raise Exception(f"Expected list from batch RPC, got: {type(resp)}")
            for item in resp:
                if item.get("error"):
                    raise Exception(f"getBalance error #{item['id']}: {item['error']}")
                results[item["id"]] = item["result"]["value"] / 1e9
        return [results[i] for i in range(len(wallets))]
    except Exception as batch_err:
        log(f"  Batch mode failed: {batch_err}")
        log(f"  Falling back to individual calls on {INDIVIDUAL_RPC}")
        log(f"  ({len(wallets)} wallets x {INDIVIDUAL_DELAY}s = ~{len(wallets)*INDIVIDUAL_DELAY:.0f}s)")

    # Fallback: individual calls
    results = []
    for i, wallet in enumerate(wallets):
        resp = single_rpc_call(
            {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
        )
        sol = resp["result"]["value"] / 1e9
        log(f"  [{i+1:02d}/{len(wallets)}] {sol:.4f} SOL  {mask(wallet)}")
        results.append(sol)
        if i < len(wallets) - 1:
            time.sleep(INDIVIDUAL_DELAY)
    return results


def get_usdc_balance(wallet):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
    }
    try:
        resp = rpc_call(payload)
    except Exception:
        log(f"  Batch RPC blocked for USDC, using individual fallback")
        resp = single_rpc_call(payload)

    total = 0.0
    for acc in resp.get("result", {}).get("value", []):
        try:
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            ui = ta.get("uiAmount")
            if ui is not None:
                total += float(ui)
            else:
                amt = int(ta.get("amount", 0))
                dec = int(ta.get("decimals", 0))
                total += amt / 10 ** dec if dec else float(amt)
        except Exception:
            continue
    return total


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
    log(f"Batch RPC: {RPC_URLS}")
    log(f"Fallback RPC: {INDIVIDUAL_RPC}")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV")

    today = datetime.now(timezone.utc).date().isoformat()
    log(f"Date: {today}  |  Wallets: {len(wallets)}")

    log(f"\n--- Fetching SOL balances ({len(wallets)} wallets) ---")
    sol_list = batch_get_sol(wallets)
    total_sol = r2(sum(sol_list))
    log(f"Total SOL: {total_sol}")

    time.sleep(BATCH_PAUSE)

    log(f"\n--- Fetching USDC balance ---")
    usdc_total = r2(get_usdc_balance(USDC_WALLET))
    log(f"  Total USDC: {usdc_total}")
    usdc_list = [usdc_total if w == USDC_WALLET else 0.0 for w in wallets]

    log(f"\n--- Fetching SOL price (AUD) ---")
    sol_price = r2(aud_prices.spot_aud([PRICE_COIN_ID]).get(PRICE_COIN_ID))
    log(f"  SOL price: {sol_price} AUD")

    log(f"\nSummary: Total SOL={total_sol}  Total USDC={usdc_total}  SOL/AUD={sol_price}")

    log(f"\n--- Ensuring Notion AUD columns exist ---")
    ensure_number_props(NOTION_DB_PERWALLET,  [PRICE_PROP, AUD_DELTA_PROP])
    ensure_number_props(NOTION_DB_DAILYTOTAL, [PRICE_PROP, AUD_DELTA_PROP])

    log(f"\n--- Fetching previous Notion rows ---")
    prev_lookup = get_prev_perwallet_rows(today, wallets)
    prev_total  = get_prev_total_row(today)
    log(f"  {len(prev_lookup)} previous per-wallet rows found")

    def aud_delta(delta):
        return r2(delta * sol_price) if (delta is not None and sol_price is not None) else None

    log(f"\n--- Writing {len(wallets)} per-wallet rows to Notion ---")
    for i, (w, sol, usdc) in enumerate(zip(wallets, sol_list, usdc_list), 1):
        sol  = r2(sol)
        usdc = r2(usdc)
        prev   = prev_lookup.get(w)
        d_sol  = r2(sol  - get_num(prev, "End Balance"))      if prev else None
        d_usdc = r2(usdc - get_num(prev, "USDC End Balance")) if prev else None
        d_aud  = aud_delta(d_sol)
        log(f"  [{i:02d}/{len(wallets)}] {mask(w)}  SOL={sol} \u0394{d_sol}  USDC={usdc} \u0394{d_usdc}  AUD\u0394{d_aud}")
        create_page(NOTION_DB_PERWALLET, {
            TITLE_PROP:         {"title": [{"text": {"content": w}}]},
            "Date":             {"date":  {"start": today}},
            "End Balance":      {"number": sol},
            "Delta":            {"number": d_sol},
            "USDC End Balance": {"number": usdc},
            "USDC Delta":       {"number": d_usdc},
            PRICE_PROP:         {"number": sol_price},
            AUD_DELTA_PROP:     {"number": d_aud},
        })

    log(f"\n--- Writing daily total row ---")
    p_sol_t  = get_num(prev_total, "End Balance")
    p_usdc_t = get_num(prev_total, "USDC End Balance")
    d_sol_t  = r2(total_sol  - p_sol_t)  if prev_total else None
    d_usdc_t = r2(usdc_total - p_usdc_t) if prev_total else None
    d_aud_t  = aud_delta(d_sol_t)
    log(f"  SOL={total_sol} \u0394{d_sol_t}  USDC={usdc_total} \u0394{d_usdc_t}  AUD\u0394{d_aud_t}")
    create_page(NOTION_DB_DAILYTOTAL, {
        DAILYTOTAL_TITLE:   {"title": [{"text": {"content": f"{total_sol:.2f} {ASSET_LABEL}"}}]},
        "Date":             {"date":  {"start": today}},
        "End Balance":      {"number": total_sol},
        "Delta":            {"number": d_sol_t},
        "USDC End Balance": {"number": usdc_total},
        "USDC Delta":       {"number": d_usdc_t},
        PRICE_PROP:         {"number": sol_price},
        AUD_DELTA_PROP:     {"number": d_aud_t},
    })
    log("\nDone.")


if __name__ == "__main__":
    main()
