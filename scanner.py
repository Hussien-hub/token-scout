#!/usr/bin/env python3
"""
Token Scout - finds freshly launched DEX tokens, filters them, runs security
checks and scores each one 0-100. Writes a dashboard (output/index.html) every
cycle and can send Telegram alerts for high scorers.

Data sources (all free, no API key):
  GeckoTerminal  new + trending pools per chain, and fresh data for watched pools
  DexScreener    website / socials / boosts for each token
  GoPlus         contract security for EVM chains
  RugCheck       contract security for Solana
  Solana RPC     dev & insider behaviour of pump.fun tokens (onchain.py, background thread)

Usage:
  python3 scanner.py            # run forever, one scan every N minutes
  python3 scanner.py --once     # single scan, then exit
  python3 scanner.py --demo     # build the dashboard from example data (no network)

Only the Python standard library is used.
"""

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import onchain

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    """KEY=VALUE lines from .env into the environment; real environment variables win."""
    try:
        with open(path) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and not k.startswith("#"):
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass


load_env(os.path.join(HERE, ".env"))

# GeckoTerminal network id -> (DexScreener chain id, GoPlus chain id or "solana", explorer token URL)
CHAINS = {
    "solana":      ("solana",   "solana", "https://solscan.io/token/{}"),
    "eth":         ("ethereum", "1",      "https://etherscan.io/token/{}"),
    "bsc":         ("bsc",      "56",     "https://bscscan.com/token/{}"),
    "base":        ("base",     "8453",   "https://basescan.org/token/{}"),
    "arbitrum":    ("arbitrum", "42161",  "https://arbiscan.io/token/{}"),
    "polygon_pos": ("polygon",  "137",    "https://polygonscan.com/token/{}"),
}

UA = {"User-Agent": "token-scout/1.0", "Accept": "application/json", "Content-Type": "application/json"}


# ---------------------------------------------------------------- http helpers

class RateLimiter:
    """Keeps a minimum gap between calls to the same host."""

    def __init__(self, gaps):
        self.gaps = gaps
        self.next = {}
        self.lock = threading.Lock()

    def wait(self, host):
        # the scan and the on-chain worker share this from two threads: reserve a slot, sleep outside the lock
        with self.lock:
            t = max(time.time(), self.next.get(host, 0))
            self.next[host] = t + self.gaps.get(host, 0.3)
        time.sleep(max(0, t - time.time()))


LIMITER = RateLimiter({
    "api.geckoterminal.com": 2.2,   # free tier ~30 req/min
    "api.dexscreener.com": 0.4,
    "api.gopluslabs.io": 1.2,
    "api.rugcheck.xyz": 1.0,
    "api.mainnet-beta.solana.com": 0.5,   # public RPC; faster RPCs (SOLANA_RPC_URL) use the 0.3 s default
})


