# IADSS Signal Tracker

A TradingView webhook receiver that executes trades on Freqtrade when the
[IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/)
by [Gregusm](https://www.tradingview.com/u/gregusm/) fires a complete buy or sell sequence.

The indicator handles all signal sequencing (Mean Reversion → Confluence → Trend flip)
directly on the chart. This server receives the final alerts and executes trades via
the Freqtrade API — no server-side state machine required.

> **Spot markets only.** The IADSS indicators are calibrated for spot price action.
> Using them with futures or perpetuals is not supported.

---

## How it works

1. Add Gregusm's **IADSS Confluence Monitor** to your TradingView chart
2. The indicator monitors three layers internally and fires alerts when a full sequence completes
3. TradingView sends a webhook to this server
4. The server calls the Freqtrade API to execute the trade

**BUY flow:** Indicator fires `BUY Sequence Complete` → `/lb-buy` → buys a configurable % of free balance (default: 50%)

**SELL flow:** Indicator fires `SELL Sequence Complete` → `/lb-sell` → sells a configurable % of open position (default: 50%)

---

## Webhook endpoints

| Endpoint | Description |
|---|---|
| `POST /lb-buy` | BUY Sequence Complete — executes buy (configurable stake, default 50% of free balance) |
| `POST /lb-sell` | SELL Sequence Complete — executes sell (configurable size, default 50% of open position) |
| `POST /confirm-buy` | BUY Early Warning — Telegram notification only, no trade |
| `POST /confirm-sell` | SELL Early Warning — Telegram notification only, no trade |
| `GET /status` | Current open trade info from Freqtrade |
| `GET /health` | Health check |

---

## Setup

### 1. Prerequisites

- Docker and Docker Compose installed
- A supported spot exchange account (Kraken, Binance, Coinbase, etc.)
- TradingView account with webhook alerts
- Telegram bot (optional, for notifications)

### 2. Configure

```bash
mkdir -p user_data/strategies
cp strategies/WebhookStrategy.py user_data/strategies/
cp config.json user_data/
```

Edit `user_data/config.json` and replace all `CHANGE_THIS` values:

- Exchange credentials (API key + secret)
- Telegram bot token and chat ID
- Freqtrade API password
- JWT secret key (random 32+ char string)
- Your trading pair whitelist

Edit `docker-compose.yml` and replace all `CHANGE_THIS` values:

- `SECRET_TOKEN` — random string for webhook authentication
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `FREQTRADE_PASS` — must match `config.json` API password

### 3. Generate secure values

```bash
# Webhook secret token
openssl rand -hex 24

# Freqtrade JWT secret
openssl rand -hex 32
```

### 4. Start

```bash
docker compose up -d
```

### 5. Set up TradingView alerts

#### Add the indicator

1. Open TradingView → your chart (e.g. SOLUSDT, 4H)
2. Click **Indicators** → search **"IADSS Confluence Monitor"** → add it
   - Direct link: https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/

#### Create alerts (2 required, 2 optional)

**Required — trade execution:**

| Alert name | Condition | Webhook URL |
|---|---|---|
| IADSS BUY | `BUY Sequence Complete` | `https://your-domain/lb-buy?token=YOUR_TOKEN` |
| IADSS SELL | `SELL Sequence Complete` | `https://your-domain/lb-sell?token=YOUR_TOKEN` |

**Optional — early warning Telegram notifications:**

| Alert name | Condition | Webhook URL |
|---|---|---|
| IADSS BUY Early Warning | `BUY Early Warning` | `https://your-domain/confirm-buy?token=YOUR_TOKEN` |
| IADSS SELL Early Warning | `SELL Early Warning` | `https://your-domain/confirm-sell?token=YOUR_TOKEN` |

**Alert settings:**

- Expiration: Open-ended
- Alert actions: Webhook URL only
- Message body:
  ```json
  {"pair": "SOL/USD"}
  ```

### 6. Access Freqtrade UI

Open `http://localhost:8067` in your browser.

### 7. Go live

When happy with dry run performance:

1. Set `"dry_run": false` in `config.json`
2. Restart: `docker compose restart freqtrade`

---

## Self-hosted setup (Unraid / home server)

Use `docker-compose.selfhosted.yml` instead of the default compose file.
This version uses a Cloudflare Tunnel for external access instead of a reverse proxy,
so no ports are exposed to the internet directly.

```bash
docker compose -f docker-compose.selfhosted.yml up -d
```

Freqtrade UI is accessible on port `8067` on your local network.
The signal-tracker webhook server is reachable via your Cloudflare Tunnel URL.

---

## Position sizing

- Each buy stakes **a configurable % of your available balance** at the time of the signal (set via `STAKE_RATIO`, default `0.5` = 50%)
- Each sell exits **a configurable % of the current open position** (set via `SELL_RATIO`, default `0.5` = 50%)
- Supports multiple buys on the same pair (DCA / pyramiding)
- Minimum stake enforced by `MIN_STAKE` env var (default `$10`)

To adjust, set these in your `docker-compose.yml` environment block:

```yaml
STAKE_RATIO: "0.25"   # buy 25% of free balance per signal
SELL_RATIO: "1.0"     # sell 100% of position on exit
MIN_STAKE: "20"       # minimum USD stake
```

## Adding more pairs

```json
"pair_whitelist": ["SOL/USD", "BTC/USD", "ETH/USD"]
```

Create separate TradingView alerts for each pair with the pair name in the message body.

---

## Security

- All webhook endpoints require `?token=YOUR_SECRET_TOKEN` in the URL
- Freqtrade UI should be behind a reverse proxy or Cloudflare Tunnel — never exposed directly
- **Never enable withdrawal permissions on your exchange API keys**
- Never commit `.env` or `user_data/` — both are gitignored
- Use `openssl rand -hex 24` to generate your secret token

---

## Telegram notifications

Once your Telegram bot is configured you will receive:

- BUY and SELL early warnings (if confirm alerts are set up)
- Trade execution confirmations with stake, rate, and trade ID
- Failure alerts with reason

You can also send commands directly to your Freqtrade bot:

- `/status` — open trades
- `/profit` — profit summary
- `/balance` — current balance
- `/stop` — stop the bot
- `/start` — start the bot

---

## Acknowledgements

A huge thank you to **[Gregusm](https://www.tradingview.com/u/gregusm/)** for creating the
**IADSS Confluence Monitor**
([view on TradingView](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/)).

This project was originally built with a custom 3-step state machine to sequence Mean Reversion,
Confluence, and Trend flip signals server-side. Following an external audit, we switched to
Gregusm's indicator which handles the entire sequence on the chart — producing cleaner signals
with no server-side state to manage or expire.

If you find his work useful, please give it a like and follow him on TradingView.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## ⚠️ Disclaimer

This software is for educational and informational purposes only. It is not financial advice.
Trading cryptocurrencies and other financial instruments involves significant risk of loss.
Past performance is not indicative of future results. You may lose some or all of your invested capital.

By using this software you acknowledge that:

- You are solely responsible for your trading decisions
- The authors accept no liability for any financial losses incurred
- You should never trade with money you cannot afford to lose
- This software comes with no guarantee of profit or performance
- You should seek independent financial advice before trading

**Use at your own risk.**

