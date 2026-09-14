# IADSS Signal Tracker

A webhook receiver for the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/) by Gregusm. Receives TradingView alerts and executes spot trades via the Freqtrade API.

> **Spot only.** The IADSS Confluence Monitor indicators work with spot markets. Futures/perps are not supported.

Required containers: `freqtrade` and `signal-tracker`. Dry-run vs live is **not** an IADSS setting — it is `"dry_run"` in `user_data/config.json`.

## How it works

The IADSS Confluence Monitor on TradingView handles signal sequencing internally (MR alignment -> Confluence -> Trend flip). When the sequence completes it fires a webhook. This server receives that webhook and calls Freqtrade `/forcebuy` or `/forcesell`.

| Alert | Endpoint | Action |
|-------|----------|--------|
| BUY Early Warning (MR + Confluence aligned) | `/confirm-buy` | Telegram notification only |
| BUY Sequence Complete (all conditions met) | `/lb-buy` | Executes buy via Freqtrade |
| SELL Early Warning | `/confirm-sell` | Telegram notification only |
| SELL Sequence Complete | `/lb-sell` | Executes sell via Freqtrade |

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Current open trade + ledger info for a pair |
| `/ledger` | GET | All pair bankrolls, P&L summary |
| `/deposit` | POST | Add cash to a pair ledger |
| `/withdraw` | POST | Remove cash from a pair ledger |
| `/position/add` | POST | Mark existing coins as already in a position |
| `/position/remove` | POST | Stop tracking some coins as a managed position |
| `/health` | GET | Health check (no auth) |

## Dry run vs live

IADSS has no dry-run flag. Freqtrade does.

| Mode | `user_data/config.json` | What happens |
|------|-------------------------|--------------|
| Paper | `"dry_run": true` | Simulated fills, FreqUI shows a green **Dry** badge |
| Live | `"dry_run": false` | Real exchange orders |

`.env` does **not** control this.

Check the running bot:

```bash
grep -E '"dry_run"|"dry_run_wallet"' user_data/config.json
curl -u admin:$FREQTRADE_PASS http://127.0.0.1:8067/api/v1/show_config | grep dry_run
```

Go live only after webhooks, sizing, and ledgers look right:

1. Exchange API key is trade-only (never withdrawals)
2. Set `"dry_run": false` in `user_data/config.json`
3. `docker compose restart freqtrade` (or `docker restart freqtrade`)
4. Confirm the Dry badge is gone and the UI balance is the exchange wallet

IADSS Telegram lines (`IADSS BUY executed`) fire in **both** modes. They are not proof of a live fill.

## TradingView alert setup

Paid TradingView plan + 2FA required for webhooks. Create 4 alerts on the IADSS Confluence Monitor, **Once per bar close**.

Webhook URL (public HTTPS hostname that tunnels to `signal-tracker:5000`):

```
https://signals.yourdomain.com/lb-buy
```

Message body must be JSON:

```json
{"pair": "HYPE/USD", "token": "YOUR_SECRET_TOKEN"}
```

Also supported: `?token=` on the URL, or headers `X-Token` / `X-Webhook-Secret`.

TradingView cannot reach a LAN IP such as `10.10.20.10`. Use the Cloudflare hostname.

There is no "fire now" button on an existing alert. To test from TradingView, create a one-shot price-cross alert with the same URL and JSON. To test without TradingView:

```bash
curl -X POST http://10.10.20.10:5000/lb-buy \
  -H "Content-Type: application/json" \
  -d '{"pair": "HYPE/USD", "token": "YOUR_SECRET_TOKEN"}'
```

Compare to PTOS: IADSS uses `/lb-buy` and `"pair": "HYPE/USD"`, not `/buy-signal` and `"coin": "HYPE"`.

## Position sizing

| Variable | Default | Description |
|----------|---------|-------------|
| `STAKE_RATIO` | `0.5` | Fraction of **ledger free cash** (or Freqtrade available capital) per buy |
| `SELL_RATIO` | `0.5` | Fraction of the open position sold per sell |
| `MIN_STAKE` | `10` | Skip buy if computed stake is below this |
| `TRADING_PAIR` | `SOL/USD` | Default pair if the webhook omits `pair` |

Per-pair overrides:

```
STAKE_RATIO_HYPE_USD=0.33
SELL_RATIO_HYPE_USD=0.33
```

Freqtrade must receive the stake on **`stakeamount`** (and `stake_amount`). If only `stake_amount` is sent, current Freqtrade ignores it and falls back to `available / max_open_trades` (e.g. $1500 / 10 = $150). This repo sends both keys.

Also set in `user_data/config.json`:

```json
"stake_amount": "unlimited",
"tradable_balance_ratio": 1.0,
"fee": 0.0026
```

