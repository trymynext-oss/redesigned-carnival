#!/usr/bin/env python3
"""
latest_evm_tokens.py — v8 (FINAL)
Discovery : DexScreener token-profiles?chainId= (eth + base)
            + Bankr API (Base-only newest launches, no auth)
Source    : Etherscan V2 getsourcecode (works on free tier for ALL chains)
Output    : tokens_with_code.txt (name\n<full source>\n\n) + token_names.txt

Note: Etherscan free tier no longer supports getLogs/eth_blockNumber on Base.
      We only use it for getsourcecode, which is still free-tier compatible.
"""

import os, json, time, requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# ── CONFIG ────────────────────────────────────────────────────
MIN_LIQ_START = 50_000
MIN_LIQ_FLOOR = 10_000
MAX_TOKENS    = 10
MAX_AGE_DAYS  = 180

CHAIN_IDS = {"ethereum": 1, "base": 8453}
DEX_CHAIN_SLUGS = {"ethereum": "ethereum", "base": "base"}

TOKENS_WITH_CODE = Path("tokens_with_code.txt")
TOKEN_NAMES      = Path("token_names.txt")

ETHERSCAN_KEY = os.environ.get("ETHERSCAN_KEY", "")
ETHERSCAN_V2  = "https://api.etherscan.io/v2/api"

# Free, no-key discovery endpoints
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKEN    = "https://api.dexscreener.com/latest/dex/tokens/{addr}"
BANKR_LAUNCHES = "https://api.bankr.bot/token-launches"

DEX_SLEEP = 0.25
ES_SLEEP  = 0.35

KNOWN_QUOTES = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "0x6b175474e89094c44da98b954eedeac495271d0f",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
    "0x4200000000000000000000000000000000000006",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
    "0x0000000000000000000000000000000000000000",
}

# ── DEDUP ─────────────────────────────────────────────────────
def load_known_names() -> set:
    if not TOKEN_NAMES.exists(): return set()
    return {ln.strip().lower() for ln in
            TOKEN_NAMES.read_text(encoding="utf-8").splitlines() if ln.strip()}

def append_token_name(n):
    with TOKEN_NAMES.open("a", encoding="utf-8") as f: f.write(n + "\n")

def append_token_with_code(n, src):
    with TOKENS_WITH_CODE.open("a", encoding="utf-8") as f:
        f.write(n.rstrip() + "\n" + src.rstrip() + "\n\n")

# ── HELPERS ───────────────────────────────────────────────────
def _fmt(ms):
    if not ms: return "?"
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc)\
        .strftime("%Y-%m-%d %H:%M UTC")

def _age(ms):
    if not ms: return "?"
    s = int((time.time()*1000 - ms)/1000)
    if s < 3600:     return f"{s//60}m ago"
    if s < 86400:    return f"{s//3600}h ago"
    if s < 86400*7:  return f"{s//86400}d ago"
    if s < 86400*30: return f"{s//(86400*7)}w ago"
    return f"{s//(86400*30)}mo ago"

# ── 1a. DISCOVERY via DexScreener profiles (chainId filter) ──
def fetch_profiles(chain: str) -> list:
    """GET /token-profiles/latest/v1?chainId=ethereum|base"""
    try:
        r = requests.get(DEX_PROFILES, params={"chainId": chain}, timeout=20)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list): items = []
    except Exception as e:
        print(f"    [!] profiles({chain}): {e}")
        return []

    out, seen = [], set()
    for it in items:
        addr = (it.get("tokenAddress") or "").lower()
        if not addr or addr in KNOWN_QUOTES or addr in seen:
            continue
        seen.add(addr)
        out.append({"chain": chain, "address": addr})
    print(f"    [~] profiles({chain}): {len(items)} raw → {len(out)} unique")
    return out

# ── 1b. DISCOVERY via Bankr API (Base-only) ───────────────────
def fetch_bankr_launches() -> list:
    """GET api.bankr.bot/token-launches — newest Base launches, no auth."""
    try:
        r = requests.get(BANKR_LAUNCHES, timeout=20)
        r.raise_for_status()
        data = r.json()
        launches = data.get("launches", []) or []
    except Exception as e:
        print(f"    [!] Bankr: {e}")
        return []

    out, seen = [], set()
    for l in launches:
        if (l.get("chain") or "").lower() != "base":
            continue
        if (l.get("status") or "") != "deployed":
            continue
        addr = (l.get("tokenAddress") or "").lower()
        if not addr or addr in KNOWN_QUOTES or addr in seen:
            continue
        seen.add(addr)
        out.append({
            "chain": "base",
            "address": addr,
            "name": l.get("tokenName") or "UNKNOWN",
            "symbol": l.get("tokenSymbol") or "UNKNOWN",
            "deploy_ts": l.get("timestamp") or 0,
        })
    print(f"    [~] Bankr: {len(launches)} raw → {len(out)} unique Base launches")
    return out

