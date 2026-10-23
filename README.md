# Token Scout

Scans new DEX pools, filters them, runs a contract security check and scores each token 0–100.
Writes a dashboard to `output/index.html` after every scan and can alert you on Telegram.

No API keys, no pip installs: Python 3.9+ standard library only.

## Quick start

```bash
cp config.example.json config.json    # first time only; config.json is not in git (it holds your Telegram token)
python3 scanner.py --demo    # dashboard with example data, to see the layout
python3 scanner.py --once    # one live scan
python3 scanner.py           # keep scanning every N minutes (config.json)
```

Open `output/index.html` in your browser. On a server, serve the folder:

```bash
cd output && python3 -m http.server 8090
# then open http://<server-ip>:8090  (or tunnel: ssh -L 8090:localhost:8090 you@server)
```

The page reloads itself after each scan and keeps your filters.

## How a token gets into the list

1. **Discovery** – GeckoTerminal `new_pools` and the 1h `trending_pools` for every chain in `networks`
   (solana, base, bsc, eth, arbitrum, polygon_pos).
   New pools leave those pages fast (on Solana 2 pages cover only the last ~5 minutes), so every
   pool found goes on a **watch list** and is re-checked each scan for `watch_minutes`.
   That is how a pool found at 4 minutes old gets listed once it passes `min_age_minutes`.
2. **Hard filters** (`filters` in config.json) – age, liquidity, FDV range, 1h volume, 1h txns.
   One entry per token (the pool with the most liquidity).
3. **Enrichment** – DexScreener adds website, X, Telegram, Discord and paid boosts.
   It also gives the token's real age: all of the token's pools are looked up and the *oldest*
   one counts. A new pool can belong to an old token, so the age filters are checked again on this age.
   DexScreener lists at most 30 pools per token; when it hits that limit or doesn't know the token yet,
   GeckoTerminal's pool list for the token is checked too.
4. **Security** – GoPlus for EVM chains, RugCheck for Solana.
   Honeypots, freeze authority and sell tax above `max_sell_tax` are dropped.
5. **Score** (table below). Solana pump.fun tokens also get **Dev & insiders** from on-chain
   checks that run in the background (next section).
6. **Stay or go** – every listed token is re-checked on each scan and stays only while it still
   meets all of the above. It is removed once it fails them (liquidity pulled, activity gone,
   older than `max_age_hours`, turned into a honeypot). DexScreener and security results are
   reused for `details_refresh_minutes`; liquidity, volume, txns and price refresh every scan.

| Part | Max | What counts |
|---|---|---|
| Safety | 25 | Starts full, minus penalties: unverified source, owner can change balances, mintable, proxy, hidden owner, taxes, top-holder concentration, unlocked LP, RugCheck risks. Unchecked tokens get 10. |
| Liquidity | 15 | Depth on a log scale ($10k → 0, $500k → 12) plus a healthy liquidity/FDV ratio (8–60%). |
| Activity | 15 | Unique buyers and txns in the last hour, buy/sell ratio between 1 and 3. Many txns from few wallets scores 0 here. |
| Momentum | 5 | Volume turnover vs liquidity, and a 1h price move of 0–80%. Tokens already up 200%+ get nothing. |
| Project | 15 | Website, X, Telegram, Discord, DexScreener profile. Copycats (same symbol or name as another token listed in the last 24 h) lose points. |
| Dev & insiders | 25 | Solana pump.fun tokens, see below. Starts at 15. EVM tokens, other Solana tokens and tokens still being checked get a neutral 10. |

Each scan rescores tracked tokens, so the dashboard shows the change since first seen and a score trend.

## Dev & insiders (Solana pump.fun tokens)

Many launches score well on market data and RugCheck but are wallet farms where the dev dumped in
the first minute. `onchain.py` checks each listed Solana token on-chain, in a background thread so
the scan stays fast: right after it is listed, then every 15 min while under 6 h old, then hourly.
The dashboard shows `checking…` until the first result, which appears at the next scan.

