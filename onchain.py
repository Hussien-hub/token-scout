"""
On-chain dev & insider checks for Solana pump.fun tokens.

Each check takes tens to hundreds of RPC calls, so scanner.py runs check() in a
background thread, never inside the scan loop. All requests go through `http`
(scanner.get_json), which shares the rate limits and retries.

Sources: Solana JSON-RPC (SOLANA_RPC_URL in .env, the public endpoint by
default), RugCheck full report and insider graph, DexScreener pairs.
"""

import hashlib
import json
import os
import time

http = None  # set by scanner.py: get_json(url, body=None)

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
# programs a plain token transfer uses; anything else in the transaction means a swap
PLAIN = {SYSTEM, TOKEN, TOKEN22, ATA, "ComputeBudget111111111111111111111111111111",
         "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"}
# pump.fun create instructions -> position of the creator (the signing "user") in their accounts
CREATE = {hashlib.sha256(b"global:create").digest()[:8]: 7,
          hashlib.sha256(b"global:create_v2").digest()[:8]: 5}

RECHECK_YOUNG, RECHECK_OLD = 15 * 60, 60 * 60   # tokens under 6 h old / older
CREATOR_CACHE = 6 * 3600
DEV_TX_LIMIT = 1000    # dev token-account transactions read per check (PHP's dev made 313 in its first 8 s)
FUNDING_SAMPLE = 30    # newest creator transactions read to measure how many wallets it funds


class Unavailable(Exception):
    """A data source is down or rate limited; the check is retried later."""


# ---------------------------------------------------------------- addresses

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
P = 2 ** 255 - 19
D = -121665 * pow(121666, P - 2, P) % P