# ── 2. ENRICH with liquidity ──────────────────────────────────
def enrich_with_pairs(chain: str, addr: str, min_liq: float) -> Optional[dict]:
    try:
        r = requests.get(DEX_TOKEN.format(addr=addr), timeout=20)
        if r.status_code == 429:
            time.sleep(2); return None
        r.raise_for_status()
        pairs = r.json().get("pairs", []) or []
    except Exception:
        return None

    best = None
    for p in pairs:
        if (p.get("chainId") or "").lower() != chain:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq < min_liq:
            continue
        if best is None or liq > best["liquidity_usd"]:
            base  = p.get("baseToken",  {}) or {}
            quote = p.get("quoteToken", {}) or {}
            ba = (base.get("address")  or "").lower()
            qa = (quote.get("address") or "").lower()
            if ba in KNOWN_QUOTES and qa not in KNOWN_QUOTES:
                tok, ta = quote, qa
            else:
                tok, ta = base,  ba
            best = {
                "name":    tok.get("name")   or tok.get("symbol") or "UNKNOWN",
                "symbol":  tok.get("symbol") or "UNKNOWN",
                "address": ta or addr,
                "chain":   chain,
                "liquidity_usd": liq,
                "pool_address": p.get("pairAddress", ""),
                "dex":     p.get("dexId", ""),
                "created_ms": p.get("pairCreatedAt") or 0,
            }
    if best:
        best["created_at"] = _fmt(best["created_ms"])
        best["age"]        = _age(best["created_ms"])
    return best

# ── 3. SOURCE via Etherscan (free tier OK for getsourcecode) ──
def _es(params):
    for attempt in range(4):
        try:
            r = requests.get(ETHERSCAN_V2, params=params, timeout=25)
            if r.status_code == 429:
                time.sleep(2**attempt); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"    [!] ES try {attempt+1}: {e}")
            time.sleep(1+attempt)
    return None

def fetch_source(chain: str, address: str) -> Optional[str]:
    cid = CHAIN_IDS.get(chain)
    if cid is None or not ETHERSCAN_KEY: return None
    d = _es({"module":"contract","action":"getsourcecode",
             "address":address,"chainid":cid,"apikey":ETHERSCAN_KEY})
    time.sleep(ES_SLEEP)
    if not d or d.get("status") != "1" or not d.get("result"): return None
    src = (d["result"][0].get("SourceCode") or "").strip()
    if not src: return None
    if src.startswith("{"):
        try:
            p = json.loads(src)
            if isinstance(p, dict) and "sources" in p:
                src = "\n".join(f"// ── FILE: {fn} ──\n{fd.get('content','')}\n"
                                for fn, fd in p["sources"].items())
        except json.JSONDecodeError: pass
    return src

# ── 4. MAIN ───────────────────────────────────────────────────
def discover_all(min_liq: float) -> list:
    # Primary: DexScreener profiles (eth + base)
    addrs = []
    for ch in DEX_CHAIN_SLUGS:
        addrs += fetch_profiles(ch)

    # Supplement: Bankr (Base-only, newest launches)
    bankr = fetch_bankr_launches()
    existing = {a["address"] for a in addrs}
    for b in bankr:
        if b["address"] not in existing:
            addrs.append({"chain": b["chain"], "address": b["address"]})

    print(f"    [~] {len(addrs)} total candidate addresses")

    found = []
    for a in addrs:
        pr = enrich_with_pairs(a["chain"], a["address"], min_liq)
        if pr: found.append(pr)
        time.sleep(DEX_SLEEP)
    return found

def main():
    if not ETHERSCAN_KEY:
        print("[!] export ETHERSCAN_KEY=..."); return

    known = load_known_names()
    print(f"[*] Known names: {len(known)}")

    candidates = []
    for th in (MIN_LIQ_START, 35_000, 20_000, MIN_LIQ_FLOOR):
        print(f"\n[*] Sweep @ >=${th:,.0f}")
        candidates = discover_all(th)
        seen, uniq = set(), []
        for t in sorted(candidates, key=lambda x: x.get("created_ms") or 0, reverse=True):
            if t["address"] in seen: continue
            seen.add(t["address"]); uniq.append(t)
        candidates = uniq
        print(f"  → {len(candidates)} candidate(s)")
        if len(candidates) >= MAX_TOKENS: break

    print(f"\n[*] {len(candidates)} total candidate(s)\n")

    processed = 0
    for t in candidates:
        if processed >= MAX_TOKENS: break
        if t["name"].lower() in known:
            print(f"[-] Skip {t['name']} (known)"); continue

        print(f"[+] {t['name']} ({t['symbol']}) on {t['chain']}")
        print(f"    addr:     {t['address']}")
        print(f"    liq:      ${t['liquidity_usd']:,.0f}")
        print(f"    dex:      {t['dex']}  pool: {t['pool_address']}")
        print(f"    created:  {t['created_at']}  ({t['age']})")

        src = fetch_source(t["chain"], t["address"])
        if not src:
            print("    [-] no verified source — skip\n"); continue
        append_token_with_code(t["name"], src)
        append_token_name(t["name"])
        known.add(t["name"].lower())
        processed += 1
        print(f"    [✓] appended {len(src):,} chars\n")

    print(f"[*] Done. Processed {processed}.")
    print(f"    {TOKEN_NAMES}       (names)")
    print(f"    {TOKENS_WITH_CODE}  (name + code)")

if __name__ == "__main__":
    main()