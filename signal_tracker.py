#!/usr/bin/env python3
"""IADSS Signal Tracker — Webhook receiver for the IADSS Confluence Monitor.

Designed to work with the IADSS Confluence Monitor indicator by Gregusm:
https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/

The indicator handles all signal sequencing (MR -> Confluence -> Trend flip)
internally on the chart. This server receives the completion alerts and
executes trades via the Freqtrade API.

Endpoints:
  POST /lb-buy        BUY Sequence Complete  -> executes buy  (STAKE_RATIO of pair ledger)
  POST /lb-sell       SELL Sequence Complete -> executes sell (SELL_RATIO of open position)
  POST /confirm-buy   BUY Early Warning      -> Telegram notification only
  POST /confirm-sell  SELL Early Warning     -> Telegram notification only
  GET  /status        current open trade + ledger info
  GET  /ledger        all pair bankrolls, P&L summary
  GET  /health        health check

Per-pair ledger (optional):
  Set ALLOCATION_SOL_USD=1000 in .env to give SOL/USD its own $1000 bankroll.
  Buys stake against that pair's ledger, not the total exchange balance.
  Profits and losses stay isolated per pair — SOL gains never fund HYPE trades.
  Omit the env var to fall back to free-balance mode (original behaviour).
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# -- Pair validation ----------------------------------------------------------
PAIR_RE = re.compile(r'^[A-Z0-9]+/[A-Z0-9]+$')

def valid_pair(pair: str) -> bool:
    return bool(PAIR_RE.match(pair))

# -- Config -------------------------------------------------------------------
SECRET_TOKEN    = os.environ.get("SECRET_TOKEN", "")
TRADING_PAIR    = os.environ.get("TRADING_PAIR", "SOL/USD")
FREQTRADE_API   = os.environ.get("FREQTRADE_API", "http://freqtrade:8080/api/v1")
FREQTRADE_USER  = os.environ.get("FREQTRADE_USER", "admin")
FREQTRADE_PASS  = os.environ.get("FREQTRADE_PASS", "")
TG_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT         = os.environ.get("TELEGRAM_CHAT_ID", "")

STAKE_RATIO     = float(os.environ.get("STAKE_RATIO",      "0.5"))
SELL_RATIO      = float(os.environ.get("SELL_RATIO",       "0.5"))
MIN_STAKE       = float(os.environ.get("MIN_STAKE",        "10.0"))
API_RETRIES     = int(os.environ.get("API_RETRIES",        "3"))
API_RETRY_DELAY = float(os.environ.get("API_RETRY_DELAY",  "5.0"))
API_TIMEOUT     = int(os.environ.get("API_TIMEOUT",        "15"))
LEDGER_FILE     = os.environ.get("LEDGER_FILE",            "/data/ledger.json")

# -- Logging ------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

if not SECRET_TOKEN:
    logger.warning("SECRET_TOKEN is not set — endpoints are UNAUTHENTICATED")

# -- Flask --------------------------------------------------------------------
app = Flask(__name__)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# -- Ledger -------------------------------------------------------------------
_ledger_lock = threading.Lock()

def _pair_to_env_key(pair: str) -> str:
    return "ALLOCATION_" + pair.replace("/", "_")

def _load_ledger() -> dict:
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to load ledger: %s", e)
    return {}

def _save_ledger(ledger: dict):
    dirpath = os.path.dirname(LEDGER_FILE) or "."
    os.makedirs(dirpath, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(ledger, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LEDGER_FILE)
    except Exception:
        os.unlink(tmp)
        raise


def get_pair_entry(pair: str) -> dict | None:
    with _ledger_lock:
        ledger = _load_ledger()
        if pair not in ledger:
            env_key   = _pair_to_env_key(pair)
            alloc_str = os.environ.get(env_key)
            if not alloc_str:
                return None
            allocation = float(alloc_str)
            ledger[pair] = {
                "allocated": allocation,
                "current":   allocation,
                "in_trade":  0.0,
                "created":   datetime.now(timezone.utc).isoformat(),
                "updated":   datetime.now(timezone.utc).isoformat(),
            }
            _save_ledger(ledger)
            logger.info("Ledger initialised: %s = $%.2f", pair, allocation)
        return dict(ledger[pair])

def _deduct_stake(pair: str, stake: float):
    with _ledger_lock:
        ledger = _load_ledger()
        if pair in ledger:
            ledger[pair]["current"]  -= stake
            ledger[pair]["in_trade"] += stake
            ledger[pair]["updated"]   = datetime.now(timezone.utc).isoformat(),
            _save_ledger(ledger)
            logger.info("Ledger deducted: %s -$%.2f  current=$%.2f  in_trade=$%.2f",
                pair, stake, ledger[pair]["current"], ledger[pair]["in_trade"])

def _credit_sell(pair: str, sell_amount: float, open_rate: float, sell_rate: float) -> float:
    cost_basis = sell_amount * open_rate
    proceeds   = sell_amount * sell_rate
    profit     = proceeds - cost_basis
    with _ledger_lock:
        ledger = _load_ledger()
        if pair in ledger:
            ledger[pair]["current"]  += proceeds
            ledger[pair]["in_trade"] -= cost_basis
            ledger[pair]["in_trade"]  = max(0.0, ledger[pair]["in_trade"])
            ledger[pair]["updated"]   = datetime.now(timezone.utc).isoformat()

            _save_ledger(ledger)
            logger.info("Ledger credited: %s sold=%.4f profit=$%+.2f  current=$%.2f in_trade=$%.2f",
                pair, sell_amount, profit, ledger[pair]["current"], ledger[pair]["in_trade"])
    return profit

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
    url  = f"{FREQTRADE_API}/{endpoint.lstrip('/')}"
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
    data = _ft_request("GET", "/balance")
    return float(data.get("available_capital", data.get("total", 0)))

def get_open_trade(pair: str):
    data   = _ft_request("GET", "/status")
    trades = [t for t in data if t["pair"] == pair and t["is_open"]]
    return sorted(trades, key=lambda t: t["open_date"])[-1] if trades else None

# -- Trade execution ----------------------------------------------------------
def execute_buy(pair: str) -> bool:
    try:
        entry = get_pair_entry(pair)
        if entry is not None:
            available = entry["current"]
            mode      = f"ledger ${available:.2f}"
        else:
            available = get_free_balance()
            mode      = f"free balance ${available:.2f}"

        stake = round(available * STAKE_RATIO, 2)

        if stake < MIN_STAKE:
            msg = (f"IADSS BUY skipped — {pair}\n"
                   f"Available ({mode}) -> stake ${stake:.2f} below ${MIN_STAKE:.0f} minimum")
            logger.warning(msg)
            telegram(msg)
            return False

        logger.info("BUY %s: $%.2f stake (%.0f%% of %s)", pair, stake, STAKE_RATIO * 100, mode)
        result    = _ft_request("POST", "/forcebuy", json={"pair": pair, "stake_amount": stake})
        trade_id  = result.get("trade_id") or result.get("id", "?")
        open_rate = result.get("open_rate", "?")

        if entry is not None:
            _deduct_stake(pair, stake)
            updated        = get_pair_entry(pair)
            ledger_summary = (f"\nLedger: ${updated['current']:.2f} liquid  "
                              f"${updated['in_trade']:.2f} in trade")
        else:
            ledger_summary = ""

        telegram(f"IADSS BUY executed\nPair: {pair}\n"
                 f"Stake: ${stake:.2f} ({int(STAKE_RATIO*100)}% of {mode})\n"
                 f"Rate: {open_rate}\nTrade ID: {trade_id}{ledger_summary}")
        logger.info("BUY success: %s trade_id=%s stake=$%.2f", pair, trade_id, stake)
        return True

    except Exception as e:
        logger.error("BUY failed for %s: %s", pair, e)
        telegram(f"IADSS BUY FAILED: {pair} -- check logs\n{e}")
        return False

def execute_sell(pair: str) -> bool:
    try:
        trade = get_open_trade(pair)
        if not trade:
            msg = f"IADSS SELL skipped -- {pair} -- no open trade found"
            logger.warning(msg)
            telegram(msg)
            return False

        trade_id     = str(trade["trade_id"])
        total_amount = float(trade["amount"])
        sell_amount  = round(total_amount * SELL_RATIO, 8)
        open_rate    = float(trade.get("open_rate", 0))
        current_rate = float(trade.get("current_rate", 0)) if trade.get("current_rate") else 0.0
        profit_pct   = trade.get("current_profit_pct", 0) * 100

        logger.info("SELL %s: %.8f of %.8f trade_id=%s", pair, sell_amount, total_amount, trade_id)
        _ft_request("POST", "/forcesell", json={
            "tradeid": trade_id, "ordertype": "market", "amount": sell_amount})

        entry = get_pair_entry(pair)
        if entry is not None and open_rate > 0 and current_rate > 0:
            profit_abs = _credit_sell(pair, sell_amount, open_rate, current_rate)
            updated    = get_pair_entry(pair)
            total_pot  = updated["current"] + updated["in_trade"]
            pnl        = total_pot - updated["allocated"]
            ledger_summary = (f"\nLedger: ${updated['current']:.2f} liquid  "
                              f"${updated['in_trade']:.2f} in trade"
                              f"\nPot: ${total_pot:.2f}  (P&L: ${pnl:+.2f} vs ${updated['allocated']:.0f} start)")
        else:
            profit_abs     = 0.0
            ledger_summary = ""

        telegram(f"IADSS SELL executed\nPair: {pair}\n"
                 f"Sold: {sell_amount:.4f} ({int(SELL_RATIO*100)}% of {total_amount:.4f})\n"
                 f"Rate: {current_rate:.4f} ({profit_pct:+.2f}%)\n"
                 f"This sell: ${profit_abs:+.2f}\nTrade ID: {trade_id}{ledger_summary}")
        logger.info("SELL success: %s sold=%.8f profit=$%+.2f", pair, sell_amount, profit_abs)
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
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    if not valid_pair(pair):
        return jsonify({"error": "invalid pair"}), 400
    logger.info("BUY early warning: %s", pair)
    telegram(f"IADSS BUY Early Warning\nMR + Confluence aligned -- waiting for trend flip\nPair: {pair}")
    return jsonify({"status": "ok", "message": "early_warning"}), 200

@app.route("/confirm-sell", methods=["POST"])
@limiter.limit("30 per minute")
@require_token
def confirm_sell():
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    if not valid_pair(pair):
        return jsonify({"error": "invalid pair"}), 400
    logger.info("SELL early warning: %s", pair)
    telegram(f"IADSS SELL Early Warning\nMR + Confluence aligned -- waiting for trend flip\nPair: {pair}")
    return jsonify({"status": "ok", "message": "early_warning"}), 200

@app.route("/lb-buy", methods=["POST"])
@limiter.limit("10 per minute")
@require_token
def lb_buy():
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    if not valid_pair(pair):
        return jsonify({"error": "invalid pair"}), 400
    logger.info("BUY sequence complete: %s", pair)
    telegram(f"BUY Sequence Complete -- firing trade for {pair}")
    success = execute_buy(pair)
    return jsonify({"status": "trade_executed" if success else "trade_failed"}), 200

@app.route("/lb-sell", methods=["POST"])
@limiter.limit("10 per minute")
@require_token
def lb_sell():
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    if not valid_pair(pair):
        return jsonify({"error": "invalid pair"}), 400
    logger.info("SELL sequence complete: %s", pair)
    telegram(f"SELL Sequence Complete -- firing trade for {pair}")
    success = execute_sell(pair)
    return jsonify({"status": "trade_executed" if success else "trade_failed"}), 200

@app.route("/status", methods=["GET"])
@limiter.limit("60 per minute")
@require_token
def status():
    pair = request.args.get("pair", TRADING_PAIR)
    try:
        trade = get_open_trade(pair)
        free  = get_free_balance()
        entry = get_pair_entry(pair)
        trade_info = None
        if trade:
            trade_info = {
                "trade_id":     trade["trade_id"],
                "amount":       trade["amount"],
                "open_rate":    trade["open_rate"],
                "current_rate": trade.get("current_rate"),
                "profit_pct":   round(trade.get("current_profit_pct", 0) * 100, 2),
                "open_date":    trade["open_date"],
            }
        ledger_info = None
        if entry:
            total = entry["current"] + entry["in_trade"]
            ledger_info = {
                "allocated":  entry["allocated"],
                "current":    round(entry["current"],  2),
                "in_trade":   round(entry["in_trade"], 2),
                "total":      round(total, 2),
                "pnl":        round(total - entry["allocated"], 2),
                "next_stake": round(entry["current"] * STAKE_RATIO, 2),
            }
        return jsonify({
            "pair":           pair,
            "open_trade":     trade_info,
            "free_balance":   round(free, 2),
            "next_buy_stake": round((entry["current"] if entry else free) * STAKE_RATIO, 2),
            "ledger":         ledger_info,
            "status":         "ok",
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/ledger", methods=["GET"])
@limiter.limit("60 per minute")
@require_token
def ledger_view():
    with _ledger_lock:
        data = _load_ledger()
    result = {}
    grand_allocated = grand_total = 0.0
    for pair, entry in data.items():
        total = entry["current"] + entry["in_trade"]
        pnl   = total - entry["allocated"]
        result[pair] = {
            "allocated":  round(entry["allocated"],  2),
            "current":    round(entry["current"],    2),
            "in_trade":   round(entry["in_trade"],   2),
            "total":      round(total,               2),
            "pnl":        round(pnl,                 2),
            "pnl_pct":    round(pnl / entry["allocated"] * 100, 2) if entry["allocated"] else 0,
            "next_stake": round(entry["current"] * STAKE_RATIO, 2),
            "updated":    entry.get("updated"),
        }
        grand_allocated += entry["allocated"]
        grand_total     += total
    return jsonify({
        "pairs": result,
        "summary": {
            "total_allocated": round(grand_allocated, 2),
            "total_value":     round(grand_total,     2),
            "total_pnl":       round(grand_total - grand_allocated, 2),
        },
        "status": "ok",
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
