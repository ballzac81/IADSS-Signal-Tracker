"""
IADSS Signal Tracker
A TradingView webhook signal sequencer for Freqtrade.
Listens for 3 signals in order within a time window, then executes a trade.

State is persisted in Redis (preferred) or a local JSON file so the sequence
survives container restarts. State is keyed per pair+direction so multiple
pairs can run independent sequences simultaneously.
"""

import os
import json
import time
import logging
from functools import wraps

import requests
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
SECRET_TOKEN    = os.environ["SECRET_TOKEN"]
FREQTRADE_URL   = os.environ.get("FREQTRADE_URL", "http://freqtrade:8080/api/v1")
FREQTRADE_USER  = os.environ.get("FREQTRADE_USER", "admin")
FREQTRADE_PASS  = os.environ["FREQTRADE_PASS"]
WINDOW_SECONDS  = int(os.environ.get("WINDOW_SECONDS", 144000))
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")
REDIS_URL       = os.environ.get("REDIS_URL", "redis://redis:6379/0")
STATE_FILE      = os.environ.get("STATE_FILE", "/data/state.json")
TRADING_PAIR    = os.environ.get("TRADING_PAIR", "SOL/USD")

# ---------------------------------------------------------------------------
# State persistence — Redis preferred, local file as fallback
# State is stored per (direction, pair) so multiple pairs don't interfere.
#   Redis key:  iadss:{direction}:{pair}   e.g. iadss:buy:SOL/USD
#   File key:   "{direction}:{pair}"       stored in a flat dict in state.json
# ---------------------------------------------------------------------------
USE_REDIS = False
_redis = None

try:
    import redis as _redis_lib
    _redis = _redis_lib.from_url(REDIS_URL, socket_connect_timeout=2)
    _redis.ping()
    USE_REDIS = True
    logger.info("State backend: Redis (%s)", REDIS_URL)
except Exception as e:
    logger.warning("Redis unavailable (%s), falling back to file: %s", e, STATE_FILE)


def _default_seq() -> dict:
    return {"step": 0, "ts": 0.0, "pair": None}


def _redis_key(direction: str, pair: str) -> str:
    return f"iadss:{direction}:{pair}"


def _file_key(direction: str, pair: str) -> str:
    return f"{direction}:{pair}"


def _load_file_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_file_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def load_seq(direction: str, pair: str) -> dict:
    if USE_REDIS:
        try:
            raw = _redis.get(_redis_key(direction, pair))
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.error("Redis read error: %s", e)
    else:
        return _load_file_state().get(_file_key(direction, pair), _default_seq())
    return _default_seq()


def save_seq(direction: str, pair: str, seq: dict) -> None:
    if USE_REDIS:
        try:
            _redis.set(_redis_key(direction, pair), json.dumps(seq))
            return
        except Exception as e:
            logger.error("Redis write error: %s — falling back to file", e)
    state = _load_file_state()
    state[_file_key(direction, pair)] = seq
    _save_file_state(state)


# ---------------------------------------------------------------------------
# Flask app + rate limiter
# ---------------------------------------------------------------------------
app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri=REDIS_URL if USE_REDIS else "memory://",
)

