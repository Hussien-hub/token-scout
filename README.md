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

1. **Discovery** – GeckoTerminal `new_pools` for every chain in `networks`
   (solana, base, bsc, eth, arbitrum, polygon_pos).
2. **Hard filters** (`filters` in config.json) – age, liquidity, FDV range, 1h volume, 1h txns.
   One entry per token (the pool with the most liquidity).
3. **Enrichment** – DexScreener adds website, X, Telegram, Discord and paid boosts.
4. **Security** – GoPlus for EVM chains, RugCheck for Solana.
   Honeypots, freeze authority and sell tax above `max_sell_tax` are dropped.
5. **Score**

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
4 chains × 2 pages = 8 calls per scan. RugCheck is called once per Solana token that passed the filters.
If you add chains or pages, keep the scan interval at 5 minutes or more.

## Tuning

- Too many tokens: raise `min_liquidity_usd`, `min_txns_h1` or `min_age_minutes`.
- Want earlier entries: lower `min_age_minutes` (more rugs get through in the first 30 minutes).
- Scoring weights live in `score_token()` in scanner.py.

A high score only means the token passed automated checks. Always read the contract, the team and the docs yourself.