def get_json(url, retries=3, body=None):
    """GET, or POST when body is given. None after retries; logs never show the
    query string, where RPC providers put API keys."""
    host = urllib.parse.urlparse(url).netloc
    for attempt in range(retries):
        LIMITER.wait(host)
        try:
            req = urllib.request.Request(url, data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            log(f"HTTP {e.code} for {url.split('?')[0]}")
            return None
        except Exception as e:  # network errors, bad json
            log(f"request failed ({e.__class__.__name__}) for {url.split('?')[0]}")
            time.sleep(2 * (attempt + 1))
    return None


onchain.http = get_json


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fnum(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 1. discovery

GT = "https://api.geckoterminal.com/api/v2/networks"


def fetch_new_pools(network, pages):
    pools = []
    for page in range(1, pages + 1):
        data = get_json(f"{GT}/{network}/new_pools?page={page}&include=base_token,dex")
        if not data:
            break
        pools += parse_pools(data, network)
    return pools


def fetch_trending_pools(network):
    """Active pools of any age; catches tokens that left the new-pools pages
    (a few minutes on Solana) before they were old enough to list."""
    data = get_json(f"{GT}/{network}/trending_pools?duration=1h&include=base_token,dex")
    return parse_pools(data, network) if data else []


def fetch_pools(network, addresses):
    """Current data for pools already on the watch list, 30 per call."""
    pools = []
    for i in range(0, len(addresses), 30):
        data = get_json(f"{GT}/{network}/pools/multi/{','.join(addresses[i:i + 30])}"
                        f"?include=base_token,dex")
        if data:
            pools += parse_pools(data, network)
    return pools


def parse_pools(data, network):
    pools = []
    tokens = {}
    for inc in data.get("included", []) or []:
        if inc.get("type") == "token":
            tokens[inc["id"]] = inc.get("attributes", {})
    for p in data.get("data", []) or []:
        a = p.get("attributes", {})
        rel = p.get("relationships", {})
        tok_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
        dex_id = (rel.get("dex", {}).get("data") or {}).get("id", "")
        tok = tokens.get(tok_id, {})
        tx_h1 = (a.get("transactions") or {}).get("h1") or {}
        tx_m5 = (a.get("transactions") or {}).get("m5") or {}
        vol = a.get("volume_usd") or {}
        pc = a.get("price_change_percentage") or {}
        token_addr = tok.get("address") or tok_id.split("_", 1)[-1]
        pools.append({
            "network": network,
            "pool": a.get("address"),
            "pair_name": a.get("name", ""),
            "token": token_addr,
            "name": tok.get("name") or a.get("name", "").split(" / ")[0],
            "symbol": tok.get("symbol") or a.get("name", "").split(" / ")[0],
            "dex": dex_id,
            "created": a.get("pool_created_at"),
            "price_usd": fnum(a.get("base_token_price_usd")),
            "liquidity": fnum(a.get("reserve_in_usd")),
            "fdv": fnum(a.get("fdv_usd")),
            "mcap": fnum(a.get("market_cap_usd")),
            "vol_m5": fnum(vol.get("m5")),
            "vol_h1": fnum(vol.get("h1")),
            "vol_h24": fnum(vol.get("h24")),
            "chg_m5": fnum(pc.get("m5")),
            "chg_h1": fnum(pc.get("h1")),
            "chg_h24": fnum(pc.get("h24")),
            "buys_h1": int(fnum(tx_h1.get("buys"))),
            "sells_h1": int(fnum(tx_h1.get("sells"))),
            "buyers_h1": int(fnum(tx_h1.get("buyers"))),
            "sellers_h1": int(fnum(tx_h1.get("sellers"))),
            "buys_m5": int(fnum(tx_m5.get("buys"))),
            "sells_m5": int(fnum(tx_m5.get("sells"))),
        })
    return pools


def age_minutes(created_iso):
    if not created_iso:
        return None
    try:
        dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except ValueError:
        return None


# ---------------------------------------------------------------- 2. hard filters

def passes_filters(p, f):
    # a token is at least as old as this pool, so "too old" is safe to decide here;
    # "too new" waits for age_reasons() once the token's first pool is known
    age = age_minutes(p["created"])
    p["age_min"] = age
    reasons = []
    if age is not None and age > f["max_age_hours"] * 60:
        reasons.append("too old")
    if p["liquidity"] < f["min_liquidity_usd"]:
        reasons.append("low liquidity")
    if p["liquidity"] > f["max_liquidity_usd"]:
        reasons.append("liquidity above range")
    fdv = p["fdv"] or p["mcap"]
    if fdv and fdv < f["min_fdv_usd"]:
        reasons.append("fdv too small")
    if fdv and fdv > f["max_fdv_usd"]:
        reasons.append("fdv too large")
    if p["vol_h1"] < f["min_volume_h1_usd"]:
        reasons.append("low volume")
    if p["buys_h1"] + p["sells_h1"] < f["min_txns_h1"]:
        reasons.append("few txns")
    return not reasons, reasons


def age_reasons(p, f):
    """Age filters on the token's real age, after enrich_dexscreener() has set it."""
    age = p["age_min"]
    if age is None:
        return ["unknown age"]
    if age < f["min_age_minutes"]:
        return ["too new"]
    if age > f["max_age_hours"] * 60:
        return ["too old"]
    return []


# ---------------------------------------------------------------- 3. enrichment

def first_pool_gt(network, token):
    """Creation time (epoch ms) of the oldest of the token's top 20 pools on GeckoTerminal."""
    data = get_json(f"{GT}/{network}/tokens/{token}/pools")
    times = []
    for x in (data or {}).get("data") or []:
        ts = iso_ts(((x.get("attributes") or {}).get("pool_created_at") or "").replace("Z", "+00:00"))
        if ts:
            times.append(ts)
    return min(times) * 1000 if times else None


def enrich_dexscreener(pools):
    """Adds websites/socials/boosts from DexScreener, and sets created/age_min to the
    time the token's first pool opened on any DEX (the scanned pool may be a newer one).
    One call per token: the multi-token endpoint returns at most 30 pairs in total."""
    info = {}
    for p in pools:
        key = (p["network"], p["token"].lower())
        if not p["token"] or key in info:
            continue
        chain = CHAINS.get(p["network"], (p["network"],))[0]
        data = get_json(f"https://api.dexscreener.com/token-pairs/v1/{chain}/{p['token']}")
        pairs = data if isinstance(data, list) else []
        d = {}
        first_ms = None
        for pair in pairs:
            created_ms = pair.get("pairCreatedAt")
            if created_ms and (first_ms is None or created_ms < first_ms):
                first_ms = created_ms
            if (pair.get("baseToken") or {}).get("address", "").lower() != key[1]:
                continue
            d["pairs"] = d.get("pairs", 0) + 1
            liq = fnum((pair.get("liquidity") or {}).get("usd"))
            if "_liq" in d and d["_liq"] >= liq and d["socials"]:
                continue
            i_ = pair.get("info") or {}
            d.update({
                "_liq": liq,
                "websites": [w.get("url") for w in i_.get("websites") or [] if w.get("url")],
                "socials": [{"type": (s.get("type") or s.get("platform") or "").lower(),
                             "url": s.get("url")} for s in i_.get("socials") or [] if s.get("url")],
                "image": i_.get("imageUrl"),
                "boosts": int(fnum((pair.get("boosts") or {}).get("active"))),
                "has_profile": bool(i_),
                "ds_url": pair.get("url"),
            })
        # DexScreener lists at most 30 pools per token and may not know a token yet;
        # then GeckoTerminal's pool list for the token can hold an older pool
        if not pairs or len(pairs) >= 30:
            gt_ms = first_pool_gt(p["network"], p["token"])
            if gt_ms and (first_ms is None or gt_ms < first_ms):
                first_ms = gt_ms
        if first_ms:
            d["first_pool"] = datetime.fromtimestamp(first_ms / 1000, timezone.utc).isoformat()
        info[key] = d
    for p in pools:
        d = info.get((p["network"], p["token"].lower()), {})
        first = d.get("first_pool")
        if first and (p["age_min"] is None or age_minutes(first) > p["age_min"]):
            p["created"] = first
        p["age_min"] = age_minutes(p["created"])
        p["websites"] = d.get("websites", [])
        p["socials"] = d.get("socials", [])
        p["image"] = d.get("image")
        p["boosts"] = d.get("boosts", 0)
        p["has_profile"] = d.get("has_profile", False)
        p["pair_count"] = d.get("pairs", 1)
        p["ds_url"] = d.get("ds_url") or (
            f"https://dexscreener.com/{CHAINS.get(p['network'], (p['network'],))[0]}/{p['pool']}")


def security_evm(pools, chain_id):
    out = {}
    addrs = list({p["token"].lower() for p in pools})
    for i in range(0, len(addrs), 20):
        chunk = addrs[i:i + 20]
        data = get_json(f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
                        f"?contract_addresses={','.join(chunk)}")
        for addr, r in ((data or {}).get("result") or {}).items():
            flags, penalty, fatal = [], 0, False

            def yes(k):
                return str(r.get(k, "0")) == "1"

            if yes("is_honeypot"):
                fatal = True; flags.append(("danger", "Honeypot"))
            if yes("cannot_sell_all"):
                penalty += 15; flags.append(("danger", "Cannot sell all"))
            if yes("owner_change_balance"):
                penalty += 15; flags.append(("danger", "Owner can change balances"))
            if str(r.get("is_open_source", "1")) == "0":
                penalty += 15; flags.append(("danger", "Source not verified"))
            if yes("hidden_owner"):
                penalty += 8; flags.append(("warn", "Hidden owner"))
            if yes("is_mintable"):
                penalty += 8; flags.append(("warn", "Mintable"))
            if yes("transfer_pausable"):
                penalty += 6; flags.append(("warn", "Transfers pausable"))
            if yes("is_proxy"):
                penalty += 5; flags.append(("warn", "Upgradeable proxy"))
            if yes("is_blacklisted"):
                penalty += 5; flags.append(("warn", "Has blacklist"))
            buy_tax, sell_tax = fnum(r.get("buy_tax")), fnum(r.get("sell_tax"))
            if sell_tax > 0.05 or buy_tax > 0.05:
                penalty += 6; flags.append(("warn", f"Tax {buy_tax:.0%}/{sell_tax:.0%}"))
            top10 = sum(fnum(h.get("percent")) for h in (r.get("holders") or [])[:10]
                        if str(h.get("is_contract", "0")) != "1" and not h.get("is_locked"))
            if top10 > 0.5:
                penalty += 10; flags.append(("danger", f"Top holders own {top10:.0%}"))
            elif top10 > 0.3:
                penalty += 4; flags.append(("warn", f"Top holders own {top10:.0%}"))
            lp_locked = sum(fnum(h.get("percent")) for h in (r.get("lp_holders") or [])
                            if h.get("is_locked") in (1, "1") or
                            str(h.get("address", "")).lower().startswith("0x000000000000000000000000000000000000dead"))
            if r.get("lp_holders") is not None:
                if lp_locked >= 0.9:
                    flags.append(("good", f"LP locked/burned {lp_locked:.0%}"))
                elif lp_locked < 0.5:
                    penalty += 8; flags.append(("warn", f"LP locked only {lp_locked:.0%}"))
            out[addr.lower()] = {
                "checked": True, "fatal": fatal, "penalty": penalty, "flags": flags,
                "sell_tax": sell_tax, "holders": int(fnum(r.get("holder_count"))),
            }
    return out


def security_solana(pools):
    out = {}
    for p in pools:
        mint = p["token"]
        r = get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
        if not r:
            continue
        flags, penalty, fatal = [], 0, False
        for risk in r.get("risks") or []:
            level = (risk.get("level") or "").lower()
            name = risk.get("name") or "Risk"
            if level == "danger":
                penalty += 12; flags.append(("danger", name))
            elif level == "warn":
                penalty += 4; flags.append(("warn", name))
        lp = r.get("lpLockedPct")
        if lp is not None:
            lp = fnum(lp)
            if lp >= 90:
                flags.append(("good", f"LP locked {lp:.0f}%"))
        if any("freeze" in f[1].lower() for f in flags if f[0] == "danger"):
            fatal = True   # freeze authority can stop you from selling
        out[mint.lower()] = {"checked": True, "fatal": fatal, "penalty": penalty,
                             "flags": flags, "sell_tax": 0.0, "holders": 0,
                             "rugcheck_score": r.get("score_normalised", r.get("score"))}
    return out


def run_security(pools):
    by_net = {}
    for p in pools:
        by_net.setdefault(p["network"], []).append(p)
    results = {}
    for net, group in by_net.items():
        gp = CHAINS.get(net, (None, None))[1]
        if gp == "solana":
            results.update(security_solana(group))
        elif gp:
            results.update(security_evm(group, gp))
    for p in pools:
        p["security"] = results.get(p["token"].lower(),
                                    {"checked": False, "fatal": False, "penalty": 0,
                                     "flags": [("warn", "Security not checked")],
                                     "sell_tax": 0.0, "holders": 0})


# ---------------------------------------------------------------- 4. scoring

def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def score_token(p):
    """Returns total 0-100 and the six component scores. The first five keep their
    original point scales and are scaled down to make room for Dev & insiders."""
    sec = p["security"]
    flags = list(sec["flags"])

    # Safety, max 25 (penalties are on the original 35-point scale)
    safety = 35 - sec["penalty"] if sec["checked"] else 14
    safety = clamp(safety, 0, 35) * 25 / 35

    # Liquidity, max 15: depth on a log scale ($10k -> 0, ~$500k -> 12) + ratio to FDV
    liq = p["liquidity"]
    depth = clamp((math.log10(max(liq, 1)) - 4) / (math.log10(500_000) - 4) * 12, 0, 12)
    fdv = p["fdv"] or p["mcap"]
    ratio = liq / fdv if fdv else 0
    ratio_pts = 3 if 0.08 <= ratio <= 0.6 else (1.5 if 0.04 <= ratio < 0.08 else 0)
    if fdv and ratio < 0.03:
        flags.append(("warn", f"Thin liquidity vs FDV ({ratio:.1%})"))
    liquidity = depth + ratio_pts

    # Activity, max 15: unique buyers, txns, buy/sell balance
    buyers = p["buyers_h1"] or p["buys_h1"]
    txns = p["buys_h1"] + p["sells_h1"]
    buyer_pts = clamp(math.log10(max(buyers, 1)) / math.log10(400) * 8, 0, 8)
    txn_pts = clamp(math.log10(max(txns, 1)) / math.log10(1500) * 6, 0, 6)
    bs = p["buys_h1"] / max(p["sells_h1"], 1)
    if 1.0 <= bs <= 3.0:
        bs_pts = 6
    elif 0.7 <= bs < 1.0 or 3.0 < bs <= 5.0:
        bs_pts = 3
    else:
        bs_pts = 0
        flags.append(("warn", "Heavy selling" if bs < 0.7 else "Buys look botted"))
    # many txns from few wallets = wash trading
    if txns > 80 and buyers and txns / max(buyers + p["sellers_h1"], 1) > 6:
        bs_pts = 0
        flags.append(("warn", "Few wallets, many txns"))
    activity = (buyer_pts + txn_pts + bs_pts) * 15 / 20

    # Momentum, max 5: volume turnover + healthy price move
    turnover = p["vol_h1"] / max(liq, 1)
    turn_pts = clamp(turnover / 0.5 * 5, 0, 5)
    chg = p["chg_h1"]
    if 0 <= chg <= 80:
        chg_pts = 5
    elif -15 <= chg < 0 or 80 < chg <= 200:
        chg_pts = 2.5
    else:
        chg_pts = 0
        if chg > 200:
            flags.append(("warn", f"Already pumped {chg:.0f}% in 1h"))
        else:
            flags.append(("warn", f"Dumping {chg:.0f}% in 1h"))
    momentum = (turn_pts + chg_pts) * 5 / 10

    # Project signals, max 15
    stypes = {s["type"] for s in p.get("socials", [])}
    project = 0
    if p.get("websites"):
        project += 7
    if "twitter" in stypes or "x" in stypes:
        project += 5
    if "telegram" in stypes:
        project += 4
    if "discord" in stypes:
        project += 2
    if p.get("has_profile"):
        project += 2
    if p.get("copies"):
        project -= 4
        flags.append(("warn", f"Copycat ({p['copies']} copies)"))
    project = clamp(project, 0, 20) * 15 / 20
    if not p.get("websites") and not stypes:
        flags.append(("warn", "No website or socials"))
    if p.get("boosts"):
        flags.append(("info", f"Paid DexScreener boosts ({p['boosts']})"))

    # Dev & insiders, max 25: on-chain checks of pump.fun tokens (onchain.py)
    oc = p.get("onchain") or {}
    caps = [5] if sec["fatal"] else []
    if oc.get("status") == "done":
        dev = clamp(15 + sum(f[3] for f in oc["flags"]), 0, 25)
        flags += [(f[1], f[2]) for f in oc["flags"]]
        ids = {f[0] for f in oc["flags"]}
        if {"dumped", "farm"} <= ids:
            caps.append(40)
        if "bundled" in ids:
            caps.append(55)
        if (oc.get("insider_pct_now") or 0) >= 20:
            caps.append(50)
    else:
        dev = 10   # EVM, not pump.fun, still checking or unavailable: neutral, like unchecked safety

    total = min([safety + liquidity + activity + momentum + project + dev] + caps)
    p["flags"] = flags
    p["parts"] = {
        "safety": round(safety, 1), "liquidity": round(liquidity, 1),
        "activity": round(activity, 1), "momentum": round(momentum, 1),
        "project": round(project, 1), "dev": round(dev, 1),
    }
    p["score"] = int(round(clamp(total, 0, 100)))
    return p["score"]


# ---------------------------------------------------------------- 5. state, alerts, output

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"tokens": {}, "alerted": []}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def telegram(cfg, text):
    tg = cfg["telegram"]
    url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=15).read()
    except Exception as e:
        log(f"telegram failed: {e}")