WEBHOOK_LIMIT = "20 per minute"


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------
def require_token(f):
    """Validates SECRET_TOKEN in the JSON body (for webhook endpoints)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        data = request.get_json(force=True, silent=True) or {}
        if data.get("token") != SECRET_TOKEN:
            logger.warning("Rejected webhook — bad token from %s", request.remote_addr)
            return jsonify({"error": "Unauthorized"}), 401
        return f(data, *args, **kwargs)
    return wrapper


def require_token_query(f):
    """Validates SECRET_TOKEN as a query param (?token=...) for GET endpoints."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.args.get("token") != SECRET_TOKEN:
            logger.warning("Rejected status request — bad token from %s", request.remote_addr)
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Telegram helper
# ---------------------------------------------------------------------------
def telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Freqtrade API helper
# ---------------------------------------------------------------------------
def freqtrade_api(method: str, endpoint: str, **kwargs):
    try:
        resp = requests.request(
            method,
            f"{FREQTRADE_URL}{endpoint}",
            auth=(FREQTRADE_USER, FREQTRADE_PASS),
            timeout=10,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Freqtrade API error [%s %s]: %s", method, endpoint, e)
        return None


# ---------------------------------------------------------------------------
# Sequence logic
# ---------------------------------------------------------------------------
def advance(direction: str, step: int, pair: str) -> tuple[bool, str]:
    """
    Advance the sequence for (direction, pair) to the given step.
    Each pair has its own independent sequence — no cross-pair interference.
    Returns (success, reason).
    """
    seq = load_seq(direction, pair)
    now = time.time()

    # Expire check
    if seq["step"] > 0 and (now - seq["ts"]) > WINDOW_SECONDS:
        logger.info("[%s][%s] window expired — resetting", direction, pair)
        save_seq(direction, pair, _default_seq())
        telegram(f"⏰ IADSS {direction.upper()} window expired for {pair} — sequence reset")
        seq = _default_seq()

    # Pair consistency check — reject if a different pair is mid-sequence
    # (Shouldn't happen with per-pair keys, but defence in depth)
    if seq["step"] > 0 and seq.get("pair") and seq["pair"] != pair:
        logger.warning("[%s] pair mismatch (active: %s, incoming: %s) — ignoring",
                       direction, seq["pair"], pair)
        return False, "pair_mismatch"

    expected = seq["step"] + 1
    if step != expected:
        logger.info("[%s][%s] step %d out of order (expected %d) — resetting",
                    direction, pair, step, expected)
        save_seq(direction, pair, _default_seq())
        return False, "out_of_order"

    save_seq(direction, pair, {"step": step, "ts": now, "pair": pair})
    return True, "ok"


def reset_seq(direction: str, pair: str) -> None:
    save_seq(direction, pair, _default_seq())


def window_remaining(direction: str, pair: str) -> int:
    seq = load_seq(direction, pair)
    if seq["step"] == 0:
        return WINDOW_SECONDS
    elapsed = time.time() - seq["ts"]
    return max(0, int(WINDOW_SECONDS - elapsed))


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------
def execute_buy(pair: str) -> bool:
    result = freqtrade_api("POST", "/forcebuy", json={"pair": pair})
    if result:
        msg = f"🟢 IADSS BUY executed: {pair}"
        logger.info(msg)
        telegram(msg)
        reset_seq("buy", pair)
        return True
    logger.error("BUY failed for %s", pair)
    telegram(f"❌ IADSS BUY FAILED: {pair} — check logs")
    return False


def execute_sell(pair: str) -> bool:
    """Sell 50% of the first open position for the pair."""
    trades = freqtrade_api("GET", "/status")
    if not trades:
        logger.error("Could not retrieve open trades")
        return False

    pair_trades = [t for t in trades if t.get("pair") == pair]
    if not pair_trades:
        logger.warning("No open trade for %s", pair)
        telegram(f"⚠️ IADSS SELL triggered for {pair} but no open trade found")
        reset_seq("sell", pair)
        return False

    trade = pair_trades[0]
    trade_id = str(trade["trade_id"])
    amount = trade.get("amount", 0) * 0.5

    result = freqtrade_api("POST", "/forcesell", json={
        "tradeid": trade_id,
        "ordertype": "market",
        "amount": amount,
    })
    if result:
        msg = f"🔴 IADSS SELL executed (50%): {pair}"
        logger.info(msg)
        telegram(msg)
        reset_seq("sell", pair)
        return True
    logger.error("SELL failed for %s", pair)
    telegram(f"❌ IADSS SELL FAILED: {pair} — check logs")
    return False


# ---------------------------------------------------------------------------
# Routes — BUY sequence
# ---------------------------------------------------------------------------
@app.route("/mr-buy", methods=["POST"])
@limiter.limit(WEBHOOK_LIMIT)
@require_token
def mr_buy(data):
    pair = data.get("pair", "UNKNOWN")
    ok, reason = advance("buy", 1, pair)
    if ok:
        telegram(f"📍 BUY 1/3: Mean Reversion — {pair} (window: {window_remaining('buy', pair) // 3600}h)")
    return jsonify({"status": "ok" if ok else "reset", "reason": reason})


@app.route("/confirm-buy", methods=["POST"])
@limiter.limit(WEBHOOK_LIMIT)
@require_token
def confirm_buy(data):
    pair = data.get("pair", "UNKNOWN")
    ok, reason = advance("buy", 2, pair)
    if ok:
        telegram(f"📍 BUY 2/3: Confirmation — {pair} (window: {window_remaining('buy', pair) // 3600}h)")
    return jsonify({"status": "ok" if ok else "reset", "reason": reason})


@app.route("/lb-buy", methods=["POST"])
@limiter.limit(WEBHOOK_LIMIT)
@require_token
def lb_buy(data):
    pair = data.get("pair", "UNKNOWN")
    ok, reason = advance("buy", 3, pair)
    if not ok:
        return jsonify({"status": "reset", "reason": reason})
    telegram(f"📍 BUY 3/3: Trend/breakout — firing trade for {pair}")
    success = execute_buy(pair)
    return jsonify({"status": "executed" if success else "error"})


# ---------------------------------------------------------------------------
# Routes — SELL sequence
# ---------------------------------------------------------------------------
@app.route("/mr-sell", methods=["POST"])
@limiter.limit(WEBHOOK_LIMIT)
@require_token
def mr_sell(data):
    pair = data.get("pair", "UNKNOWN")
    ok, reason = advance("sell", 1, pair)
    if ok:
        telegram(f"📍 SELL 1/3: Mean Reversion — {pair} (window: {window_remaining('sell', pair) // 3600}h)")
    return jsonify({"status": "ok" if ok else "reset", "reason": reason})


@app.route("/confirm-sell", methods=["POST"])
@limiter.limit(WEBHOOK_LIMIT)
@require_token
def confirm_sell(data):
    pair = data.get("pair", "UNKNOWN")
    ok, reason = advance("sell", 2, pair)
    if ok:
        telegram(f"📍 SELL 2/3: Confirmation — {pair} (window: {window_remaining('sell', pair) // 3600}h)")
    return jsonify({"status": "ok" if ok else "reset", "reason": reason})


@app.route("/lb-sell", methods=["POST"])
@limiter.limit(WEBHOOK_LIMIT)
@require_token
def lb_sell(data):
    pair = data.get("pair", "UNKNOWN")
    ok, reason = advance("sell", 3, pair)
    if not ok:
        return jsonify({"status": "reset", "reason": reason})
    telegram(f"📍 SELL 3/3: Trend/breakout — firing sell for {pair}")
    success = execute_sell(pair)
    return jsonify({"status": "executed" if success else "error"})


# ---------------------------------------------------------------------------
# Status endpoint — token-protected
# Usage: GET /status?token=YOUR_SECRET_TOKEN[&pair=SOL/USD]
# Defaults to TRADING_PAIR env var if no pair specified.
# ---------------------------------------------------------------------------
@app.route("/status", methods=["GET"])
@limiter.limit("60 per hour")
@require_token_query
def status():
    pair = request.args.get("pair", TRADING_PAIR)
    now = time.time()
    result = {"pair": pair}
    for direction in ("buy", "sell"):
        seq = load_seq(direction, pair)
        step = seq.get("step", 0)
        ts = seq.get("ts", 0.0)
        elapsed = int(now - ts) if step > 0 else 0
        remaining = max(0, WINDOW_SECONDS - elapsed) if step > 0 else WINDOW_SECONDS
        result[direction] = {
            "step": step,
            "elapsed_seconds": elapsed,
            "remaining_seconds": remaining,
            "window_expired": step > 0 and elapsed > WINDOW_SECONDS,
        }
    result["persistence"] = "redis" if USE_REDIS else "file"
    result["window_seconds"] = WINDOW_SECONDS
    return jsonify(result)


# ---------------------------------------------------------------------------
# Health check — unauthenticated (used by Docker healthcheck only)
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
@limiter.exempt
def health():
    return jsonify({"status": "ok", "persistence": "redis" if USE_REDIS else "file"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info(
        "IADSS Signal Tracker starting — persistence: %s, window: %ds",
        "Redis" if USE_REDIS else "file",
        WINDOW_SECONDS,
    )
    app.run(host="0.0.0.0", port=5000)
