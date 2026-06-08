#!/usr/bin/env python3
"""IADSS Signal Tracker — Webhook receiver for the IADSS Confluence Monitor.

Designed to work with the IADSS Confluence Monitor indicator by Gregusm:
https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/

The indicator handles all signal sequencing (MR -> Confluence -> Trend flip)
internally on the chart. This server receives the completion alerts and
executes trades via the Freqtrade API.

Endpoints:
  POST /lb-buy        BUY Sequence Complete  -> executes buy  (50% of free balance)
  POST /lb-sell       SELL Sequence Complete -> executes sell (50% of open position)
  POST /confirm-buy   BUY Early Warning      -> Telegram notification only
  POST /confirm-sell  SELL Early Warning     -> Telegram notification only
  GET  /status        current open trade info from Freqtrade
  GET  /health        health check
"""

import logging
import os
import time
import requests
from functools import wraps
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# -- Config -------------------------------------------------------------------
SECRET_TOKEN    = os.environ.get("SECRET_TOKEN", "")
TRADING_PAIR    = os.environ.get("TRADING_PAIR", "SOL/USD")
FREQTRADE_API   = os.environ.get("FREQTRADE_API", "http://freqtrade:8080/api/v1")
FREQTRADE_USER  = os.environ.get("FREQTRADE_USER", "admin")
FREQTRADE_PASS  = os.environ.get("FREQTRADE_PASS", "")
TG_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT         = os.environ.get("TELEGRAM_CHAT_ID", "")

STAKE_RATIO     = float(os.environ.get("STAKE_RATIO", "0.5"))   # % of free balance per buy
SELL_RATIO      = float(os.environ.get("SELL_RATIO",  "0.5"))   # % of position per sell
MIN_STAKE       = float(os.environ.get("MIN_STAKE",   "10.0"))  # minimum USD stake
API_RETRIES     = int(os.environ.get("API_RETRIES",   "3"))
API_RETRY_DELAY = float(os.environ.get("API_RETRY_DELAY", "5.0"))
API_TIMEOUT     = int(os.environ.get("API_TIMEOUT",   "15"))

# -- Logging ------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# -- Flask --------------------------------------------------------------------
app = Flask(__name__)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# -- Telegram -----------------------------------------------------------------
def telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Telegram failed: %s", e)

# -- Freqtrade API helpers ----------------------------------------------------
def _ft_request(method: str, endpoint: str, **kwargs) -> dict:
    """Make a Freqtrade API request with retries."""
    url = f"{FREQTRADE_API}/{endpoint.lstrip('/')}"
    auth = (FREQTRADE_USER, FREQTRADE_PASS)
    last_error = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.request(method, url, auth=auth, timeout=API_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = e
            logger.warning("Freqtrade API attempt %d/%d failed: %s", attempt, API_RETRIES, e)
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_DELAY)
    raise RuntimeError(f"Freqtrade API failed after {API_RETRIES} attempts: {last_error}")

def get_free_balance() -> float:
    """Return free stake currency balance available for trading."""
    data = _ft_request("GET", "/balance")
    return float(data.get("available_capital", data.get("total", 0)))

def get_open_trade(pair: str):
    """Return the open trade for a pair (most recent), or None."""
    data = _ft_request("GET", "/status")
    trades = [t for t in data if t["pair"] == pair and t["is_open"]]
    return sorted(trades, key=lambda t: t["open_date"])[-1] if trades else None

# -- Trade execution ----------------------------------------------------------
def execute_buy(pair: str) -> bool:
    """Buy using STAKE_RATIO of free balance."""
    try:
        free  = get_free_balance()
        stake = round(free * STAKE_RATIO, 2)

        if stake < MIN_STAKE:
            msg = (
                f"IADSS BUY skipped for {pair}\n"
                f"Free: ${free:.2f} -> stake ${stake:.2f} below ${MIN_STAKE:.0f} minimum"
            )
            logger.warning(msg)
            telegram(msg)
            return False

        logger.info("BUY %s: staking $%.2f (%.0f%% of $%.2f free)", pair, stake, STAKE_RATIO * 100, free)
        result    = _ft_request("POST", "/forcebuy", json={"pair": pair, "stake_amount": stake})
        trade_id  = result.get("trade_id") or result.get("id", "?")
        open_rate = result.get("open_rate", "?")

        telegram(
            f"IADSS BUY executed\n"
            f"Pair: {pair}\n"
            f"Stake: ${stake:.2f} ({int(STAKE_RATIO*100)}% of ${free:.2f} free)\n"
            f"Rate: {open_rate}\n"
            f"Trade ID: {trade_id}"
        )
        logger.info("BUY success: %s trade_id=%s stake=$%.2f", pair, trade_id, stake)
        return True

    except Exception as e:
        logger.error("BUY failed for %s: %s", pair, e)
        telegram(f"IADSS BUY FAILED: {pair} -- check logs\n{e}")
        return False