def b58d(s):
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    return b"\0" * (len(s) - len(s.lstrip("1"))) + n.to_bytes((n.bit_length() + 7) // 8, "big")


def b58e(b):
    n, s = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def on_curve(b):
    """True if 32 bytes decode to an ed25519 point; program addresses must not."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    return x2 == 0 or pow(x2, (P - 1) // 2, P) == 1


def pda(seeds, program):
    """Solana find_program_address."""
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + b58d(program) + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58e(h)


def ata(owner, mint, program):
    return pda([b58d(owner), b58d(program), b58d(mint)], ATA)


# ---------------------------------------------------------------- RPC

def rpc(method, *params):
    url = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
    r = http(url, body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode())
    if r is None:
        raise Unavailable(f"Solana RPC failed on {method}")
    if "error" in r:
        if "could not find account" in str(r["error"].get("message")):
            return None
        raise Unavailable(f"Solana RPC {method}: {r['error'].get('message')}")
    return r.get("result")


def get_tx(sig):
    return rpc("getTransaction", sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1})


def instructions(tx):
    """Top-level and inner instructions of a jsonParsed transaction."""
    ixs = list(tx["transaction"]["message"]["instructions"])
    for inner in tx["meta"].get("innerInstructions") or []:
        ixs += inner["instructions"]
    return ixs


def sig_pages(address, pages, until=None):
    """Signature pages for an address, newest first, 1000 per page."""
    before = None
    for _ in range(pages):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        page = rpc("getSignaturesForAddress", address, opts) or []
        yield page
        if len(page) < 1000:
            return
        before = page[-1]["signature"]


def balance(tx, key, owner, mint):
    return sum(int(b["uiTokenAmount"]["amount"]) for b in tx["meta"].get(key) or []
               if b.get("owner") == owner and b.get("mint") == mint)


# ---------------------------------------------------------------- the checks

def find_create(mint):
    """(creator, created) from the pump.fun create instruction, or None for a mint
    without a pump.fun bonding curve. The creator is the signer of create:
    RugCheck's `creator` is the curve's fee recipient, which launch tools point elsewhere."""
    curve = pda([b"bonding-curve", b58d(mint)], PUMP)
    if not (rpc("getAccountInfo", curve, {"encoding": "base64"}) or {}).get("value"):
        return None
    # the curve's history stops growing at migration, so its oldest transaction is cheap to reach
    pages = list(sig_pages(curve, 30))
    if len(pages[-1]) == 1000:
        raise Unavailable("bonding curve history longer than 30k transactions")
    oldest = [s for page in pages for s in page if s["err"] is None][-3:]
    for s in reversed(oldest):
        tx = get_tx(s["signature"])
        for ix in instructions(tx) if tx else []:
            user = CREATE.get(b58d(ix["data"])[:8]) if ix.get("programId") == PUMP and "data" in ix else None
            if user is not None:
                return ix["accounts"][user], tx["blockTime"]
    raise Unavailable("pump.fun create transaction not found")


def dev_trades(mint, dev, program, created, st):
    """Appends the dev's new token moves [seconds after create, raw amount] to
    st["moves"] and returns the dev's current balance."""
    # shortcut: reads only the dev's associated token account; tokens moved through other accounts are missed
    acct = ata(dev, mint, program)
    new = [s for page in sig_pages(acct, 30, st.get("last_sig")) for s in page]
    todo = [s for s in reversed(new) if s["err"] is None][:DEV_TX_LIMIT]
    for s in todo:
        tx = get_tx(s["signature"])
        delta = balance(tx, "postTokenBalances", dev, mint) - balance(tx, "preTokenBalances", dev, mint) if tx else 0
        if delta:
            st.setdefault("moves", []).append([tx["blockTime"] - created, delta])
    if new:
        # when capped, continue after the last transaction read
        st["last_sig"] = todo[-1]["signature"] if len(todo) == DEV_TX_LIMIT else new[0]["signature"]
    bal = rpc("getTokenAccountBalance", acct)
    return int(bal["value"]["amount"]) if bal else 0


def creator_profile(dev):
    """How many wallets the creator funded and who funded it. Wallets funded = SOL
    recipients in its newest transactions, scaled to all of its transactions
    (exact for small wallets). Farm wallets make thousands of transactions, so
    reading every one would take hundreds of calls."""
    ok, recent, oldest, complete = 0, [], None, False
    for page in sig_pages(dev, 40):
        good = [s for s in page if s["err"] is None]
        ok += len(good)
        recent = recent or good[:FUNDING_SAMPLE]
        oldest = good[-1] if good else oldest
        complete = len(page) < 1000
    recipients = set()
    for s in recent:
        tx = get_tx(s["signature"])
        for ix in tx["transaction"]["message"]["instructions"] if tx else []:
            p = ix.get("parsed")
            if (ix.get("programId") == SYSTEM and isinstance(p, dict)
                    and p.get("type") in ("transfer", "transferWithSeed") and p["info"].get("source") == dev):
                recipients.add(p["info"]["destination"])
    funded = len(recipients) if ok <= len(recent) else round(len(recipients) / max(len(recent), 1) * ok)
    funder = None
    if complete and oldest:
        tx = get_tx(oldest["signature"])
        for ix in instructions(tx) if tx else []:
            p = ix.get("parsed")
            if isinstance(p, dict) and p.get("type") == "transfer" and p["info"].get("destination") == dev:
                funder = p["info"]["source"]
                break
    return {"at": time.time(), "funded": funded, "funded_capped": not complete, "funder": funder,
            "funder_txs": len(rpc("getSignaturesForAddress", funder, {"limit": 1000}) or []) if funder else None}


def curve_seconds(mint, created):
    """Seconds from create to the first PumpSwap pool (curve completed); None while on the curve.
    The curve account's own last transaction is no use: unrelated transactions touch it for hours.
    The migration pool is not always against SOL (CHOO's is against WBTC)."""
    pairs = http(f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}")
    times = [x["pairCreatedAt"] / 1000 for x in (pairs if isinstance(pairs, list) else [])
             if x.get("dexId") == "pumpswap" and x.get("pairCreatedAt")
             and (x.get("baseToken") or {}).get("address") == mint]
    return max(0, min(times) - created) if times else None


def held_by(wallets, mint, program):
    """Current raw balance of the wallets' associated token accounts, 100 per call."""
    accts = [ata(w, mint, program) for w in wallets]
    total = 0
    for i in range(0, len(accts), 100):
        for a in (rpc("getMultipleAccounts", accts[i:i + 100], {"encoding": "jsonParsed"}) or {}).get("value") or []:
            if a and isinstance(a["data"], dict):
                total += int(a["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    return total


def received_by_transfer(token_account, owner, mint):
    """True if tokens first arrived in the account by a plain transfer, not a swap.
    Wallets often create the account in one transaction and buy in the next, so the
    first transaction that raised the balance is the one that counts."""
    sigs = [s for page in sig_pages(token_account, 3) for s in page if s["err"] is None]
    for s in list(reversed(sigs))[:5]:
        tx = get_tx(s["signature"])
        if tx and balance(tx, "postTokenBalances", owner, mint) > balance(tx, "preTokenBalances", owner, mint):
            return {ix.get("programId") for ix in instructions(tx)} <= PLAIN
    return False


def flags_for(o, prev_holders, now):
    """[id, level, text, points] for the Dev & insiders score (base 15, 0-25)."""
    f = []
    sold, sold10, funded = o["dev_sold_pct"], o["dev_sold_10m_pct"], o["funded"]
    if sold10 is not None and sold10 >= 80:
        f.append(["dumped", "danger", "Dev dumped at launch", -15])
    elif sold is not None and sold >= 80:
        f.append(["sold_most", "warn", "Dev sold most", -8])
    age_h = (now - o["created"]) / 3600
    if age_h >= 6 and o["dev_initial_pct"] and o["dev_current_pct"] >= o["dev_initial_pct"] / 2:
        kept = min(o["dev_current_pct"] / o["dev_initial_pct"], 1)
        f.append(["holding", "good", f"Dev still holding {kept:.0%}", 4])
    if funded >= 500:
        f.append(["farm", "danger", f"Serial launcher / wallet farm ({funded:,} funded)", -12])
    elif funded >= 100:
        f.append(["likely_farm", "warn", f"Likely farm ({funded} funded)", -6])
    elif funded < 50:
        f.append(["few_funded", "good", "Dev funded few wallets", 3])
    c = o["curve_sec"]
    if c is not None and c < 120:
        # a fast curve alone is not damning (CHOO filled in 0 s and is fine)
        if (sold is not None and sold >= 80) or funded >= 100:
            f.append(["bundled", "danger", f"Bundled launch (curve filled in {c:.0f}s)", -8])
        else:
            f.append(["fast_curve", "warn", f"Curve filled in {c:.0f}s", -3])
    now_pct, peak = o["insider_pct_now"], o["insider_pct_peak"]
    if now_pct is not None:
        if now_pct >= 10:
            f.append(["insiders", "danger", f"Insiders holding {now_pct:.0f}%", -8])
        elif peak >= 10 and now_pct < 3:
            f.append(["insiders_exited", "info", f"Insiders already exited (peak {peak:.0f}%)", 0])
        elif now_pct < 5:
            f.append(["insiders_low", "good", "Insiders hold <5%", 3])
    if o["round_allocations"] >= 3:
        f.append(["round", "warn", f"Round-number allocations ({o['round_allocations']})", -5])
    if o["launch_tool"]:
        f.append(["launch_tool", "info", f"Launch tool: {o['launch_tool']}", -2 if funded >= 100 else 0])
    if age_h > 24 and o["holders"] and prev_holders and o["holders"] > prev_holders:
        f.append(["holders_growing", "good", "Holders still growing", 2])
    return f


def check(mint, prev, creators):
    """One on-chain check of a Solana token. prev is the last result (reused for the
    create transaction and the dev's transactions already read); creators holds
    cached creator profiles. Returns (result, new creator profile or None)."""
    prev, now = prev or {}, time.time()
    st = json.loads(json.dumps(prev.get("_") or {}))
    if prev.get("creator"):
        dev, created = prev["creator"], prev["created"]
    else:
        found = find_create(mint)
        if not found:
            return {"status": "n/a", "checked_at": now, "next_at": now + 24 * 3600}, None
        dev, created = found
    mint_acct = (rpc("getAccountInfo", mint, {"encoding": "jsonParsed"}) or {}).get("value")
    if not mint_acct:
        raise Unavailable("mint account not found")
    program = mint_acct["owner"]
    supply = int(mint_acct["data"]["parsed"]["info"]["supply"])
    pct = lambda raw: raw / supply * 100

    dev_now = dev_trades(mint, dev, program, created, st)
    moves = st.get("moves", [])
    bought = sum(x for _, x in moves if x > 0)
    sold = lambda within: min(100.0, -sum(x for t, x in moves if x < 0 and t <= within) / bought * 100) if bought else None

    profile = creators.get(dev)
    fresh = None
    if not profile or now - profile["at"] > CREATOR_CACHE:
        profile = fresh = creator_profile(dev)

    report = http(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report") or {}
    skip = {a for a, k in (report.get("knownAccounts") or {}).items() if k.get("type") in ("AMM", "LOCKER")}
    insider_now = insider_peak = None
    graph = http(f"https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph") if report else None
    if isinstance(graph, list):
        # RugCheck's currentHolding goes stale, so the cluster wallets' balances are read on-chain
        wallets = sorted({n["id"] for net in graph for n in net.get("nodes") or []
                          if n.get("participant") and n.get("id") not in skip})
        insider_now = pct(held_by(wallets, mint, program))
        insider_peak = max(insider_now, pct(sum(int(n.get("tokenAmount") or 0)
                                                for n in report.get("insiderNetworks") or [])))
    # shortcut: RugCheck lists the top 20 holders, not 50; upgrade with a paid holders API
    rounds = st.setdefault("round", {})
    for h in report.get("topHolders") or []:
        amt = h.get("uiAmount") or 0
        if h.get("owner") not in skip and amt >= 1_000_000 and amt % 1_000_000 == 0 and h["address"] not in rounds:
            rounds[h["address"]] = received_by_transfer(h["address"], h["owner"], mint)
    uri = (report.get("tokenMeta") or {}).get("uri") or ""
    image = (report.get("fileMeta") or {}).get("image") or ""

    o = {
        "status": "done", "checked_at": now, "creator": dev, "created": created,
        "dev_initial_pct": pct(sum(x for t, x in moves if x > 0 and t <= 60)),
        "dev_bought_pct": pct(bought),
        "dev_sold_pct": sold(float("inf")),
        "dev_sold_10m_pct": sold(600),
        "dev_first_sell_sec": next((t for t, x in moves if x < 0), None),
        "dev_current_pct": pct(dev_now),
        "funded": profile["funded"], "funded_capped": profile["funded_capped"],
        "funder": profile["funder"], "funder_txs": profile["funder_txs"],
        "curve_sec": prev["curve_sec"] if prev.get("curve_sec") is not None else curve_seconds(mint, created),
        "insider_pct_now": insider_now, "insider_pct_peak": insider_peak,
        "holders": report.get("totalHolders"),
        "round_allocations": sum(1 for h in report.get("topHolders") or [] if rounds.get(h["address"])),
        "launch_tool": "j7tracker" if "j7tracker.io" in uri else "axiom" if "axiom-cdn" in image else None,
        "_": st,
    }
    o["flags"] = flags_for(o, prev.get("holders"), now)

    # history: flags that appear or clear after the first check, with their time
    ids = {f[0] for f in o["flags"]}
    o["new"] = {k: t for k, t in (prev.get("new") or {}).items() if k in ids and now - t < 1800}
    events = list(prev.get("events") or [])
    if prev.get("status") == "done":
        old = {f[0]: f[2] for f in prev["flags"]}
        for f in o["flags"]:
            if f[0] not in old:
                events.append([now, f[2]])
                o["new"][f[0]] = now
        events += [[now, f"Cleared: {text}"] for k, text in old.items() if k not in ids]
    o["events"] = events[-20:]
    o["next_at"] = now + (RECHECK_YOUNG if now - created < 6 * 3600 else RECHECK_OLD)
    return o, fresh
