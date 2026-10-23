"""Example tokens for `scanner.py --demo`. Invented names, not real tokens."""

import random
from datetime import datetime, timedelta, timezone

from onchain import flags_for

EXAMPLES = [
    # name, symbol, network, dex, age_min, liq, fdv, vol_h1, chg_h1, buys, sells, buyers, sellers, web, socials, sec
    ("Lumen Relay", "LUMR", "base", "aerodrome", 210, 182000, 1450000, 96000, 34, 640, 410, 380, 250, True, ["twitter", "telegram", "discord"], "clean_lp"),
    ("Quarry Labs", "QRY", "solana", "raydium", 95, 124000, 880000, 71000, 22, 520, 330, 300, 190, True, ["twitter", "telegram"], "sol_clean"),
    ("Tidewater", "TIDE", "eth", "uniswap_v2", 640, 410000, 3900000, 88000, 9, 210, 160, 150, 110, True, ["twitter", "telegram"], "clean_lp"),
    ("Orbit Ledger", "ORBL", "bsc", "pancakeswap_v2", 160, 66000, 520000, 31000, 48, 300, 190, 210, 120, True, ["twitter"], "mintable"),
    ("Kiln", "KILN", "solana", "pumpswap", 48, 38000, 210000, 42000, 140, 900, 420, 520, 230, False, ["twitter"], "sol_warn"),
    ("Pebble AI", "PBAI", "base", "uniswap_v3", 300, 52000, 2400000, 12000, -8, 120, 140, 90, 100, True, ["twitter", "telegram"], "thin"),
    ("Ferris Wheel", "FERRIS", "solana", "raydium", 75, 29000, 160000, 54000, 310, 1400, 380, 260, 120, False, [], "sol_warn"),
    ("Vaultline", "VLT", "eth", "uniswap_v2", 1100, 95000, 690000, 9000, -22, 90, 170, 70, 130, True, ["twitter"], "unverified"),
    ("Moss Protocol", "MOSS", "bsc", "pancakeswap_v2", 40, 21000, 140000, 15000, 18, 260, 70, 40, 20, False, ["telegram"], "concentrated"),
    ("Driftnet", "DRFT", "base", "aerodrome", 520, 240000, 1900000, 61000, 12, 330, 280, 260, 220, True, ["twitter", "telegram", "discord"], "clean_lp"),
    ("Saltmarsh", "SALT", "solana", "meteora", 130, 74000, 450000, 22000, 6, 260, 210, 180, 150, True, ["twitter"], "sol_clean"),
    ("Honeycomb", "HCMB", "bsc", "pancakeswap_v2", 60, 33000, 260000, 19000, 55, 340, 12, 200, 10, False, ["telegram"], "honeypot"),
]

SEC = {
    "clean_lp": (0, [("good", "LP locked/burned 100%")]),
    "sol_clean": (4, [("warn", "Low amount of LP providers"), ("good", "LP locked 100%")]),
    "mintable": (8, [("warn", "Mintable")]),
    "sol_warn": (16, [("warn", "Top 10 holders high ownership"), ("danger", "Mutable metadata")]),
    "thin": (6, [("warn", "Upgradeable proxy")]),
    "unverified": (23, [("danger", "Source not verified"), ("warn", "Hidden owner")]),
    "concentrated": (18, [("danger", "Top holders own 62%"), ("warn", "LP locked only 20%")]),
    "honeypot": (0, [("danger", "Honeypot")]),
}

# on-chain dev & insider results for the Solana examples; SALT is not a pump.fun token, FERRIS is still being checked
ONCHAIN = {
    "QRY": dict(dev_initial_pct=3.1, dev_current_pct=2.9, dev_sold_pct=6.0, dev_sold_10m_pct=0.0, dev_first_sell_sec=4100,
                funded=12, funded_capped=False, curve_sec=1800, insider_pct_now=3.2, insider_pct_peak=4.0,
                round_allocations=0, launch_tool=None, holders=2400),
    "KILN": dict(dev_initial_pct=15.4, dev_current_pct=0.0, dev_sold_pct=100.0, dev_sold_10m_pct=100.0, dev_first_sell_sec=40,
                 funded=24800, funded_capped=True, curve_sec=35, insider_pct_now=1.1, insider_pct_peak=17.5,
                 round_allocations=4, launch_tool="j7tracker", holders=900),
}