def fmt_usd(x):
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:.1f}k"
    return f"${x:.0f}"


def alert_text(p):
    bad = [f[1] for f in p["flags"] if f[0] in ("danger", "warn")][:3]
    return (f"<b>{p['symbol']}</b> on {p['network']} · score <b>{p['score']}</b>\n"
            f"Liq {fmt_usd(p['liquidity'])} · FDV {fmt_usd(p['fdv'] or p['mcap'])} · "
            f"Vol 1h {fmt_usd(p['vol_h1'])} · {p['chg_h1']:+.0f}% 1h\n"
            + (f"Flags: {', '.join(bad)}\n" if bad else "")
            + f"{p['ds_url']}")


def write_dashboard(cfg, state, meta):
    out_dir = os.path.join(HERE, cfg["output_dir"])
    os.makedirs(out_dir, exist_ok=True)
    tokens = sorted(state["tokens"].values(), key=lambda t: -t["score"])
    payload = {"meta": meta, "tokens": tokens}
    with open(os.path.join(out_dir, "tokens.json"), "w") as f:
        json.dump(payload, f, indent=1)
    with open(os.path.join(HERE, "dashboard_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(payload).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", blob)
    html = html.replace("__REFRESH__", str(max(60, int(cfg["scan_interval_minutes"] * 60))))
    path = os.path.join(out_dir, "index.html")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(path + ".tmp", path)
    return path


def public_view(p):
    keep = ["network", "pool", "token", "name", "symbol", "dex", "created", "age_min",
            "price_usd", "liquidity", "fdv", "mcap", "vol_h1", "vol_h24", "chg_m5", "chg_h1",
            "chg_h24", "buys_h1", "sells_h1", "buyers_h1", "sellers_h1", "websites",
            "socials", "image", "boosts", "score", "parts", "flags", "ds_url"]
    v = {k: p.get(k) for k in keep}
    v["checked"] = p["security"]["checked"]
    v["onchain"] = {k: x for k, x in (p.get("onchain") or {}).items() if k != "_"} or None
    v["holders"] = p["security"].get("holders", 0)
    v["explorer"] = CHAINS.get(p["network"], (None, None, "{}"))[2].format(p["token"])
    v["gt_url"] = f"https://www.geckoterminal.com/{p['network']}/pools/{p['pool']}"
    if p["network"] == "solana":
        v["rugcheck_url"] = f"https://rugcheck.xyz/tokens/{p['token']}"
    return v


# ---------------------------------------------------------------- main loop

LOCK = threading.Lock()  # state shared by the scan loop and the on-chain worker
DETAILS = ("created", "websites", "socials", "image", "boosts", "has_profile",
           "pair_count", "ds_url", "security")


def iso_ts(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0


def scan(cfg, state):
    started = time.time()
    f = cfg["filters"]
    now = datetime.now(timezone.utc).isoformat()
    # pools re-checked every scan: new ones for watch_minutes, listed tokens for as
    # long as they stay listed
    watch = state.setdefault("watch", {})
    checks = state.setdefault("checks", {})
    for v in state["tokens"].values():
        watch.setdefault(f"{v['network']}:{v['pool']}",
                         {"network": v["network"], "pool": v["pool"], "found": iso_ts(v["first_seen"])})

    seen = {}
    for net in cfg["networks"]:
        if net not in CHAINS:
            log(f"skip unknown network '{net}'")
            continue
        new = fetch_new_pools(net, cfg["pages_per_network"])
        trending = fetch_trending_pools(net)
        for p in new + trending:
            seen[f"{net}:{p['pool']}"] = p
        log(f"{net}: {len(new)} new pools, {len(trending)} trending")
    recheck = {}
    for k, w in watch.items():
        if k not in seen:
            recheck.setdefault(w["network"], []).append(w["pool"])
    for net, addrs in recheck.items():
        for p in fetch_pools(net, addrs):
            seen[f"{net}:{p['pool']}"] = p
    found = list(seen.values())
    log(f"{len(found)} pools checked, {sum(map(len, recheck.values()))} of them from the watch list")

    # one entry per token: keep the pool with the most liquidity
    best = {}
    for k, p in seen.items():
        ok, reasons = passes_filters(p, f)
        if k not in watch and "too old" not in reasons:
            watch[k] = {"network": p["network"], "pool": p["pool"], "found": started}
        if not ok:
            continue
        tk = (p["network"], p["token"].lower())
        if tk not in best or p["liquidity"] > best[tk]["liquidity"]:
            best[tk] = p
    passed = list(best.values())
    log(f"{len(passed)} of {len(found)} pools passed filters")

    # DexScreener and security results are reused for details_refresh_minutes
    fresh_after = started - cfg.get("details_refresh_minutes", 30) * 60
    stale = []
    for p in passed:
        c = checks.get(f"{p['network']}:{p['token'].lower()}")
        if c and c["at"] > fresh_after:
            p.update(c["details"])
            p["age_min"] = age_minutes(p["created"])
        else:
            stale.append(p)
    if stale:
        enrich_dexscreener(stale)
    young = [p for p in passed if not age_reasons(p, f)]
    if len(young) < len(passed):
        log(f"{len(passed) - len(young)} dropped on token age (first pool on any DEX)")
    passed = young
    unchecked = [p for p in passed if "security" not in p]
    if unchecked:
        run_security(unchecked)
        for p in unchecked:
            checks[f"{p['network']}:{p['token'].lower()}"] = {
                "at": started, "details": {k: p[k] for k in DETAILS}}

    # copycats: same symbol or name as another token listed in the last 24 h
    names = state.setdefault("names", {})
    for p in passed:
        names[f"{p['network']}:{p['token'].lower()}"] = [p["symbol"].lower(), p["name"].lower(), started]
    for k in [k for k, x in names.items() if x[2] < started - 86400]:
        del names[k]
    for p in passed:
        k = f"{p['network']}:{p['token'].lower()}"
        sym, name = names[k][:2]
        p["copies"] = sum(1 for o, x in names.items() if o != k and (x[0] == sym or (name and x[1] == name)))

    alerts = 0
    listed = set()
    with LOCK:  # the on-chain worker thread reads tokens and writes onchain/creators
        for p in passed:
            sec = p["security"]
            if sec["fatal"] and f.get("drop_honeypots", True):
                continue
            if sec.get("sell_tax", 0) > f["max_sell_tax"]:
                continue
            key = f"{p['network']}:{p['token'].lower()}"
            p["onchain"] = state["onchain"].get(key)
            score_token(p)
            listed.add(key)
            prev = state["tokens"].get(key)
            v = public_view(p)
            v["first_seen"] = prev["first_seen"] if prev else now
            v["first_score"] = prev["first_score"] if prev else p["score"]
            v["last_seen"] = now
            hist = (prev or {}).get("history", [])[-23:]
            v["history"] = hist + [p["score"]]
            state["tokens"][key] = v
            tg = cfg["telegram"]
            if (tg.get("enabled") and p["score"] >= tg.get("min_score", 70)
                    and key not in state["alerted"]):
                telegram(cfg, alert_text(p))
                state["alerted"].append(key)
                alerts += 1

        # a token stays listed only while it meets the filters; if its pool could not be
        # fetched this scan it is kept as it was, for up to 3 scan intervals
        missing_cutoff = started - 3 * cfg["scan_interval_minutes"] * 60
        dropped = 0
        for key, v in list(state["tokens"].items()):
            if key in listed:
                continue
            if f"{v['network']}:{v['pool']}" in seen or iso_ts(v["last_seen"]) < missing_cutoff:
                del state["tokens"][key]
                dropped += 1
        if dropped:
            log(f"{dropped} tokens no longer meet the filters and were removed")
        for cache in (checks, state["onchain"]):
            for key in [key for key in cache if key not in state["tokens"]]:
                del cache[key]
        for dev in [d for d, c in state["creators"].items() if started - c["at"] > onchain.CREATOR_CACHE]:
            del state["creators"][dev]

    # stop watching pools that are past the age range or never qualified in watch_minutes
    keep = {f"{v['network']}:{v['pool']}" for v in state["tokens"].values()}
    give_up = started - cfg.get("watch_minutes", 60) * 60
    for k, w in list(watch.items()):
        p = seen.get(k)
        too_old = p is not None and p["age_min"] is not None and p["age_min"] > f["max_age_hours"] * 60
        if k not in keep and (too_old or w["found"] < give_up):
            del watch[k]
    state["alerted"] = state["alerted"][-2000:]

    meta = {
        "scanned_at": now,
        "pools_found": len(found),
        "watching": len(watch),
        "passed": len(passed),
        "tracked": len(state["tokens"]),
        "alerts": alerts,
        "duration_s": round(time.time() - started, 1),
        "interval_min": cfg["scan_interval_minutes"],
        "networks": cfg["networks"],
        "filters": f,
        "demo": False,
    }
    return meta


def onchain_worker(state):
    """Background thread: on-chain dev & insider checks of listed Solana tokens, one at a
    time. New tokens first, then re-checks when due (every 15 min under 6 h old, else hourly)."""
    while True:
        with LOCK:
            now = time.time()
            due = sorted(((state["onchain"].get(k) or {}).get("next_at", 0), k, t["token"])
                         for k, t in state["tokens"].items() if t["network"] == "solana")
            due = [d for d in due if d[0] <= now]
            if due:
                key, mint = due[0][1:]
                prev = state["onchain"].get(key)
                creators = dict(state["creators"])
        if not due:
            time.sleep(20)
            continue
        try:
            result, profile = onchain.check(mint, prev, creators)
            log(f"on-chain {mint}: {', '.join(f[2] for f in result.get('flags', [])) or result['status']} "
                f"({time.time() - now:.0f}s)")
        except Exception as e:  # source down, rate limited or odd data: keep the last result, retry later
            log(f"on-chain {mint}: unavailable ({e})")
            result, profile = dict(prev or {"status": "unavailable"}, error=str(e), next_at=time.time() + 900), None
        with LOCK:
            if key in state["tokens"]:
                state["onchain"][key] = result
            if profile:
                state["creators"][result["creator"]] = profile


def main():
    ap = argparse.ArgumentParser(description="Token Scout scanner")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--once", action="store_true", help="run a single scan")
    ap.add_argument("--demo", action="store_true", help="build dashboard from example data")
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = json.load(fh)
    state_path = os.path.join(HERE, "state.json")

    if args.demo:
        from demo_data import build_demo_state
        state, meta = build_demo_state(cfg, score_token, public_view)
        path = write_dashboard(cfg, state, meta)
        log(f"demo dashboard written to {path}")
        return

    state = load_state(state_path)
    state.setdefault("onchain", {})
    state.setdefault("creators", {})
    if not args.once:
        threading.Thread(target=onchain_worker, args=(state,), daemon=True).start()
    while True:
        try:
            meta = scan(cfg, state)
            with LOCK:
                save_state(state_path, state)
                path = write_dashboard(cfg, state, meta)
            log(f"dashboard updated: {path} ({meta['tracked']} tokens tracked)")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"scan error: {e!r}")
        if args.once:
            break
        time.sleep(cfg["scan_interval_minutes"] * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
