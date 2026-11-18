# IADSS Signal Tracker

Webhook receiver for spot trades on Freqtrade. TradingView fires the alert. This server sizes the order from the live exchange balance and tells Freqtrade to buy or sell.

It is not tied to one indicator. The same endpoints serve:

- the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/) by Gregusm
- ATR alerts (or any other TradingView alert that POSTs the same JSON)

The tracker does not read the chart. It only checks the token and the `pair`, then calls Freqtrade.

> **Spot only.** Futures and perps are not supported.

Required containers: `freqtrade` and `signal-tracker`. Dry-run vs live is **not** a tracker setting. It is `"dry_run"` in `user_data/config.json`.

A running container does not pick up git changes on its own. Rebuild `signal-tracker` after a sizing change. Restart `freqtrade` after a `WebhookStrategy.py` change.

## How it works

| Alert | Endpoint | Action |
|-------|----------|--------|
| BUY early warning | `/confirm-buy` | Telegram only |
| BUY | `/lb-buy` | Buy `STAKE_RATIO` of the live free stake currency |
| SELL early warning | `/confirm-sell` | Telegram only |
| SELL | `/lb-sell` | Sell `SELL_RATIO` of that pair's coin balance |

IADSS names those alerts "sequence complete" (MR + Confluence, then the trend flip). An ATR alert is just a buy or a sell. Both hit the same URLs.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Open trade, live free balance, next buy stake |
| `/ledger` | GET | Bookkeeping only. Not used to size orders |
| `/deposit` | POST | Add cash to a pair ledger |
| `/withdraw` | POST | Remove cash from a pair ledger |
| `/position/add` | POST | Mark existing coins as already in a position |
| `/position/remove` | POST | Stop tracking some coins as a managed position |
| `/health` | GET | Health check (no auth) |

`/status` reports `stake_source: live_exchange` and `next_buy_stake` from the live free balance. A buy log line looks like `live USD $500.00`. The ledger JSON has no buy size in it.

## ATR signals

ATR uses this same tracker. It is not a different program. Run it as its **own** compose stack so it does not share the other bot's container, database, or ledger. Give that copy its own `container_name` and host ports. `FREQTRADE_API` inside the compose file stays `http://freqtrade:8080/api/v1`: that is the service name on that stack's network, not some other container called freqtrade.

The published ports in this repo are:

| File | Freqtrade UI | Webhook |
|------|----------------|---------|
| `docker-compose.yml` | host `8067` → `8080` | host `5001` → `5000` |
| `docker-compose.selfhosted.yml` | host `8067` → `8080` | host `5000` → `5000` |

One running second stack (ATR on Kraken) uses different ports so it can sit next to the first one:

| | First stack | ATR example |
|--|-------------|-------------|
| Freqtrade container | `freqtrade` | `ATR_freqtrade` |
| Signal tracker | `signal-tracker` | `ATR_signal-tracker` |
| UI | host `8067` | host `8057` |
| Webhook | host `5000` or `5001` | host `5011` |
| Public webhook | your own hostname | `https://atr-signals.ballzac.uk` |

Use your own hostname. `atr-signals.ballzac.uk` is just that example. Point Cloudflare at the signal-tracker port of the stack that should receive the alert, with an empty path. A hostname aimed at the wrong port returns 502.

ATR buy alert:

```
https://atr-signals.ballzac.uk/lb-buy
```

```json
{"pair": "HYPE/USD", "token": "YOUR_SECRET_TOKEN"}
```

The sell alert is the same body posted to `/lb-sell`. `/confirm-buy` and `/confirm-sell` only send Telegram.

Whitelist the pair on **that** bot. The buy, the sell, and the stop then apply to that bot's open trade only. They do not look at other coins on the exchange account.

Copy `strategies/WebhookStrategy.py` into that stack's `user_data/strategies/` and restart only that freqtrade container. Rebuilding `signal-tracker` does not reload the strategy.

## Dry run vs live

| Mode | `user_data/config.json` | What happens |
|------|-------------------------|--------------|
| Paper | `"dry_run": true` | Simulated fills, FreqUI shows a green **Dry** badge |
| Live | `"dry_run": false` | Real exchange orders |

