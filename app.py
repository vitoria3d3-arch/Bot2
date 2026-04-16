"""
Binance Futures Bot - Limit Order Cycling
Supports 4 independent API keys, demo/live toggle, balance display, auto-cycling.
"""

import os
import time
import hmac
import hashlib
import threading
import logging
import uuid
import math
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, render_template, request, jsonify, Response
import json

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bot")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory state: multiple trades per API slot
# ---------------------------------------------------------------------------
# active_trades maps trade_id -> trade_state_dict
active_trades = {}
# trade_locks maps trade_id -> lock
trade_locks = {}
# Store API credentials for each slot to make UI easier
api_credentials = [None, None, None, None]

URL_LIVE = "https://fapi.binance.com"
URL_TESTNET = "https://testnet.binancefuture.com"
RECV_WINDOW = 10000


# ---------------------------------------------------------------------------
# Binance helpers – all accept base_url so each slot can be demo or live
# ---------------------------------------------------------------------------
def _sign(params: dict, secret: str) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = RECV_WINDOW
    qs = urlencode(params)
    params["signature"] = hmac.new(
        secret.encode(), qs.encode(), hashlib.sha256
    ).hexdigest()
    return params


def _headers(api_key: str) -> dict:
    return {"X-MBX-APIKEY": api_key}


class BinanceAPIError(Exception):
    def __init__(self, status_code: int, code: int, msg: str):
        self.status_code = status_code
        self.code = code
        self.msg = msg
        super().__init__(f"Binance {status_code}: [{code}] {msg}")


def _handle_response(resp: requests.Response):
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        return resp.text

    if resp.status_code >= 400 or (
        isinstance(data, dict) and "code" in data and data["code"] < 0
    ):
        code = data.get("code", resp.status_code) if isinstance(data, dict) else resp.status_code
        msg = data.get("msg", resp.text) if isinstance(data, dict) else resp.text
        raise BinanceAPIError(resp.status_code, code, msg)

    return data


def bn_get(base_url: str, path: str, params: dict, api_key: str, secret: str):
    params = _sign(dict(params), secret)
    r = requests.get(
        f"{base_url}{path}", params=params,
        headers=_headers(api_key), timeout=15,
    )
    return _handle_response(r)


def bn_post(base_url: str, path: str, params: dict, api_key: str, secret: str):
    params = _sign(dict(params), secret)
    r = requests.post(
        f"{base_url}{path}", params=params,
        headers=_headers(api_key), timeout=15,
    )
    return _handle_response(r)


def bn_delete(base_url: str, path: str, params: dict, api_key: str, secret: str):
    params = _sign(dict(params), secret)
    r = requests.delete(
        f"{base_url}{path}", params=params,
        headers=_headers(api_key), timeout=15,
    )
    return _handle_response(r)


# ---------------------------------------------------------------------------
# Price / symbol helpers
# ---------------------------------------------------------------------------
def get_mark_price(base_url: str, symbol: str) -> float:
    r = requests.get(
        f"{base_url}/fapi/v1/premiumIndex",
        params={"symbol": symbol}, timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["markPrice"])


_symbol_info_cache: dict = {}


