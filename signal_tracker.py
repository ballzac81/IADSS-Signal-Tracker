#!/usr/bin/env python3
"""IADSS Signal Tracker — Flask webhook server with 3-step buy/sell sequence.

Buy logic : queries live balance -> stakes 50% of free cash (configurable via STAKE_RATIO)
Sell logic: queries open trade  -> sells 50% of current position (configurable via SELL_RATIO)
"""

import json
import logging
import os
import time
import requests
from functools import wraps
from flask import Flask, request, jsonify

# -- Config -------------------------------------------------------------------
SECRET_TOKEN    = os.environ.get("SECRET_TOKEN", "")
TRADING_PAIR    = os.environ.get("TRADING_PAIR", "SOL/USD")
WINDOW_SECONDS  = int(os.environ.get("WINDOW_SECONDS", 144000))
REDIS_URL       = os.environ.get("REDIS_URL", "redis://IADSS_redis:6379/0")
FREQTRADE_API   = os.environ.get("FREQTRADE_API", "http://freqtrade:8080/api/v1")
FREQTRADE_USER  = os.environ.get("FREQTRADE_USER", "admin")
FREQTRADE_PASS  = os.environ.get("FREQTRADE_PASS", "")
TG_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT         = os.environ.get("TELEGRAM_CHAT_ID", "")

STAKE_RATIO     = float(os.environ.get("STAKE_RATIO", "0.5"))   # 50% of free balance per buy
SELL_RATIO      = float(os.environ.get("SELL_RATIO", "0.5"))    # 50% of position per sell
MIN_STAKE       = float(os.environ.get("MIN_STAKE", "10.0"))    # minimum USD stake
API_RETRIES     = int(os.environ.get("API_RETRIES", "3"))
API_RETRY_DELAY = float(os.environ.get("API_RETRY_DELAY", "5.0"))
API_TIMEOUT     = int(os.environ.get("API_TIMEOUT", "15"))

# -- Logging ------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# -- Redis setup --------------------------------------------------------------
try:
    import redis as _redis_mod
    _redis = _redis_mod.from_url(REDIS_URL, decode_responses=True)
    _redis.ping()
    USE_REDIS = True
    logger.info("State backend: Redis (%s)", REDIS_URL)
except Exception as e:
    logger.warning("Redis unavailable (%s) -- using file state", e)
    USE_REDIS = False

STATE_FILE = "/tmp/iadss_state.json"

# -- Flask --------------------------------------------------------------------
app = Flask(__name__)

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

# -- State helpers ------------------------------------------------------------
def _default_seq() -> dict:
    return {"step": 0, "ts": 0.0, "pair": ""}

def _redis_key(direction: str, pair: str) -> str:
    return f"iadss:{direction}:{pair}"

def _file_key(direction: str, pair: str) -> str:
    return f"{direction}:{pair}"

def _load_file_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_file_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def load_seq(direction: str, pair: str) -> dict:
    if USE_REDIS:
        raw = _redis.get(_redis_key(direction, pair))
        if raw:
            return json.loads(raw)
    else:
        return _load_file_state().get(_file_key(direction, pair), _default_seq())
    return _default_seq()

def save_seq(direction: str, pair: str, seq: dict):
    if USE_REDIS:
        _redis.set(_redis_key(direction, pair), json.dumps(seq), ex=WINDOW_SECONDS + 3600)
    else:
        state = _load_file_state()
        state[_file_key(direction, pair)] = seq
        _save_file_state(state)

# -- Sequence advancement -----------------------------------------------------
def advance(direction: str, step: int, pair: str) -> tuple:
    seq = load_seq(direction, pair)
    now = time.time()

    if seq["step"] > 0 and (now - seq["ts"]) > WINDOW_SECONDS:
        save_seq(direction, pair, _default_seq())
        telegram(f"IADSS {direction.upper()} window expired for {pair} -- sequence reset")
        seq = _default_seq()

    if seq["step"] > 0 and seq.get("pair") and seq["pair"] != pair:
        logger.warning("[%s] pair mismatch (%s vs %s) -- resetting", direction, seq["pair"], pair)
        return False, "pair_mismatch"

    expected = seq["step"] + 1
    if step != expected:
        save_seq(direction, pair, _default_seq())
        return False, "out_of_order"

    save_seq(direction, pair, {"step": step, "ts": now, "pair": pair})
    return True, "ok"

# -- Freqtrade API helpers -----------------------------------------------------
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
    # available_capital accounts for already-open trade allocations
    return float(data.get("available_capital", data.get("total", 0)))

def get_open_trade(pair: str) -> dict:
    """Return the open trade for a pair (most recently opened), or None."""
    data = _ft_request("GET", "/trades", params={"is_open": True})
    trades = data.get("trades", [])
    open_trades = [t for t in trades if t["pair"] == pair and t["is_open"]]
    if not open_trades:
        return None
    return sorted(open_trades, key=lambda t: t["open_date"])[-1]