`.env` does **not** control this.

```bash
grep -E '"dry_run"|"dry_run_wallet"' user_data/config.json
curl -u admin:$FREQTRADE_PASS http://127.0.0.1:8067/api/v1/show_config | grep dry_run
```

Go live only after a webhook sizes from the live balance:

1. Exchange API key is trade-only (never withdrawals)
2. Set `"dry_run": false` in `user_data/config.json`
3. `docker compose restart freqtrade` (or `docker restart freqtrade`)
4. Confirm the Dry badge is gone and the UI balance is the exchange wallet

Telegram lines (`IADSS BUY executed`) fire in **both** modes. They are not proof of a live fill.

## TradingView alert setup

Paid TradingView plan + 2FA required for webhooks. Set alerts to **Once per bar close**.

```
https://signals.yourdomain.com/lb-buy
```

```json
{"pair": "HYPE/USD", "token": "YOUR_SECRET_TOKEN"}
```

Also supported: `?token=` on the URL, or headers `X-Token` / `X-Webhook-Secret`.

TradingView cannot reach a LAN IP such as `10.10.20.10`. Use the Cloudflare hostname.

There is no "fire now" button on an existing alert. To test from TradingView, create a one-shot price-cross alert with the same URL and JSON. To test without TradingView:

```bash
curl -X POST http://127.0.0.1:5001/lb-buy \
  -H "Content-Type: application/json" \
  -d '{"pair": "HYPE/USD", "token": "YOUR_SECRET_TOKEN"}'
```

`5001` is the host port in `docker-compose.yml`. The self-hosted file publishes `5000`. A second stack uses whatever port you gave it.

Compare to PTOS: this repo uses `/lb-buy` and `"pair": "HYPE/USD"`, not `/buy-signal` and `"coin": "HYPE"`.

## Position sizing

Orders are sized from the live exchange balance Freqtrade reports. The per-pair ledger is not used.

| Variable | Default | Description |
|----------|---------|-------------|
| `STAKE_RATIO` | `0.5` | Fraction of **live free stake currency** (USD) per buy |
| `SELL_RATIO` | `0.5` | Fraction of the **total base-coin balance** of that pair per sell |
| `STAKE_CURRENCY` | `USD` | Stake currency. Kraken USD is also read as `ZUSD` |
| `MIN_STAKE` | `10` | Skip the buy if the computed stake is below this |
| `TRADING_PAIR` | `SOL/USD` | Default pair if the webhook omits `pair` |

A pair-specific value wins over the global one:

```
STAKE_RATIO_HYPE_USD=0.33
SELL_RATIO_HYPE_USD=0.33
```

`1` on a sell ratio sells the whole coin balance of that pair. Restart `signal-tracker` after changing these.

Two limits:

- A buy spends that fraction of the **same** free USD pot. Pairs do not have separate bankrolls. Two buys in a row, with nothing filled in between, can each try to spend 50% of whatever is still free.
- A sell is that fraction of the total coins the exchange holds for the pair. Free balance is the wrong number once a stop is resting, because the stop locks the coins. Freqtrade still only sells what an open trade covers. Coins bought outside the bot, or filled while the bot crashed before it wrote a trade, are not sold. The log says so. If the entry order is still unfilled (`amount` is 0), the sell is skipped.
- The included config sets `position_adjustment_enable`, which partial sells need. Turning that off makes a 50% sell fail.

Freqtrade must receive the stake on **`stakeamount`** (and `stake_amount`). If only `stake_amount` is sent, current Freqtrade ignores it and falls back to `available / max_open_trades`. This repo sends both keys.

Also set in `user_data/config.json`:

```json
"stake_amount": "unlimited",
"tradable_balance_ratio": 1.0,
"fee": 0.0026
```

- `tradable_balance_ratio: 0.5` hides half the wallet from Freqtrade, so the live-balance stake is half of what you expect.
- `"fee": 0.0026` avoids a Kraken crash on older images (`FtPrecise` / empty fee). Current `freqtradeorg/freqtrade:stable` (2026.9 or newer) is what this stack runs.

## Stop loss