- `tradable_balance_ratio: 0.5` hides half the dry/live wallet from Freqtrade. Use `1.0` when pair ledgers already sum to the wallet you want to trade.
- `"fee": 0.0026` avoids a Kraken dry-run crash (`FtPrecise` / empty `fee_open`). Use your real taker rate live if it differs.

## Per-pair ledger

Ledgers are IADSS bookkeeping in `/data/ledger.json`. They do **not** move money on the exchange and they do **not** change `dry_run_wallet`.

`ALLOCATION_SOL_USD=1000` in `.env` only **seeds** a pair the first time it is seen. After that, use `/deposit` and `/withdraw`. Changing the env var later does not top up an existing row.

Example: $3000 paper wallet, three pairs, 50% first buy:

```
POST /deposit  {"pair":"SOL/USD","amount":1000,"token":"..."}
POST /deposit  {"pair":"HYPE/USD","amount":1000,"token":"..."}
POST /deposit  {"pair":"TAO/USD","amount":1000,"token":"..."}
```

Next `/lb-buy` per pair is $500. When live you still need the same USD sitting on the exchange.

```bash
# Inspect
curl "http://HOST:5000/ledger?token=YOUR_SECRET_TOKEN"

# Add cash to a running ledger (no restart)
curl -X POST http://HOST:5000/deposit \
  -H "Content-Type: application/json" \
  -d '{"pair": "HYPE/USD", "amount": 200, "token": "YOUR_SECRET_TOKEN"}'
```

If you already hold coins, `/position/add` so that cost is not treated as free cash.

## Setup

### Prerequisites
- Docker and Docker Compose
- Spot exchange account supported by Freqtrade (Kraken, Coinbase, Binance, …)
- TradingView + IADSS Confluence Monitor
- Telegram bot (optional)

### 1. Clone and configure

```
git clone https://github.com/ballzac81/IADSS-Signal-Tracker.git
cd IADSS-Signal-Tracker
cp .env.example .env
```

### 2. Freqtrade config

```
mkdir -p user_data/strategies
cp config.json user_data/
cp strategies/WebhookStrategy.py user_data/strategies/
```

Edit `user_data/config.json`:
- Exchange API key and secret (read + trade only)
- Telegram token and chat id
- `api_server.password` (must match `.env` `FREQTRADE_PASS`)
- JWT secret (`openssl rand -hex 32`)
- Pair whitelist
- Leave `"dry_run": true` until you have tested

### 3. Secrets

```
openssl rand -hex 24   # SECRET_TOKEN
openssl rand -hex 32   # JWT secret
```

### 4. Start

VPS / standard:

```
docker compose up -d
```

Self-hosted (Unraid / NAS):

```
docker compose -f docker-compose.selfhosted.yml up -d
```

### 5. Access Freqtrade UI

- VPS: `http://YOUR_SERVER_IP:8067`
- Self-hosted: `https://freqtrade.yourdomain.com` or `https://trade.yourdomain.com`
- FreqUI "Bot Name" is a label (`IADSS`). Username is `api_server.username` (default `admin`). Times in the UI are UTC.

### 6. Go live

See [Dry run vs live](#dry-run-vs-live) above.

## Self-hosted / Cloudflare

`docker-compose.selfhosted.yml` publishes:

- `signal-tracker` `5000:5000`
- `freqtrade` `8067:8080` (host 8067 → container 8080)

Freqtrade listens on **8080 inside the container**. Do not map host 8080 if qBittorrent (or anything else) already owns it.

Cloudflare public hostname for the UI:

- Best: `HTTP` → `freqtrade:8080` (cloudflared on the same Docker network, e.g. `ballzac`)
- Fallback: `HTTP` → `HOST_LAN_IP:8067` after the `8067:8080` publish exists
- Path field: **empty** (do not leave the `^/blog` example)
- `10.10.20.10:8067` with no published port = connection refused / Cloudflare 502
- `10.10.20.10:8080` is often a different container

Webhook hostname:

```
signals.yourdomain.com  →  HTTP  signal-tracker:5000
```

or `HOST_LAN_IP:5000`.

## Security

- Trade endpoints require `SECRET_TOKEN` (URL, header, or JSON body)
- Rate limits: 10/min trades, 30/min early warnings, 60/min status
- Pair names must match `BASE/QUOTE`
- Never enable withdrawal on exchange API keys
- Do not commit `.env`

## Adding pairs

Whitelist in `user_data/config.json`, then a TradingView alert whose body has that `pair`. Optionally `/deposit` a bankroll for the new pair.

## License

MIT License — see [LICENSE](LICENSE).

## Acknowledgements

Signal sequencing by the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/) by Gregusm.

## Disclaimer

This software is for educational purposes only and is not financial advice. Trading involves significant risk of loss. You are solely responsible for your trading decisions. The authors accept no liability for any financial losses. Never trade with money you cannot afford to lose. Test thoroughly in dry-run mode before going live.