def get_symbol_info(base_url: str, symbol: str) -> dict:
    cache_key = f"{base_url}:{symbol}"
    if cache_key in _symbol_info_cache:
        return _symbol_info_cache[cache_key]
    
    r = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=15)
    r.raise_for_status()
    for s in r.json()["symbols"]:
        if s["symbol"] == symbol:
            info = {
                "pp": s["pricePrecision"],
                "qp": s["quantityPrecision"],
                "tick_size": 0.0,
                "min_qty": 0.0,
                "step_size": 0.0,
                "min_notional": 0.0
            }
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    info["tick_size"] = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    info["min_qty"] = float(f["minQty"])
                    info["step_size"] = float(f["stepSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    info["min_notional"] = float(f.get("notional", 0) or f.get("minNotional", 0))
            
            _symbol_info_cache[cache_key] = info
            return info
    raise ValueError(f"Symbol {symbol} not found on Binance Futures")


def round_step(value: float, step: float) -> float:
    """Round a value to the nearest multiple of step without floating point errors."""
    if not step:
        return value
    # Use decimal-like precision for step rounding
    precision = len(str(step).split('.')[-1]) if '.' in str(step) else 0
    rounded = round(round(value / step) * step, precision)
    return rounded


def round_price(price: float, precision: int) -> float:
    return float(f"%.{precision}f" % price)


def round_qty(qty: float, precision: int) -> float:
    return float(f"%.{precision}f" % qty)


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------
def get_account_balance(base_url: str, api_key: str, secret: str) -> list:
    """Return list of asset balances with non-zero values."""
    data = bn_get(base_url, "/fapi/v2/balance", {}, api_key, secret)
    balances = []
    for item in data:
        total = float(item.get("balance", 0))
        available = float(item.get("availableBalance", 0))
        if total != 0 or available != 0:
            balances.append({
                "asset": item["asset"],
                "balance": round(total, 4),
                "available": round(available, 4),
            })
    return balances


# ---------------------------------------------------------------------------
# Order helpers
# ---------------------------------------------------------------------------
def place_limit_order(
    base_url: str, api_key: str, secret: str,
    symbol: str, side: str, qty: float, price: float,
) -> dict:
    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": price,
    }
    return bn_post(base_url, "/fapi/v1/order", params, api_key, secret)


def cancel_all_orders(base_url: str, api_key: str, secret: str, symbol: str):
    try:
        return bn_delete(base_url, "/fapi/v1/allOpenOrders", {"symbol": symbol}, api_key, secret)
    except Exception:
        return None


def cancel_order(base_url: str, api_key: str, secret: str, symbol: str, order_id: int):
    try:
        return bn_delete(base_url, "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, api_key, secret)
    except Exception:
        return None


def get_order_status(
    base_url: str, api_key: str, secret: str, symbol: str, order_id: int
) -> str:
    data = bn_get(
        base_url, "/fapi/v1/order",
        {"symbol": symbol, "orderId": order_id},
        api_key, secret,
    )
    return data["status"]


def set_leverage(base_url: str, api_key: str, secret: str, symbol: str, leverage: int):
    try:
        bn_post(base_url, "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, api_key, secret)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Bot worker thread
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Bot worker thread
# ---------------------------------------------------------------------------
def bot_worker(trade_id: str):
    trade = active_trades.get(trade_id)
    if not trade:
        return

    base_url = trade["base_url"]
    api_key = trade["api_key"]
    secret = trade["secret"]
    symbol = trade["symbol"]
    direction = trade["direction"]
    pct = trade["pct"]
    usdc_amount = trade["qty"]
    leverage = trade["leverage"]
    mode_label = "TESTNET" if base_url == URL_TESTNET else "LIVE"

    log.info(f"[{trade_id}] Starting ({mode_label}): {symbol} {direction} {pct}% amount=${usdc_amount} USDC lev={leverage}x")
    trade["status"] = "running"
    trade["log"] = []

    def add_log(msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        trade["log"] = (trade["log"] or [])[-29:] + [entry]
        log.info(f"[{trade_id}] {msg}")

    add_log(f"Iniciando em {symbol} ({direction}) com ${usdc_amount} USDC")

    try:
        add_log(f"Modo: {mode_label}")
        set_leverage(base_url, api_key, secret, symbol, leverage)
        
        entry_side = "BUY" if direction == "LONG" else "SELL"
        
        # Anchor the Cycle: Handle manual entry price or fetch mark price
        info = get_symbol_info(base_url, symbol)
        
        manual_entry = trade.get("entry_price")
        if manual_entry and float(manual_entry) > 0:
            initial_mark = float(manual_entry)
            add_log(f"Usando Preço de Entrada Manual: {initial_mark}")
        else:
            initial_mark = get_mark_price(base_url, symbol)
            add_log(f"Preço de Entrada Automático (Mark): {initial_mark}")
        
        intended_notional = usdc_amount * leverage
        min_notional = info.get("min_notional", 0)
        
        # Calculate required quantity: Try CLOSEST (rounding) first
        step = info["step_size"]
        if step > 0:
            raw_qty = intended_notional / initial_mark
            asset_qty = round_step(raw_qty, step)
            
            # If closest is below Binance minimum, then and ONLY THEN round up
            if (asset_qty * initial_mark) < min_notional:
                asset_qty = math.ceil(min_notional / initial_mark / step) * step
            
            # Final rounding for precision
            asset_qty = float(f"%.{info['qp']}f" % asset_qty)
        else:
            asset_qty = intended_notional / initial_mark
        
        entry_price_r = round_step(initial_mark, info["tick_size"])
        
        # Constraint Checks (Done once at start)
        if asset_qty < info["min_qty"]:
            add_log(f"Erro: Quantia ${usdc_amount} com {leverage}x muito baixa. Mínimo para {symbol} é {info['min_qty']} (~${round(info['min_qty'] * initial_mark, 2)})")
            trade["status"] = "error"
            return
        
        actual_notional = asset_qty * entry_price_r
        if actual_notional < info["min_notional"]:
            add_log(f"Erro: Valor Total ${round(actual_notional, 2)} abaixo do mínimo da Binance (${info['min_notional']}). Aumente a margem ou alavancagem.")
            trade["status"] = "error"
            return

        # Initialize Cycle Loop
        cycle = 0
        while trade.get("running"):
            cycle += 1

            # ---- Phase 1: entry order ----
            add_log(f"Ciclo {cycle}: {entry_side} {asset_qty} @ {entry_price_r}")
            add_log(f"-> Margem: ${usdc_amount} | Alavancagem: {leverage}x | Total Est.: ${round(actual_notional, 2)}")
            try:
                order = place_limit_order(
                    base_url, api_key, secret, symbol, entry_side, asset_qty, entry_price_r
                )
                order_id = order["orderId"]
                trade["current_order"] = {
                    "side": entry_side, "price": entry_price_r, "orderId": order_id,
                }
            except BinanceAPIError as e:
                add_log(f"Erro na ordem: {e.msg}")
                trade["status"] = "error"
                return
            except Exception as e:
                add_log(f"Erro na ordem: {e}")
                trade["status"] = "error"
                return

            fill_ok = _wait_fill_trade(base_url, api_key, secret, symbol, order_id, trade, add_log)
            if not fill_ok:
                return
            add_log(f"{entry_side} preenchido @ {entry_price_r}")

            # ---- Phase 2: take-profit order ----
            if direction == "LONG":
                tp_price = entry_price_r * (1 + pct / 100)
                tp_side = "SELL"
            else:
                tp_price = entry_price_r * (1 - pct / 100)
                tp_side = "BUY"

            tp_price_r = round_step(tp_price, info["tick_size"])
            add_log(f"Ciclo {cycle}: {tp_side} TP limit {asset_qty} @ {tp_price_r}")
            try:
                order2 = place_limit_order(
                    base_url, api_key, secret, symbol, tp_side, asset_qty, tp_price_r
                )
                order_id2 = order2["orderId"]
                trade["current_order"] = {
                    "side": tp_side, "price": tp_price_r, "orderId": order_id2,
                }
            except BinanceAPIError as e:
                add_log(f"Erro TP: {e.msg}")
                trade["status"] = "error"
                return
            except Exception as e:
                add_log(f"Erro TP: {e}")
                trade["status"] = "error"
                return

            fill_ok = _wait_fill_trade(base_url, api_key, secret, symbol, order_id2, trade, add_log)
            if not fill_ok:
                return
            add_log(f"{tp_side} TP preenchido @ {tp_price_r} ✓ Ciclo {cycle} completo")
            trade["cycles"] = cycle

    except Exception as e:
        add_log(f"Erro fatal: {e}")
        trade["status"] = "error"
        return

    add_log("Bot parou.")
    trade["status"] = "stopped"


def _wait_fill_trade(
    base_url: str, api_key: str, secret: str,
    symbol: str, order_id: int, trade: dict, add_log,
) -> bool:
    consecutive_errors = 0
    while trade.get("running"):
        time.sleep(3)
        try:
            status = get_order_status(base_url, api_key, secret, symbol, order_id)
            consecutive_errors = 0
            if status == "FILLED":
                return True
            if status in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
                add_log(f"Ordem {order_id} status inesperado: {status}")
                trade["status"] = "error"
                return False
        except Exception as e:
            consecutive_errors += 1
            add_log(f"Erro ao verificar ordem: {e}")
            if consecutive_errors >= 10:
                add_log("Muitos erros seguidos, parando.")
                trade["status"] = "error"
                return False
            time.sleep(5)

    cancel_order(base_url, api_key, secret, symbol, order_id)
    add_log("Parado – ordem cancelada.")
    trade["status"] = "stopped"
    return False



# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/server-ip")
def api_server_ip():
    if not hasattr(api_server_ip, "_cached"):
        try:
            r = requests.get("https://api.ipify.org?format=json", timeout=5)
            r.raise_for_status()
            api_server_ip._cached = r.json().get("ip", "unknown")
        except Exception:
            try:
                r = requests.get("https://ifconfig.me/ip", timeout=5)
                r.raise_for_status()
                api_server_ip._cached = r.text.strip()
            except Exception:
                api_server_ip._cached = "unavailable"
    return jsonify({"ip": api_server_ip._cached})


@app.route("/api/balance", methods=["POST"])
def api_balance():
    """Fetch USDT balance for a given API key pair. Used by the dashboard."""
    data = request.json
    if not data:
        return jsonify({"error": "empty body"}), 400

    api_key = (data.get("api_key") or "").strip()
    secret = (data.get("secret") or "").strip()
    mode = (data.get("mode") or "live").strip().lower()

    if not api_key or not secret:
        return jsonify({"error": "API Key e Secret são obrigatórios"}), 400

    base_url = URL_TESTNET if mode == "demo" else URL_LIVE

    try:
        balances = get_account_balance(base_url, api_key, secret)
        return jsonify({"ok": True, "balances": balances, "mode": mode})
    except BinanceAPIError as e:
        return jsonify({"error": e.msg}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_slots_data():
    out_slots = []
    for i in range(4):
        creds = api_credentials[i]
        slot_trades = []
        for tid, t in active_trades.items():
            if t.get("slot_id") == i:
                slot_trades.append({
                    "trade_id": tid,
                    "symbol": t.get("symbol"),
                    "direction": t.get("direction"),
                    "pct": t.get("pct"),
                    "qty": t.get("qty"),
                    "leverage": t.get("leverage"),
                    "mode": t.get("mode", "live"),
                    "status": t.get("status", "unknown"),
                    "cycles": t.get("cycles", 0),
                    "current_order": t.get("current_order"),
                    "log": (t.get("log") or [])[-20:],
                })
        
        out_slots.append({
            "slot_id": i,
            "connected": creds is not None,
            "trades": slot_trades
        })
    return out_slots


@app.route("/api/status")
def api_status():
    """Return all active trades and API key persistence info."""
    return jsonify(get_slots_data())


@app.route("/api/events")
def api_events():
    def event_stream():
        while True:
            data = get_slots_data()
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(1)
            
    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json
    if not data:
        return jsonify({"error": "Corpo da requisição vazio"}), 400

    try:
        slot_id = int(data["slot"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Slot inválido"}), 400

    if not 0 <= slot_id <= 3:
        return jsonify({"error": "Slot inválido (0-3)"}), 400

    # Required fields
    for field in ("api_key", "secret", "symbol", "direction", "pct", "qty"):
        val = data.get(field)
        if val is None or (isinstance(val, str) and not str(val).strip()):
            return jsonify({"error": f"O campo '{field}' não pode estar vazio"}), 400

    try:
        pct = float(data["pct"])
        qty = float(data["qty"])
        leverage = int(data.get("leverage", 10))
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Valor numérico inválido: {e}"}), 400

    direction = data["direction"].strip().upper()
    mode = (data.get("mode") or "live").strip().lower()
    base_url = URL_TESTNET if mode == "demo" else URL_LIVE

    # Store credentials for the slot
    api_credentials[slot_id] = {
        "api_key": data["api_key"].strip(),
        "secret": data["secret"].strip(),
        "mode": mode
    }

    trade_id = f"slot{slot_id}_{uuid.uuid4().hex[:6]}"
    
    active_trades[trade_id] = {
        "trade_id": trade_id,
        "slot_id": slot_id,
        "api_key": data["api_key"].strip(),
        "secret": data["secret"].strip(),
        "symbol": data["symbol"].strip().upper(),
        "direction": direction,
        "pct": pct,
        "qty": qty,
        "leverage": leverage,
        "mode": mode,
        "base_url": base_url,
        "entry_price": data.get("entry_price"),
        "running": True,
        "status": "starting",
        "cycles": 0,
        "log": [],
        "current_order": None,
    }

    t = threading.Thread(target=bot_worker, args=(trade_id,), daemon=True)
    t.start()

    return jsonify({"ok": True, "trade_id": trade_id})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.json
    if not data:
        return jsonify({"error": "Corpo da requisição vazio"}), 400
    
    trade_id = data.get("trade_id")
    if not trade_id:
        return jsonify({"error": "trade_id obrigatório"}), 400

    if trade_id in active_trades:
        active_trades[trade_id]["running"] = False
        return jsonify({"ok": True})
    
    return jsonify({"error": "Trade não encontrado"}), 404



# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
