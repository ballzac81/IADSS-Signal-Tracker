# IADSS Signal Tracker

A webhook receiver for the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/) by Gregusm. Receives TradingView alerts and executes spot trades via the Freqtrade API.

> **Spot only.** The IADSS Confluence Monitor indicators work with spot markets. Futures/perps are not supported.

## How it works

The IADSS Confluence Monitor on TradingView handles all signal sequencing internally (MR alignment -> Confluence -> Trend flip). When the full sequence completes, it fires a webhook. This server receives that webhook and executes the trade.

Two alert types per side:

| Alert | Endpoint | Action |
|-------|----------|--------|
| BUY Early Warning (MR + Confluence aligned) | `/confirm-buy` | Telegram notification only |
| BUY Sequence Complete (all conditions met) | `/lb-buy` | Executes buy via Freqtrade |
| SELL Early Warning | `/confirm-sell` | Telegram notification only |
| SELL Sequence Complete | `/lb-sell` | Executes sell via Freqtrade |

Additional endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Current open trade + ledger info for a pair |
| `/ledger` | GET | All pair bankrolls, P&L summary |
| `/health` | GET | Health check (no auth) |

## TradingView alert setup

Create 4 alerts on the IADSS Confluence Monitor. Set each to fire "Once per bar close" and add your webhook URL.

Webhook URL format:

    https://signals.yourdomain.com/lb-buy?token=YOUR_SECRET_TOKEN

Webhook message body (JSON):

    {"pair": "SOL/USD"}

For multi-pair setups, set the pair in the message body. The `token` can go in the URL or as an `X-Token` header.

## Position sizing

Position sizes are fully configurable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `STAKE_RATIO` | `0.5` | Fraction of available balance used per buy (0.5 = 50%) |
| `SELL_RATIO` | `0.5` | Fraction of open position sold per sell signal (0.5 = 50%) |
| `MIN_STAKE` | `10` | Minimum USD stake -- skips buy if below this |
| `TRADING_PAIR` | `SOL/USD` | Default pair if not specified in webhook body |

## Per-pair ledger (isolated bankrolls)

Run multiple pairs simultaneously with completely separate bankrolls. SOL profits never fund HYPE trades and vice versa.

Set an allocation for each pair in `.env`:

    ALLOCATION_SOL_USD=1000
    ALLOCATION_HYPE_USD=1000

Each pair tracks its own liquid cash, cost basis of units currently in trade, and total P&L vs the starting allocation.

Check all pair balances any time:

    GET /ledger?token=YOUR_SECRET_TOKEN

Example response:

    {
      "pairs": {
        "SOL/USD": {"allocated": 1000, "current": 850, "in_trade": 250, "total": 1100, "pnl": 100},
        "HYPE/USD": {"allocated": 1000, "current": 500, "in_trade": 500, "total": 1000, "pnl": 0}
      },
      "summary": {"total_allocated": 2000, "total_value": 2100, "total_pnl": 100}
    }

Omit `ALLOCATION_*` vars to fall back to free-balance mode (stakes against total exchange balance).

## Setup

### Prerequisites
- Docker and Docker Compose
- A spot exchange account supported by Freqtrade (Kraken, Coinbase, Binance etc.)
- TradingView account with the IADSS Confluence Monitor indicator
- Telegram bot (optional, for trade notifications)

### 1. Clone and configure

    git clone https://github.com/ballzac81/IADSS-Signal-Tracker.git
    cd IADSS-Signal-Tracker
    cp .env.example .env

Edit `.env` with your values.

### 2. Set up Freqtrade config

    mkdir -p user_data/strategies
    cp config.json user_data/
    cp strategies/WebhookStrategy.py user_data/strategies/

Edit `user_data/config.json` and replace all `CHANGE_THIS` placeholders:
- Exchange API key and secret (read + trade only -- never enable withdrawals)
- Telegram bot token and chat ID
- Freqtrade API password
- JWT secret key (openssl rand -hex 32)
- Your trading pair whitelist

### 3. Generate secrets

    openssl rand -hex 24   # SECRET_TOKEN for webhook auth
    openssl rand -hex 32   # JWT secret for Freqtrade UI

### 4. Start

VPS / standard:

    docker compose up -d

Self-hosted (Unraid, NAS, home server):

    docker compose -f docker-compose.selfhosted.yml up -d

### 5. Access Freqtrade UI

- VPS: `http://YOUR_SERVER_IP:8067`
- Self-hosted: `https://trade.yourdomain.com` (via Cloudflare Tunnel)

### 6. Go live

Test thoroughly with `"dry_run": true` first. When ready:

1. Set `"dry_run": false` in `user_data/config.json`
2. `docker compose restart`

## Self-hosted deployment

`docker-compose.selfhosted.yml` uses Cloudflare Tunnel instead of open ports -- no port forwarding needed, works behind CGNAT, Cloudflare handles HTTPS.

Two options:
- **Option A** -- You already have a Cloudflare Tunnel container running (e.g. Unraid Community App). Set `DOCKER_NETWORK` in `.env`.
- **Option B** -- Fresh setup. Create a tunnel in Cloudflare Zero Trust, add the token to `.env` as `CLOUDFLARE_TUNNEL_TOKEN`, and uncomment the `cloudflared` service.

In Cloudflare Zero Trust -> Tunnels -> your tunnel -> Public Hostnames:

    signals.yourdomain.com  ->  http://signal-tracker:5000
    trade.yourdomain.com    ->  http://freqtrade:8080

## Security

- All trade endpoints require `SECRET_TOKEN` (URL param or `X-Token` header)
- Rate limiting: 10/min on trade endpoints, 30/min on early warnings, 60/min on status
- Pair validation: rejects malformed pair names
- Never enable withdrawal permissions on exchange API keys
- The `.env` file is gitignored -- never commit it

## Adding more pairs

Add pairs to the whitelist in `config.json`:

    "pair_whitelist": ["SOL/USD", "BTC/USD", "ETH/USD"]

Create separate TradingView alerts for each pair with the pair name in the message body:

    {"pair": "BTC/USD"}

Optionally give each pair its own bankroll in `.env`:

    ALLOCATION_BTC_USD=2000

## License

MIT License -- see [LICENSE](LICENSE) for details.

## Acknowledgements

Signal sequencing powered by the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/) by Gregusm.

## Disclaimer

This software is for educational purposes only and is not financial advice. Trading involves significant risk of loss. You are solely responsible for your trading decisions. The authors accept no liability for any financial losses. Never trade with money you cannot afford to lose. Test thoroughly in dry-run mode before going live.