`WebhookStrategy` places the stop. It covers the **whole open trade of that pair**, and nothing else on the account. After a 50% sell, the stop shrinks to whatever of that trade is still open.

| Stage | Stop |
|-------|------|
| Until the trade is +12% | 10% under the entry. Does not move |
| From +12% on | 10% under the highest price since entry. Only moves up |

The first trailed stop is about 0.8% over the entry, so it is still green, and price can fall 10% from the high before it hits. That is the crash exit. The sell webhook is still how the indicator closes the trade. `minimal_roi` is `{"0": 100}`. That number is a ratio, so this is 10,000% and ROI never fires.

`user_data/config.json` puts that stop on the exchange:

```json
"order_types": {
    "entry": "limit",
    "exit": "limit",
    "stoploss": "limit",
    "stoploss_on_exchange": true,
    "stoploss_on_exchange_interval": 60
}
```

Freqtrade cancels and replaces the exchange stop as the trail moves, about once a minute. If the bot is offline, the last stop stays on the exchange and can still fill, but it will not trail higher until the bot is back.

The stop is only placed after the entry fills. Restarting freqtrade does not sell. It replaces the exchange stop, so there is a short gap with no stop.

## Per-pair ledger

`/data/ledger.json` is bookkeeping. Buys and sells do not move it, and `ALLOCATION_HYPE_USD` does not cap an order.

`ALLOCATION_SOL_USD=1000` still seeds a row the first time that pair is seen. After that, use `/deposit` and `/withdraw`. Changing the env var later does not top up an existing row.

```bash
curl "http://HOST:5000/ledger?token=YOUR_SECRET_TOKEN"

curl -X POST http://HOST:5000/deposit \
  -H "Content-Type: application/json" \
  -d '{"pair": "HYPE/USD", "amount": 200, "token": "YOUR_SECRET_TOKEN"}'
```

## Setup

### Prerequisites
- Docker and Docker Compose
- Spot exchange account supported by Freqtrade (Kraken, Coinbase, Binance, …)
- TradingView
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
- Telegram token and chat id. The template ships with `"telegram": {"enabled": false}`. Set `enabled` to `true` only after the token is real.
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

A second stack (ATR) needs its own directory, `container_name`, and host ports. Do not reuse `freqtrade` or `8067`.

### 5. Access Freqtrade UI

- VPS (`docker-compose.yml`): UI `http://YOUR_SERVER_IP:8067`, webhooks on host port `5001`
- Self-hosted file: UI host port `8067`, webhooks on host port `5000`
- A second stack uses the ports you published. The ATR example is UI `8057` and webhooks `5011`
- FreqUI "Bot Name" is a label. Username is `api_server.username` (default `admin`). Times in the UI are UTC.

### 6. Go live

See [Dry run vs live](#dry-run-vs-live) above.

## Self-hosted / Cloudflare

`docker-compose.selfhosted.yml` publishes:

- `signal-tracker` `5000:5000`
- `freqtrade` `8067:8080` (host 8067 → container 8080)

`docker-compose.yml` publishes the webhook on **5001**, not 5000. Freqtrade still listens on **8080 inside the container**. Do not map host 8080 if another app already owns it.

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

or `HOST_LAN_IP:5000` for the self-hosted file, and `:5001` for `docker-compose.yml`. A second stack's hostname must point at that stack's webhook port, not this one.

## Security

- Trade endpoints require `SECRET_TOKEN` (URL, header, or JSON body)
- Rate limits: 10/min trades, 30/min early warnings, 60/min status
- Pair names must match `BASE/QUOTE`
- Never enable withdrawal on exchange API keys
- Do not commit `.env`

## Adding pairs

Whitelist the pair on the bot that should trade it, then send `"pair"` in the alert body. Sizing is the live free balance on that bot, not a per-pair ledger.

## License

MIT License — see [LICENSE](LICENSE).

## Acknowledgements

IADSS signal sequencing by the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/) by Gregusm.

## Disclaimer

This software is for educational purposes only and is not financial advice. Trading involves significant risk of loss. You are solely responsible for your trading decisions. The authors accept no liability for any financial losses. Never trade with money you cannot afford to lose. Test thoroughly in dry-run mode before going live.
