#!/usr/bin/env python3
"""IADSS Signal Tracker — Webhook receiver for the IADSS Confluence Monitor.

Designed to work with the IADSS Confluence Monitor indicator by Gregusm:
https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/

The indicator handles all signal sequencing (MR -> Confluence -> Trend flip)
internally on the chart. This server receives the completion alerts and
executes trades via the Freqtrade API.

Endpoints:
  POST /lb-buy          BUY Sequence Complete  -> executes buy
  POST /lb-sell         SELL Sequence Complete -> executes sell
  POST /confirm-buy     BUY Early Warning
  POST /confirm-sell    SELL Early Warning
  POST /deposit         Add cash to a pair ledger
  POST /withdraw        Remove cash from a pair ledger
  POST /position/add    Mark existing coins as already in a position
  POST /position/remove Stop tracking some coins as a managed position
  GET  /status          current open trade + ledger info
  GET  /ledger          all pair bankrolls, P&L summary
  GET  /health          health check

Per-pair ledger (optional):
  Set ALLOCATION_SOL_USD=1000 in .env to give SOL/USD its own $1000 bankroll.
  Buys stake against that pair's ledger, not the total exchange balance.
  Profits and losses stay isolated per pair.

Per-pair ratios (optional):
  STAKE_RATIO_HYPE_USD=0.33
  SELL_RATIO_HYPE_USD=0.33
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

PAIR_RE = re.compile(r'^[A-Z0-9]+/[A-Z0-9]+$')

def valid_pair(pair: str) -> bool:
    return bool(PAIR_RE.match(pair))

SECRET_TOKEN    = os.environ.get("SECRET_TOKEN", "")
TRADING_PAIR    = os.environ.get("TRADING_PAIR", "SOL/USD")
FREQTRADE_API   = os.environ.get("FREQTRADE_API", "http://freqtrade:8080/api/v1")
FREQTRADE_USER  = os.environ.get("FREQTRADE_USER", "admin")
FREQTRADE_PASS  = os.environ.get("FREQTRADE_PASS", "")
TG_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT         = os.environ.get("TELEGRAM_CHAT_ID", "")

STAKE_RATIO     = float(os.environ.get("STAKE_RATIO", "0.5"))
SELL_RATIO      = float(os.environ.get("SELL_RATIO", "0.5"))
MIN_STAKE       = float(os.environ.get("MIN_STAKE", "10.0"))

def get_stake_ratio(pair: str) -> float:
    key = "STAKE_RATIO_" + pair.replace("/", "_")
    return float(os.environ.get(key, STAKE_RATIO))

def get_sell_ratio(pair: str) -> float:
    key = "SELL_RATIO_" + pair.replace("/", "_")
    return float(os.environ.get(key, SELL_RATIO))

API_RETRIES     = int(os.environ.get("API_RETRIES", "3"))
API_RETRY_DELAY = float(os.environ.get("API_RETRY_DELAY", "5.0"))
API_TIMEOUT     = int(os.environ.get("API_TIMEOUT", "15"))
LEDGER_FILE     = os.environ.get("LEDGER_FILE", "/data/ledger.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

if not SECRET_TOKEN:
    logger.warning("SECRET_TOKEN is not set — endpoints are UNAUTHENTICATED")

app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

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
            env_key = _pair_to_env_key(pair)
            alloc_str = os.environ.get(env_key)
            if not alloc_str:
                return None
            allocation = float(alloc_str)
            ledger[pair] = {
                "allocated": allocation,
                "current": allocation,
                "in_trade": 0.0,
                "created": datetime.now(timezone.utc).isoformat(),
                "updated": datetime.now(timezone.utc).isoformat(),
            }
            _save_ledger(ledger)
            logger.info("Ledger initialised: %s = $%.2f", pair, allocation)
        return dict(ledger[pair])

def _deduct_stake(pair: str, stake: float):
    with _ledger_lock:
        ledger = _load_ledger()
        if pair in ledger:
            ledger[pair]["current"] -= stake
            ledger[pair]["in_trade"] += stake
            ledger[pair]["updated"] = datetime.now(timezone.utc).isoformat()
            _save_ledger(ledger)
            logger.info("Ledger deducted: %s -$%.2f  current=$%.2f  in_trade=$%.2f",
                pair, stake, ledger[pair]["current"], ledger[pair]["in_trade"])

def _credit_sell(pair: str, sell_amount: float, open_rate: float, sell_rate: float) -> float:
    cost_basis = sell_amount * open_rate
    proceeds = sell_amount * sell_rate
    profit = proceeds - cost_basis
    with _ledger_lock:
        ledger = _load_ledger()
        if pair in ledger:
            ledger[pair]["current"] += proceeds
            ledger[pair]["in_trade"] -= cost_basis
            ledger[pair]["in_trade"] = max(0.0, ledger[pair]["in_trade"])
            ledger[pair]["updated"] = datetime.now(timezone.utc).isoformat()
            _save_ledger(ledger)
            logger.info("Ledger credited: %s sold=%.4f profit=$%+.2f  current=$%.2f in_trade=$%.2f",
                pair, sell_amount, profit, ledger[pair]["current"], ledger[pair]["in_trade"])
    return profit

def deposit(pair: str, amount: float) -> dict:
    if amount <= 0:
        raise ValueError("amount must be positive")
    with _ledger_lock:
        ledger = _load_ledger()
        if pair not in ledger:
            env_key = _pair_to_env_key(pair)
            alloc_str = os.environ.get(env_key)
            if alloc_str:
                base = float(alloc_str)
                ledger[pair] = {
                    "allocated": base + amount,
                    "current": base + amount,
                    "in_trade": 0.0,
                    "created": datetime.now(timezone.utc).isoformat(),
                    "updated": datetime.now(timezone.utc).isoformat(),
                }
            else:
                ledger[pair] = {
                    "allocated": amount,
                    "current": amount,
                    "in_trade": 0.0,
                    "created": datetime.now(timezone.utc).isoformat(),
                    "updated": datetime.now(timezone.utc).isoformat(),
                }
            _save_ledger(ledger)
            logger.info("Ledger created via deposit: %s = $%.2f", pair, ledger[pair]["allocated"])
            return dict(ledger[pair])
        ledger[pair]["allocated"] += amount
        ledger[pair]["current"] += amount
        ledger[pair]["updated"] = datetime.now(timezone.utc).isoformat()
        _save_ledger(ledger)
        logger.info("Deposit %s +$%.2f → allocated=$%.2f current=$%.2f",
                    pair, amount, ledger[pair]["allocated"], ledger[pair]["current"])
        return dict(ledger[pair])

def withdraw(pair: str, amount: float) -> dict:
    if amount <= 0:
        raise ValueError("amount must be positive")
    with _ledger_lock:
        ledger = _load_ledger()
        if pair not in ledger:
            raise ValueError(f"no ledger for {pair}")
        if ledger[pair]["current"] < amount:
            raise ValueError(f"insufficient free balance (have ${ledger[pair]['current']:.2f})")
        ledger[pair]["allocated"] = max(0.0, ledger[pair]["allocated"] - amount)
        ledger[pair]["current"] -= amount
        ledger[pair]["updated"] = datetime.now(timezone.utc).isoformat()
        _save_ledger(ledger)
        logger.info("Withdraw %s -$%.2f → allocated=$%.2f current=$%.2f",
                    pair, amount, ledger[pair]["allocated"], ledger[pair]["current"])
        return dict(ledger[pair])

def position_add(pair: str, coin_amount: float, cost_usd: float) -> dict:
    """Mark existing coins as already in a position."""
    if coin_amount <= 0:
        raise ValueError("amount must be positive")
    if cost_usd is None or cost_usd <= 0:
        raise ValueError("cost_usd is required (approximate USD value of the coins)")
    with _ledger_lock:
        ledger = _load_ledger()
        if pair not in ledger:
            ledger[pair] = {
                "allocated": cost_usd,
                "current": 0.0,
                "in_trade": cost_usd,
                "created": datetime.now(timezone.utc).isoformat(),
                "updated": datetime.now(timezone.utc).isoformat(),
            }
        else:
            available = ledger[pair]["current"]
            deduct = min(available, cost_usd)
            ledger[pair]["current"] -= deduct
            ledger[pair]["in_trade"] += cost_usd
            if deduct < cost_usd:
                ledger[pair]["allocated"] += (cost_usd - deduct)
            ledger[pair]["updated"] = datetime.now(timezone.utc).isoformat()
        _save_ledger(ledger)
        logger.info("Position add %s: +%.6f coins (~$%.2f) → current=$%.2f in_trade=$%.2f",
                    pair, coin_amount, cost_usd, ledger[pair]["current"], ledger[pair]["in_trade"])
        return dict(ledger[pair])

def position_remove(pair: str, coin_amount: float, cost_usd: float) -> dict:
    """Stop tracking some coins as a managed position."""
    if coin_amount <= 0:
        raise ValueError("amount must be positive")
    if cost_usd is None or cost_usd <= 0:
        raise ValueError("cost_usd is required")
    with _ledger_lock:
        ledger = _load_ledger()
        if pair not in ledger:
            raise ValueError(f"no ledger for {pair}")
        if ledger[pair]["in_trade"] < cost_usd * 0.99:
            raise ValueError(f"in_trade only has ${ledger[pair]['in_trade']:.2f}")
        ledger[pair]["in_trade"] -= cost_usd
        ledger[pair]["current"] += cost_usd
        ledger[pair]["updated"] = datetime.now(timezone.utc).isoformat()
        _save_ledger(ledger)
        logger.info("Position remove %s: -%.6f coins (~$%.2f) → current=$%.2f in_trade=$%.2f",
                    pair, coin_amount, cost_usd, ledger[pair]["current"], ledger[pair]["in_trade"])
        return dict(ledger[pair])

def telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg}, timeout=5)
    except Exception as e:
        logger.warning("Telegram failed: %s", e)

def _ft_request(method: str, endpoint: str, **kwargs) -> dict:
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
    data = _ft_request("GET", "/balance")
    return float(data.get("available_capital", data.get("total", 0)))

def get_open_trade(pair: str):
    data = _ft_request("GET", "/status")
    trades = [t for t in data if t["pair"] == pair and t["is_open"]]
    return sorted(trades, key=lambda t: t["open_date"])[-1] if trades else None

def execute_buy(pair: str) -> bool:
    try:
        entry = get_pair_entry(pair)
        if entry is not None:
            available = entry["current"]
            mode = f"ledger ${available:.2f}"
        else:
            available = get_free_balance()
            mode = f"free balance ${available:.2f}"
        ratio = get_stake_ratio(pair)
        stake = round(available * ratio, 2)
        if stake < MIN_STAKE:
            msg = (f"IADSS BUY skipped — {pair}\nAvailable ({mode}) -> stake ${stake:.2f} below ${MIN_STAKE:.0f} minimum")
            logger.warning(msg)
            telegram(msg)
            return False
        logger.info("BUY %s: $%.2f stake (%.0f%% of %s)", pair, stake, ratio * 100, mode)
        result = _ft_request("POST", "/forcebuy", json={"pair": pair, "stake_amount": stake})
        trade_id = result.get("trade_id") or result.get("id", "?")
        open_rate = result.get("open_rate", "?")
        if entry is not None:
            _deduct_stake(pair, stake)
            updated = get_pair_entry(pair)
            ledger_summary = (f"\nLedger: ${updated['current']:.2f} liquid  ${updated['in_trade']:.2f} in trade")
        else:
            ledger_summary = ""
        telegram(f"IADSS BUY executed\nPair: {pair}\nStake: ${stake:.2f} ({int(ratio*100)}% of {mode})\nRate: {open_rate}\nTrade ID: {trade_id}{ledger_summary}")
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
        trade_id = str(trade["trade_id"])
        total_amount = float(trade["amount"])
        sell_ratio = get_sell_ratio(pair)
        sell_amount = round(total_amount * sell_ratio, 8)
        open_rate = float(trade.get("open_rate", 0))
        current_rate = float(trade.get("current_rate", 0)) if trade.get("current_rate") else 0.0
        profit_pct = trade.get("current_profit_pct", 0) * 100
        logger.info("SELL %s: %.8f of %.8f trade_id=%s", pair, sell_amount, total_amount, trade_id)
        _ft_request("POST", "/forcesell", json={"tradeid": trade_id, "ordertype": "market", "amount": sell_amount})
        entry = get_pair_entry(pair)
        if entry is not None and open_rate > 0 and current_rate > 0:
            profit_abs = _credit_sell(pair, sell_amount, open_rate, current_rate)
            updated = get_pair_entry(pair)
            total_pot = updated["current"] + updated["in_trade"]
            pnl = total_pot - updated["allocated"]
            ledger_summary = (f"\nLedger: ${updated['current']:.2f} liquid  ${updated['in_trade']:.2f} in trade"
                              f"\nPot: ${total_pot:.2f}  (P&L: ${pnl:+.2f} vs ${updated['allocated']:.0f} start)")
        else:
            profit_abs = 0.0
            ledger_summary = ""
        telegram(f"IADSS SELL executed\nPair: {pair}\nSold: {sell_amount:.4f} ({int(sell_ratio*100)}% of {total_amount:.4f})\n"
                 f"Rate: {current_rate:.4f} ({profit_pct:+.2f}%)\nThis sell: ${profit_abs:+.2f}\nTrade ID: {trade_id}{ledger_summary}")
        logger.info("SELL success: %s sold=%.8f profit=$%+.2f", pair, sell_amount, profit_abs)
        return True
    except Exception as e:
        logger.error("SELL failed for %s: %s", pair, e)
        telegram(f"IADSS SELL FAILED: {pair} -- check logs\n{e}")
        return False

def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        data = request.get_json(silent=True) or {}
        token = (request.args.get("token") or request.headers.get("X-Token")
                 or request.headers.get("X-Webhook-Secret") or data.get("token") or "")
        if SECRET_TOKEN and token != SECRET_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

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
        free = get_free_balance()
        entry = get_pair_entry(pair)
        trade_info = None
        if trade:
            trade_info = {
                "trade_id": trade["trade_id"], "amount": trade["amount"],
                "open_rate": trade["open_rate"], "current_rate": trade.get("current_rate"),
                "profit_pct": round(trade.get("current_profit_pct", 0) * 100, 2),
                "open_date": trade["open_date"],
            }
        ledger_info = None
        if entry:
            total = entry["current"] + entry["in_trade"]
            ledger_info = {
                "allocated": entry["allocated"], "current": round(entry["current"], 2),
                "in_trade": round(entry["in_trade"], 2), "total": round(total, 2),
                "pnl": round(total - entry["allocated"], 2),
                "next_stake": round(entry["current"] * get_stake_ratio(pair), 2),
            }
        return jsonify({
            "pair": pair, "open_trade": trade_info, "free_balance": round(free, 2),
            "next_buy_stake": round((entry["current"] if entry else free) * get_stake_ratio(pair), 2),
            "ledger": ledger_info, "status": "ok",
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
        pnl = total - entry["allocated"]
        result[pair] = {
            "allocated": round(entry["allocated"], 2), "current": round(entry["current"], 2),
            "in_trade": round(entry["in_trade"], 2), "total": round(total, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / entry["allocated"] * 100, 2) if entry["allocated"] else 0,
            "next_stake": round(entry["current"] * get_stake_ratio(pair), 2),
            "updated": entry.get("updated"),
        }
        grand_allocated += entry["allocated"]
        grand_total += total
    return jsonify({
        "pairs": result,
        "summary": {
            "total_allocated": round(grand_allocated, 2),
            "total_value": round(grand_total, 2),
            "total_pnl": round(grand_total - grand_allocated, 2),
        },
        "status": "ok",
    })

@app.route("/deposit", methods=["POST"])
@limiter.limit("20 per minute")
@require_token
def deposit_endpoint():
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    amount = data.get("amount")
    if not valid_pair(pair):
        return jsonify({"error": "invalid pair"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    try:
        entry = deposit(pair, amount)
        telegram(f"IADSS DEPOSIT\nPair: {pair}\nAmount: +${amount:.2f}\nAllocated: ${entry['allocated']:.2f}\nCurrent: ${entry['current']:.2f}")
        return jsonify({"status": "ok", "pair": pair, "deposited": amount,
                        "allocated": round(entry["allocated"], 2), "current": round(entry["current"], 2),
                        "in_trade": round(entry["in_trade"], 2)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Deposit failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/withdraw", methods=["POST"])
@limiter.limit("20 per minute")
@require_token
def withdraw_endpoint():
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    amount = data.get("amount")
    if not valid_pair(pair):
        return jsonify({"error": "invalid pair"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    try:
        entry = withdraw(pair, amount)
        telegram(f"IADSS WITHDRAW\nPair: {pair}\nAmount: -${amount:.2f}\nAllocated: ${entry['allocated']:.2f}\nCurrent: ${entry['current']:.2f}")
        return jsonify({"status": "ok", "pair": pair, "withdrawn": amount,
                        "allocated": round(entry["allocated"], 2), "current": round(entry["current"], 2),
                        "in_trade": round(entry["in_trade"], 2)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Withdraw failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/position/add", methods=["POST"])
@limiter.limit("20 per minute")
@require_token
def position_add_endpoint():
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    amount = data.get("amount")
    cost_usd = data.get("cost_usd")
    if not valid_pair(pair):
        return jsonify({"error": "invalid pair"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "amount (coins) must be a number"}), 400
    if cost_usd is None:
        return jsonify({"error": "cost_usd is required (USD value of the coins)"}), 400
    try:
        cost_usd = float(cost_usd)
    except (TypeError, ValueError):
        return jsonify({"error": "cost_usd must be a number"}), 400
    try:
        entry = position_add(pair, amount, cost_usd)
        telegram(f"IADSS POSITION ADD\nPair: {pair}\nCoins: {amount}\nIn trade: ${entry['in_trade']:.2f}\nFree: ${entry['current']:.2f}")
        return jsonify({"status": "ok", "pair": pair, "coins_added": amount,
                        "allocated": round(entry["allocated"], 2), "current": round(entry["current"], 2),
                        "in_trade": round(entry["in_trade"], 2)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Position add failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/position/remove", methods=["POST"])
@limiter.limit("20 per minute")
@require_token
def position_remove_endpoint():
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)
    amount = data.get("amount")
    cost_usd = data.get("cost_usd")
    if not valid_pair(pair):
        return jsonify({"error": "invalid pair"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "amount (coins) must be a number"}), 400
    if cost_usd is None:
        return jsonify({"error": "cost_usd is required (USD value of the coins)"}), 400
    try:
        cost_usd = float(cost_usd)
    except (TypeError, ValueError):
        return jsonify({"error": "cost_usd must be a number"}), 400
    try:
        entry = position_remove(pair, amount, cost_usd)
        telegram(f"IADSS POSITION REMOVE\nPair: {pair}\nCoins: {amount}\nFreed: ${cost_usd:.2f}\nFree: ${entry['current']:.2f}")
        return jsonify({"status": "ok", "pair": pair, "coins_removed": amount,
                        "allocated": round(entry["allocated"], 2), "current": round(entry["current"], 2),
                        "in_trade": round(entry["in_trade"], 2)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Position remove failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
