#!/usr/bin/env python3
"""
Diagnostic — verify wallet count and Robinhood Chain ETH balances
without touching Notion. Addresses are masked (0x1234...abcd) — safe for
public repo logs.

Trigger via: Actions → Robinhood Chain Notion Tracker → Run workflow → mode=diagnostic
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error

RPC_URL         = os.environ.get("RH_PRIMARY_RPC", "https://rpc.mainnet.chain.robinhood.com").strip()
WALLETS_CSV     = os.environ.get("ETH_WALLETS_CSV", "")
RPC_TIMEOUT     = int(os.environ.get("RPC_TIMEOUT",      "30"))
RPC_RETRIES     = int(os.environ.get("RPC_RETRIES",      "5"))
RPC_BACKOFF_CAP = float(os.environ.get("RPC_BACKOFF_CAP", "30"))
CALL_DELAY      = float(os.environ.get("CALL_DELAY",      "0.5"))
USER_AGENT      = os.environ.get("RPC_USER_AGENT", "notion-tracker/1.0 (+https://github.com)").strip()

STABLE_CONTRACT = os.environ.get("STABLE_CONTRACT", "0xE246BC49b0598d7Cd9f0eAD48B885034f1254380").strip()
STABLE_DECIMALS = int(os.environ.get("STABLE_DECIMALS", "6"))
STABLE_SYMBOL   = os.environ.get("STABLE_SYMBOL", "USDT").strip()

ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
WEI_PER_ETH = 10 ** 18
BALANCEOF_SELECTOR = "0x70a08231"


def fail(msg): print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):  print(msg, flush=True)
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
    chunks = [c for c in re.split(r"[\s,;]+", (raw or "").strip()) if c]
    addr_like = [c for c in chunks if "0x" in c.lower()]
    rejected = [c for c in addr_like if not re.fullmatch(r"0x[0-9a-fA-F]{40}", c)]
    dupes = len(ADDR_RE.findall(raw or "")) - len(accepted)
    log(f"Entries in secret: {len(chunks)}  ->  accepted: {len(accepted)}"
        + (f"  (dupes collapsed: {dupes})" if dupes > 0 else ""))
    if rejected:
        log(f"WARNING: {len(rejected)} malformed address(es) REJECTED (ETH not counted):")
        for c in rejected[:20]:
            log(f"  rejected: {mask(c)}  (length {len(c)}, expected 42)")


def backoff(attempt):
    d = min(2 ** attempt + random.uniform(0, 0.8), RPC_BACKOFF_CAP)
    log(f"  Retrying in {d:.1f}s...")
    time.sleep(d)


def hex_to_int(h):
    if not h or h == "0x":
        return 0
    return int(h, 16) if isinstance(h, str) and h.startswith("0x") else int(h)


def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                RPC_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
            if data.get("error"):
                raise Exception(f"RPC error: {data['error']}")
            return data["result"]
        except urllib.error.HTTPError as e:
            try:    detail = e.read().decode()
            except: detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in {408, 425, 429, 500, 502, 503, 504}:
                backoff(attempt)
            else:
                raise Exception(last_err)
        except Exception as ex:
            last_err = str(ex)
            backoff(attempt)
    raise Exception(f"RPC failed: {last_err}")


def get_eth(wallet):
    return hex_to_int(rpc("eth_getBalance", [wallet, "latest"])) / WEI_PER_ETH


def get_stable(wallet):
    if not STABLE_CONTRACT:
        return 0.0
    data = BALANCEOF_SELECTOR + wallet.lower().replace("0x", "").rjust(64, "0")
    raw = rpc("eth_call", [{"to": STABLE_CONTRACT, "data": data}, "latest"])
    return hex_to_int(raw) / (10 ** STABLE_DECIMALS)


def main():
    wallets = parse_wallets(WALLETS_CSV)
    report_wallet_parse(WALLETS_CSV, wallets)
    if not wallets:
        fail("No valid 0x EVM addresses found in ETH_WALLETS_CSV")

    print("=" * 60)
    print(f"RPC:          {RPC_URL}")
    print(f"Wallets:      {len(wallets)}")
    print(f"Stablecoin:   {STABLE_SYMBOL} {mask(STABLE_CONTRACT)} (decimals={STABLE_DECIMALS})")
    print("=" * 60)

    rows = []
    total_eth, total_stable = 0.0, 0.0
    for i, wallet in enumerate(wallets, 1):
        eth = get_eth(wallet)
        stable = get_stable(wallet)
        rows.append((wallet, eth, stable))
        total_eth += eth
        total_stable += stable
        log(f"  [{i:3d}/{len(wallets)}] {eth:>14.6f} ETH  {stable:>12.2f} {STABLE_SYMBOL}  {mask(wallet)}")
        if i < len(wallets):
            time.sleep(CALL_DELAY)

    rows_sorted = sorted(rows, key=lambda x: x[1], reverse=True)
    zero = [w for w, e, s in rows if e == 0 and s == 0]

    print()
    print("=" * 60)
    print("SORTED BY ETH (descending)")
    print("=" * 60)
    for i, (w, e, s) in enumerate(rows_sorted, 1):
        print(f"  {i:3d}. {e:>14.6f} ETH  {s:>12.2f} {STABLE_SYMBOL}  {mask(w)}")

    print()
    print("=" * 60)
    print(f"Total ETH:        {total_eth:.6f}")
    print(f"Total {STABLE_SYMBOL:<10} {total_stable:.2f}")
    print(f"Wallets:          {len(wallets)}")
    print(f"Empty (0/0):      {len(zero)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