def execute_sell(pair: str) -> bool:
    """Sell SELL_RATIO of the current open position."""
    try:
        trade = get_open_trade(pair)

        if not trade:
            msg = f"IADSS SELL skipped for {pair} -- no open trade found"
            logger.warning(msg)
            telegram(msg)
            return False

        trade_id     = str(trade["trade_id"])
        total_amount = float(trade["amount"])
        sell_amount  = round(total_amount * SELL_RATIO, 8)
        current_rate = trade.get("current_rate", "?")
        profit_pct   = trade.get("current_profit_pct", 0) * 100

        logger.info("SELL %s: %.8f of %.8f trade_id=%s", pair, sell_amount, total_amount, trade_id)
        _ft_request("POST", "/forcesell", json={
            "tradeid":   trade_id,
            "ordertype": "market",
            "amount":    sell_amount,
        })

        telegram(
            f"IADSS SELL executed\n"
            f"Pair: {pair}\n"
            f"Sold: {sell_amount:.4f} ({int(SELL_RATIO*100)}% of {total_amount:.4f})\n"
            f"Rate: {current_rate} ({profit_pct:+.2f}%)\n"
            f"Trade ID: {trade_id}"
        )
        logger.info("SELL success: %s sold %.8f", pair, sell_amount)
        return True

    except Exception as e:
        logger.error("SELL failed for %s: %s", pair, e)
        telegram(f"IADSS SELL FAILED: {pair} -- check logs\n{e}")
        return False

# -- Auth decorator -----------------------------------------------------------
def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.args.get("token") or request.headers.get("X-Token")
        if SECRET_TOKEN and token != SECRET_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# -- Endpoints ----------------------------------------------------------------

@app.route("/confirm-buy", methods=["POST"])
@limiter.limit("30 per minute")
@require_token
def confirm_buy():
    """BUY Early Warning from Gregusm's indicator (MR + Confluence aligned)."""
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    logger.info("BUY early warning: %s", pair)
    telegram(
        f"IADSS BUY Early Warning\n"
        f"MR + Confluence aligned -- waiting for trend flip\n"
        f"Pair: {pair}"
    )
    return jsonify({"status": "ok", "message": "early_warning"}), 200

@app.route("/confirm-sell", methods=["POST"])
@limiter.limit("30 per minute")
@require_token
def confirm_sell():
    """SELL Early Warning from Gregusm's indicator (MR + Confluence aligned)."""
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    logger.info("SELL early warning: %s", pair)
    telegram(
        f"IADSS SELL Early Warning\n"
        f"MR + Confluence aligned -- waiting for trend flip\n"
        f"Pair: {pair}"
    )
    return jsonify({"status": "ok", "message": "early_warning"}), 200

@app.route("/lb-buy", methods=["POST"])
@limiter.limit("10 per minute")
@require_token
def lb_buy():
    """BUY Sequence Complete — all three steps confirmed, execute buy."""
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    logger.info("BUY sequence complete: %s", pair)
    telegram(f"BUY Sequence Complete -- firing trade for {pair}")
    success = execute_buy(pair)
    return jsonify({"status": "trade_executed" if success else "trade_failed"}), 200

@app.route("/lb-sell", methods=["POST"])
@limiter.limit("10 per minute")
@require_token
def lb_sell():
    """SELL Sequence Complete — all three steps confirmed, execute sell."""
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    logger.info("SELL sequence complete: %s", pair)
    telegram(f"SELL Sequence Complete -- firing trade for {pair}")
    success = execute_sell(pair)
    return jsonify({"status": "trade_executed" if success else "trade_failed"}), 200

# -- Status -------------------------------------------------------------------
@app.route("/status", methods=["GET"])
@limiter.limit("60 per minute")
@require_token
def status():
    pair = request.args.get("pair", TRADING_PAIR)
    try:
        trade = get_open_trade(pair)
        free  = get_free_balance()
        trade_info = None
        if trade:
            trade_info = {
                "trade_id":   trade["trade_id"],
                "amount":     trade["amount"],
                "open_rate":  trade["open_rate"],
                "current_rate": trade.get("current_rate"),
                "profit_pct": round(trade.get("current_profit_pct", 0) * 100, 2),
                "open_date":  trade["open_date"],
            }
        return jsonify({
            "pair":            pair,
            "open_trade":      trade_info,
            "free_balance":    round(free, 2),
            "next_buy_stake":  round(free * STAKE_RATIO, 2),
            "status":          "ok",
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