def demo_onchain(sym, now, age):
    if sym == "SALT":
        return {"status": "n/a"}
    if sym not in ONCHAIN:
        return None
    ts = now.timestamp()
    o = dict(ONCHAIN[sym], status="done", creator="DemoCreator1111111111111111111111111111111",
             created=ts - age * 60, checked_at=ts - 240, funder=None, funder_txs=None, events=[], new={})
    o["flags"] = flags_for(o, None, ts)
    if sym == "KILN":
        exited = next(f for f in o["flags"] if f[0] == "insiders_exited")
        o["events"] = [[ts - 600, exited[2]]]
        o["new"] = {exited[0]: ts - 600}
    return o


SOCIAL_URLS = {"twitter": "https://x.com/", "telegram": "https://t.me/", "discord": "https://discord.gg/"}


def build_demo_state(cfg, score_token, public_view):
    rng = random.Random(7)
    now = datetime.now(timezone.utc)
    tokens = {}
    for (name, sym, net, dex, age, liq, fdv, vol, chg, b, s, bu, se, web, soc, sec) in EXAMPLES:
        if sec == "honeypot":
            continue  # the scanner drops honeypots before scoring
        addr = "Demo" + "".join(rng.choice("abcdef0123456789") for _ in range(36))
        penalty, flags = SEC[sec]
        p = {
            "network": net, "pool": "DemoPool" + addr[4:20], "token": addr, "name": name,
            "symbol": sym, "dex": dex, "created": (now - timedelta(minutes=age)).isoformat(),
            "age_min": age, "price_usd": fdv / 1e9, "liquidity": liq, "fdv": fdv, "mcap": 0,
            "vol_h1": vol, "vol_h24": vol * rng.uniform(4, 12), "chg_m5": chg / 6,
            "chg_h1": chg, "chg_h24": chg * 1.8, "buys_h1": b, "sells_h1": s,
            "buyers_h1": bu, "sellers_h1": se,
            "websites": ["https://example.com"] if web else [],
            "socials": [{"type": t, "url": SOCIAL_URLS[t] + "example"} for t in soc],
            "image": None, "boosts": 1 if sym == "KILN" else 0, "has_profile": web,
            "ds_url": "https://dexscreener.com/",
            "security": {"checked": True, "fatal": False, "penalty": penalty,
                         "flags": list(flags), "sell_tax": 0.0,
                         "holders": rng.randint(150, 2400)},
        }
        if net == "solana":
            p["onchain"] = demo_onchain(sym, now, age)
        score_token(p)
        v = public_view(p)
        v["ds_url"] = "https://dexscreener.com/"
        v["gt_url"] = "https://www.geckoterminal.com/"
        v["explorer"] = "https://dexscreener.com/"
        v.pop("rugcheck_url", None)
        hist = [max(0, min(100, p["score"] + rng.randint(-12, 6))) for _ in range(8)] + [p["score"]]
        v["history"] = hist
        v["first_score"] = hist[0]
        v["first_seen"] = (now - timedelta(minutes=min(age, 180))).isoformat()
        v["last_seen"] = now.isoformat()
        tokens[f"{net}:{addr}"] = v
    meta = {
        "scanned_at": now.isoformat(), "pools_found": 160, "passed": len(tokens) + 1,
        "tracked": len(tokens), "alerts": 0, "duration_s": 0,
        "interval_min": cfg["scan_interval_minutes"], "networks": cfg["networks"],
        "filters": cfg["filters"], "demo": True,
    }
    return {"tokens": tokens, "alerted": []}, meta
