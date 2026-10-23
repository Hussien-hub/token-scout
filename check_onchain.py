#!/usr/bin/env python3
"""
Runs the on-chain dev & insider checks (onchain.py) on pump.fun tokens and prints
what they found and the full score. Exits with 1 if a reference token does not
get the expected result.

  python3 check_onchain.py            # the 5 reference tokens
  python3 check_onchain.py <mint>...  # any tokens, no expectations

Takes a few minutes on the public Solana RPC (SOLANA_RPC_URL in .env to use another).
"""

import sys
import time

import onchain
import scanner

# mint -> (name, dev-dumped wallet-farm launch?)
CASES = {
    "635BZjhhzBvn7EsErsL3ABMRmfsfEjGi8YJTCia9pump": ("VECTOR", True),
    "8idEA8QovsZJka6d9Z35fejE5MbL37BJxvw5k5r6pump": ("McRib", True),
    "6CaTKnA3eJEisxYrS4dKGmqBPJ8apePbc65xteJhpump": ("POOT", True),
    "Cbkg5w7V7W2J2sUmecnyN825CbvwvXwasfhduVtNpump": ("PHP", True),
    "EJAxERvKU4kEVXn3NarjvRtgaMaJyP9ZuMeKA7JJpump": ("CHOO", False),
}


def market(mint):
    """The token's most liquid pool, as the scanner sees it."""
    data = scanner.get_json(f"{scanner.GT}/solana/tokens/{mint}/pools?include=base_token")
    pools = [p for p in scanner.parse_pools(data, "solana") if p["token"] == mint] if data else []
    return max(pools, key=lambda p: p["liquidity"]) if pools else None


def num(x, fmt):
    return "?" if x is None else format(x, fmt)


def main():
    assert onchain.b58e(onchain.b58d(onchain.PUMP)) == onchain.PUMP
    failed = []
    for mint in sys.argv[1:] or list(CASES):
        name, bad = CASES.get(mint, (mint[:8], None))
        t0 = time.time()
        o, _ = onchain.check(mint, None, {})
        p = market(mint)
        if p:
            scanner.run_security([p])
            p["onchain"] = o
            scanner.score_token(p)
        ids = {f[0] for f in o.get("flags", [])}
        print(f"\n{name}  score {p['score'] if p else '?'} (dev & insiders {p['parts']['dev'] if p else '?'}/25)  "
              f"status {o['status']}, checked in {time.time() - t0:.0f}s")
        if o["status"] == "done":
            print(f"  creator   {o['creator']}")
            print(f"  dev       bought {o['dev_initial_pct']:.1f}% at launch, sold {num(o['dev_sold_pct'], '.0f')}% "
                  f"({num(o['dev_sold_10m_pct'], '.0f')}% within 10 min), first sell "
                  f"{num(o['dev_first_sell_sec'], 'd')}s after create, holds {o['dev_current_pct']:.2f}% now")
            print(f"  wallets   funded ≈ {o['funded']:,}{'+' if o['funded_capped'] else ''}, creator funded by "
                  f"{o['funder'] or 'unknown'} ({o['funder_txs'] or '?'} txs)")
            print(f"  launch    curve filled in {num(o['curve_sec'], '.0f')}s, insiders now {num(o['insider_pct_now'], '.1f')}% "
                  f"/ peak {num(o['insider_pct_peak'], '.1f')}%, round allocations {o['round_allocations']}, "
                  f"launch tool {o['launch_tool']}")
            for f in o["flags"]:
                print(f"  {f[3]:+4d}  {f[1]:6} {f[2]}")
        if bad is True and not ({"dumped", "farm"} <= ids and p and p["score"] <= 40):
            failed.append(f"{name}: expected 'Dev dumped at launch' + 'Serial launcher' and score <= 40")
        if bad is False and (ids & {"farm", "likely_farm"} or "holding" not in ids):
            failed.append(f"{name}: expected no farm flag and 'Dev still holding'")
    print("\n" + ("FAILED\n  " + "\n  ".join(failed) if failed else "All expectations met."))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
