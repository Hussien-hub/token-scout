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
5. **Score**
6. **Stay or go** – every listed token is re-checked on each scan and stays only while it still
   meets all of the above. It is removed once it fails them (liquidity pulled, activity gone,
   older than `max_age_hours`, turned into a honeypot). DexScreener and security results are
   reused for `details_refresh_minutes`; liquidity, volume, txns and price refresh every scan.

| Part | Max | What counts |
|---|---|---|
| Safety | 35 | Starts at 35, minus penalties: unverified source, owner can change balances, mintable, proxy, hidden owner, taxes, top-holder concentration, unlocked LP, RugCheck risks. Unchecked tokens get 14. |
| Liquidity | 15 | Depth on a log scale ($10k → 0, $500k → 12) plus a healthy liquidity/FDV ratio (8–60%). |
| Activity | 20 | Unique buyers and txns in the last hour, buy/sell ratio between 1 and 3. Many txns from few wallets scores 0 here. |
| Momentum | 10 | Volume turnover vs liquidity, and a 1h price move of 0–80%. Tokens already up 200%+ get nothing. |
| Project | 20 | Website 7, X 5, Telegram 4, Discord 2, DexScreener profile 2. |

Each scan rescores tracked tokens, so the dashboard shows the change since first seen and a score trend.

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
