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

Sizing:
  Buys use STAKE_RATIO (default 50%) of the live free stake currency.
  Sells use SELL_RATIO (default 50%) of the total base-coin balance of that pair.
  A resting stop-loss locks coins, so the sell uses total balance, not free.
  Freqtrade can only sell coins that belong to an open trade.
  The per-pair ledger is bookkeeping only and is not used to size orders.

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
STAKE_CURRENCY  = os.environ.get("STAKE_CURRENCY", "USD").upper()

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

def _currencies(data: dict) -> list:
    rows = data.get("currencies") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []

def _currency_from_balance(data: dict, currency: str, field: str = "free"):
    """Return the live amount, or None if that currency is not in the payload."""
    want = currency.upper()
    aliases = {want}
    if want == "USD":
        aliases.add("ZUSD")
    found = False
    total = 0.0
    for row in _currencies(data):
        code = str(row.get("currency") or "").upper()
        if code not in aliases or row.get("is_position"):
            continue
        found = True
        try:
            total += float(row.get(field) or 0)
        except (TypeError, ValueError):
            continue
    return total if found else None

def get_currency_amount(currency: str, field: str = "free") -> float:
    """Live exchange amount for one currency from Freqtrade /balance."""
    data = _ft_request("GET", "/balance")
    amount = _currency_from_balance(data, currency, field)
    return 0.0 if amount is None else amount

def get_live_stake_free() -> float:
    """Free stake-currency cash on the exchange. Ignores the ledger."""
    data = _ft_request("GET", "/balance")
    if not isinstance(data, dict):
        return 0.0
    stake = str(data.get("stake") or STAKE_CURRENCY).upper()
    free = _currency_from_balance(data, stake, "free")
    if free is not None:
        return free
    try:
        return float(data.get("available_capital") or 0)
    except (TypeError, ValueError):
        return 0.0

def get_free_balance() -> float:
    return get_live_stake_free()

def get_open_trades(pair: str) -> list:
    data = _ft_request("GET", "/status")
    if not isinstance(data, list):
        return []
    trades = [t for t in data if t.get("pair") == pair and t.get("is_open")]
    return sorted(trades, key=lambda t: t.get("open_date") or "")

def get_open_trade(pair: str):
    trades = get_open_trades(pair)
    return trades[-1] if trades else None

def execute_buy(pair: str) -> bool:
    try:
        available = get_live_stake_free()
        mode = f"live {STAKE_CURRENCY} ${available:.2f}"
        ratio = get_stake_ratio(pair)
        stake = round(available * ratio, 2)
        if stake < MIN_STAKE:
            msg = (f"IADSS BUY skipped — {pair}\nAvailable ({mode}) -> stake ${stake:.2f} below ${MIN_STAKE:.0f} minimum")
            logger.warning(msg)
            telegram(msg)
            return False
        logger.info("BUY %s: $%.2f stake (%.0f%% of %s)", pair, stake, ratio * 100, mode)
        result = _ft_request("POST", "/forcebuy", json={"pair": pair, "stake_amount": stake, "stakeamount": stake})
        trade_id = result.get("trade_id") or result.get("id", "?")
        open_rate = result.get("open_rate", "?")
        telegram(f"IADSS BUY executed\nPair: {pair}\nStake: ${stake:.2f} ({int(ratio*100)}% of {mode})\nRate: {open_rate}\nTrade ID: {trade_id}")
        logger.info("BUY success: %s trade_id=%s stake=$%.2f", pair, trade_id, stake)
        return True
    except Exception as e:
        logger.error("BUY failed for %s: %s", pair, e)
        telegram(f"IADSS BUY FAILED: {pair} -- check logs\n{e}")
        return False

def execute_sell(pair: str) -> bool:
    try:
        base = pair.split("/", 1)[0]
        # Total balance, not free. A stop on the exchange locks the coins,
        # so "free" is often 0 while the trade is still open.
        held = get_currency_amount(base, "balance")
        if held <= 0:
            held = get_currency_amount(base, "free")
        sell_ratio = get_sell_ratio(pair)
        target = round(held * sell_ratio, 8)
        if target <= 0:
            msg = f"IADSS SELL skipped -- {pair} -- exchange holds 0 {base}"
            logger.warning(msg)
            telegram(msg)
            return False
        # Newest trade first. Freqtrade can only exit coins it has a trade for.
        trades = list(reversed(get_open_trades(pair)))
        if not trades:
            msg = (f"IADSS SELL skipped -- {pair} -- exchange holds {held:.8f} {base} "
                   f"but Freqtrade has no open trade to sell")
            logger.warning(msg)
            telegram(msg)
            return False
        remaining = target
        sold_total = 0.0
        last_id = "?"
        current_rate = 0.0
        profit_pct = 0.0
        open_rate = 0.0
        for trade in trades:
            if remaining <= 1e-12:
                break
            trade_amt = float(trade.get("amount") or 0)
            if trade_amt <= 0:
                continue
            chunk = round(min(remaining, trade_amt), 8)
            trade_id = str(trade["trade_id"])
            logger.info("SELL %s: %.8f of live %.8f %s (%.0f%%) trade_id=%s trade_amount=%.8f",
                        pair, chunk, held, base, sell_ratio * 100, trade_id, trade_amt)
            _ft_request("POST", "/forcesell", json={"tradeid": trade_id, "ordertype": "market", "amount": chunk})
            remaining = round(remaining - chunk, 8)
            sold_total = round(sold_total + chunk, 8)
            last_id = trade_id
            open_rate = float(trade.get("open_rate") or 0)
            current_rate = float(trade.get("current_rate") or 0)
            profit_pct = float(trade.get("current_profit_pct") or 0) * 100
        if sold_total <= 0:
            msg = (f"IADSS SELL skipped -- {pair} -- exchange holds {held:.8f} {base} "
                   f"but the open trade amount is still 0 (order not filled)")
            logger.warning(msg)
            telegram(msg)
            return False
        note = ""
        if remaining > 1e-8:
            note = f"\nNote: wanted {target:.8f} {base} but Freqtrade only tracks {sold_total:.8f}"
        profit_abs = (sold_total * (current_rate - open_rate)) if open_rate > 0 and current_rate > 0 else 0.0
        telegram(f"IADSS SELL executed\nPair: {pair}\nSold: {sold_total:.4f} ({int(sell_ratio*100)}% of live {held:.4f} {base})\n"
                 f"Rate: {current_rate:.4f} ({profit_pct:+.2f}%)\nThis sell: ${profit_abs:+.2f}\nTrade ID: {last_id}{note}")
        logger.info("SELL success: %s sold=%.8f of live %.8f profit=$%+.2f", pair, sold_total, held, profit_abs)
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
            }
        return jsonify({
            "pair": pair, "open_trade": trade_info, "free_balance": round(free, 2),
            "next_buy_stake": round(free * get_stake_ratio(pair), 2),
            "stake_source": "live_exchange",
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