# -- Trade execution ----------------------------------------------------------
def execute_buy(pair: str) -> bool:
    """Open or add to position using STAKE_RATIO of free balance."""
    try:
        free  = get_free_balance()
        stake = round(free * STAKE_RATIO, 2)

        if stake < MIN_STAKE:
            msg = (
                f"IADSS BUY skipped for {pair}\n"
                f"Free balance: ${free:.2f} -> stake ${stake:.2f} is below ${MIN_STAKE:.0f} minimum"
            )
            logger.warning(msg)
            telegram(msg)
            return False

        logger.info("BUY %s: staking $%.2f (%.0f%% of $%.2f free)", pair, stake, STAKE_RATIO * 100, free)
        result    = _ft_request("POST", "/forcebuy", json={"pair": pair, "stake_amount": stake})
        trade_id  = result.get("trade_id") or result.get("id", "?")
        open_rate = result.get("open_rate", "?")

        msg = (
            f"IADSS BUY executed\n"
            f"Pair: {pair}\n"
            f"Stake: ${stake:.2f} (50% of ${free:.2f} free)\n"
            f"Rate: {open_rate}\n"
            f"Trade ID: {trade_id}"
        )
        telegram(msg)
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

        trade_id           = str(trade["trade_id"])
        total_amount       = float(trade["amount"])   # base currency (e.g. SOL)
        sell_amount        = round(total_amount * SELL_RATIO, 8)
        current_rate       = trade.get("current_rate", "?")
        current_profit_pct = trade.get("current_profit_pct", 0) * 100

        logger.info(
            "SELL %s: %.8f of %.8f (%.0f%%) trade_id=%s",
            pair, sell_amount, total_amount, SELL_RATIO * 100, trade_id,
        )

        _ft_request("POST", "/forcesell", json={
            "tradeid":   trade_id,
            "ordertype": "market",
            "amount":    sell_amount,
        })

        msg = (
            f"IADSS SELL executed\n"
            f"Pair: {pair}\n"
            f"Sold: {sell_amount:.4f} (50% of {total_amount:.4f})\n"
            f"Rate: {current_rate} ({current_profit_pct:+.2f}%)\n"
            f"Trade ID: {trade_id}"
        )
        telegram(msg)
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

# -- Signal handler -----------------------------------------------------------
def handle_signal(direction: str, step: int):
    data = request.get_json(silent=True) or {}
    pair = data.get("pair", TRADING_PAIR)

    ok, reason = advance(direction, step, pair)
    if not ok:
        logger.info("[%s] step %d rejected: %s (%s)", direction, step, reason, pair)
        return jsonify({"status": reason}), 200

    step_labels = {
        1: "1/3: Market structure",
        2: "2/3: Confirmation",
        3: "3/3: Trend/breakout -- firing trade",
    }
    label = step_labels.get(step, f"{step}/3")

    if step < 3:
        telegram(f"IADSS {direction.upper()} {label} -- {pair}")
        return jsonify({"status": "ok", "step": step}), 200

    # Step 3 -- execute trade then reset sequence
    if direction == "buy":
        telegram(f"BUY 3/3: Trend/breakout -- firing trade for {pair}")
        success = execute_buy(pair)
    else:
        telegram(f"SELL 3/3: Trend/breakout -- firing trade for {pair}")
        success = execute_sell(pair)

    save_seq(direction, pair, _default_seq())
    return jsonify({"status": "trade_executed" if success else "trade_failed"}), 200

# -- Endpoints ----------------------------------------------------------------
@app.route("/mr-buy",      methods=["POST"])
@require_token
def mr_buy():
    return handle_signal("buy", 1)

@app.route("/confirm-buy", methods=["POST"])
@require_token
def confirm_buy():
    return handle_signal("buy", 2)

@app.route("/lb-buy",      methods=["POST"])
@require_token
def lb_buy():
    return handle_signal("buy", 3)

@app.route("/mr-sell",      methods=["POST"])
@require_token
def mr_sell():
    return handle_signal("sell", 1)

@app.route("/confirm-sell", methods=["POST"])
@require_token
def confirm_sell():
    return handle_signal("sell", 2)

@app.route("/lb-sell",      methods=["POST"])
@require_token
def lb_sell():
    return handle_signal("sell", 3)

# -- Status -------------------------------------------------------------------
@app.route("/status", methods=["GET"])
@require_token
def status():
    pair     = request.args.get("pair", TRADING_PAIR)
    buy_seq  = load_seq("buy",  pair)
    sell_seq = load_seq("sell", pair)
    now      = time.time()

    def seq_info(seq):
        step      = seq["step"]
        remaining = max(0, WINDOW_SECONDS - (now - seq["ts"])) if step > 0 else 0
        return {
            "step":              f"{step}/3",
            "pair":              seq.get("pair", ""),
            "window_remaining_s": int(remaining),
        }

    return jsonify({
        "pair":        pair,
        "buy":         seq_info(buy_seq),
        "sell":        seq_info(sell_seq),
        "persistence": "redis" if USE_REDIS else "file",
        "status":      "ok",
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