| Check | How | Flag | Points |
|---|---|---|---|
| Real creator | Signer of the pump.fun create instruction (RugCheck's `creator` is the fee recipient, which launch tools point elsewhere) | | |
| Dev sold | The creator's token account history | `Dev dumped at launch` ≥ 80% sold within 10 min (HIGH) / `Dev sold most` ≥ 80% (MEDIUM) | −15 / −8 |
| Dev holding | Current balance vs launch buy, after 6 h | `Dev still holding` ≥ 50% | +4 |
| Wallet farm | Wallets the creator sent SOL to: a sample of its newest transactions scaled to all of them (cached 6 h per creator) | `Serial launcher / wallet farm` ≥ 500 (HIGH) / `Likely farm` 100–499 / < 50 | −12 / −6 / +3 |
| Bundled launch | Create → first PumpSwap pool under 120 s **and** the dev sold most or is a farm; otherwise `Curve filled in Ns` | HIGH / MEDIUM | −8 / −3 |
| Insiders | RugCheck insider wallets, their **current** balances read on-chain (RugCheck's are often stale) | `Insiders holding X%` ≥ 10% (HIGH) / `Insiders already exited` peak ≥ 10%, now < 3% / < 5% | −8 / 0 / +3 |
| Round allocations | Top-20 holders with exact-million balances that arrived by plain transfer | ≥ 3 (MEDIUM) | −5 |
| Launch tool | Metadata on `metadata.j7tracker.io` or image on `axiom-cdn` | INFO | −2 with farm signals |
| Holders | Older than 24 h and holders still growing | | +2 |

Caps on the total score: dev dumped at launch + wallet farm → 40, insiders holding ≥ 20% → 50,
bundled launch → 55. The detail panel lists every point gained or lost, the creator, who funded it,
and when flags appeared or cleared ("NEW" for 30 min).

If the RPC or RugCheck is down the token shows `unavailable` (or keeps its last result) and is
retried in 15 min; the scan and dashboard never wait for it.

**RPC:** the public Solana RPC works but allows about one transaction lookup per second, so a first
check takes 1–4 minutes. For more speed put a faster RPC (a free Helius key works) in `.env`,
see `.env.example`. Never put keys in config.json or commit `.env`.

Check it against known tokens (5 reference launches, prints every metric and the score):

```bash
python3 check_onchain.py              # exits 1 if a reference token gets the wrong result
python3 check_onchain.py <mint> ...   # any pump.fun tokens
```

## Telegram alerts

In config.json set `"enabled": true`, your `bot_token` (from @BotFather) and `chat_id`.
Each token alerts once when it reaches `min_score`.

## Run it as a service

```ini
# /etc/systemd/system/token-scout.service
[Unit]
Description=Token Scout
After=network-online.target

[Service]
WorkingDirectory=/opt/token_scout
ExecStart=/usr/bin/python3 /opt/token_scout/scanner.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now token-scout
journalctl -u token-scout -f
```

## Rate limits

GeckoTerminal's free API allows about 30 calls per minute, so the scanner waits ~2.2 s between calls.
Per scan: 4 chains × (2 new-pool pages + 1 trending page) = 12 calls, plus 1 call per 30 watched pools
(a few hundred pools with the default `watch_minutes`, mostly Solana). DexScreener and the security
check run once per token, then again every `details_refresh_minutes` while it is listed.
If you add chains or pages, or raise `watch_minutes`, keep the scan interval at 5 minutes or more.

## Tuning

- Too many tokens: raise `min_liquidity_usd`, `min_txns_h1` or `min_age_minutes`.
- Want earlier entries: lower `min_age_minutes` (more rugs get through in the first 30 minutes).
- Tokens that take off later than an hour after their pool opened: raise `watch_minutes`
  (more API calls per scan). Active ones are also picked up from the trending list.
- Scoring weights live in `score_token()` in scanner.py.

A high score only means the token passed automated checks. Always read the contract, the team and the docs yourself.
