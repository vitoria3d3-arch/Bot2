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
            if s.get("contractType") != "PERPETUAL":
                raise ValueError(f"{symbol} is not a perpetual futures contract (type: {s.get('contractType')})")
            info = {
                "pp": s["pricePrecision"],
                "qp": s["quantityPrecision"],
                "tick_size": 0.0,
                "min_qty": 0.0,
                "max_qty": 0.0,
                "step_size": 0.0,
                "min_notional": 0.0,
            }
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    info["tick_size"] = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    info["min_qty"] = float(f["minQty"])
                    info["max_qty"] = float(f["maxQty"])
                    info["step_size"] = float(f["stepSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    info["min_notional"] = float(f.get("notional", 0) or f.get("minNotional", 0))

            _symbol_info_cache[cache_key] = info
            return info
    raise ValueError(f"Symbol {symbol} not found on Binance Futures")



def round_step(value: float, step: float, precision: int) -> float:
    """Round value to the nearest step multiple, formatted to Binance's precision."""
    if not step:
        return round(value, precision)
    rounded = round(round(value / step) * step, precision)
    # Guard: step coarser than value (e.g. testnet tick=1, price=0.09) → rounds to 0
    if rounded <= 0 < value:
        return round(value, precision)
    return rounded


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
    reduce_only: bool = False, position_side: str = None,
) -> dict:
    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": price,
    }
    if position_side:
        # Hedge mode: positionSide drives open/close — reduceOnly must not be sent
        params["positionSide"] = position_side
    elif reduce_only:
        params["reduceOnly"] = "true"
    return bn_post(base_url, "/fapi/v1/order", params, api_key, secret)


def place_market_close_order(
    base_url: str, api_key: str, secret: str,
    symbol: str, side: str, qty: float,
    position_side: str = None,
) -> dict:
    """Market order to close an open position."""
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
    }
    if position_side:
        params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
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


def get_current_leverage(base_url: str, api_key: str, secret: str, symbol: str) -> int:
    """Read the leverage currently set on Binance for this symbol (never changes it)."""
    data = bn_get(base_url, "/fapi/v2/positionRisk", {"symbol": symbol}, api_key, secret)
    for pos in data:
        if pos.get("symbol") == symbol:
            return int(float(pos.get("leverage", 1)))
    return 1


_position_mode_cache: dict = {}


def get_position_mode(base_url: str, api_key: str, secret: str) -> bool:
    """Return True if the account is in hedge mode (dualSidePosition), False for one-way."""
    cache_key = f"{base_url}:{api_key}"
    if cache_key in _position_mode_cache:
        return _position_mode_cache[cache_key]
    data = bn_get(base_url, "/fapi/v1/positionSide/dual", {}, api_key, secret)
    hedge = bool(data.get("dualSidePosition", False))
    _position_mode_cache[cache_key] = hedge
    return hedge


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
    mode_label = "TESTNET" if base_url == URL_TESTNET else "LIVE"

    log.info(f"[{trade_id}] Starting ({mode_label}): {symbol} {direction} {pct}% amount=${usdc_amount} USDC")
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

        # Read leverage from Binance — never set it
        leverage = get_current_leverage(base_url, api_key, secret, symbol)
        trade["leverage"] = leverage
        add_log(f"Alavancagem na Binance: {leverage}x")

        hedge_mode = get_position_mode(base_url, api_key, secret)
        # In hedge mode positionSide must be set; in one-way mode leave it None
        position_side = ("LONG" if direction == "LONG" else "SHORT") if hedge_mode else None
        add_log(f"Modo de posição: {'Hedge' if hedge_mode else 'One-Way'}")

        entry_side = "BUY" if direction == "LONG" else "SELL"
        
        # Anchor the Cycle: Handle manual entry price or fetch mark price
        info = get_symbol_info(base_url, symbol)
        
        manual_entry = trade.get("entry_price")
        if manual_entry and float(manual_entry) > 0:
            initial_mark = float(manual_entry)
            entry_price_r = round_step(initial_mark, info["tick_size"], info["pp"])
            if abs(entry_price_r - initial_mark) > 1e-8:
                tick = info["tick_size"]
                add_log(f"Erro: Preço {initial_mark} não é múltiplo válido do tick_size ({tick}). "
                        f"Use {entry_price_r} ou {round_step(initial_mark + tick, tick, info['pp'])}.")
                trade["status"] = "error"
                return
            add_log(f"Usando Preço de Entrada Manual: {initial_mark}")
        else:
            initial_mark = get_mark_price(base_url, symbol)
            entry_price_r = round_step(initial_mark, info["tick_size"], info["pp"])
            add_log(f"Preço de Entrada Automático (Mark): {entry_price_r}")

        intended_notional = usdc_amount * leverage
        min_notional = info.get("min_notional", 0)

        raw_qty = intended_notional / initial_mark
        asset_qty = round_step(raw_qty, info["step_size"], info["qp"])

        # Validate qty against Binance's LOT_SIZE bounds
        if asset_qty < info["min_qty"]:
            min_margin = round(info["min_qty"] * initial_mark / leverage, 2)
            add_log(f"Erro: Quantidade {asset_qty} abaixo do mínimo {info['min_qty']}. Margem mínima necessária: ${min_margin} com {leverage}x.")
            trade["status"] = "error"
            return

        if info["max_qty"] > 0 and asset_qty > info["max_qty"]:
            add_log(f"Erro: Quantidade {asset_qty} excede o máximo {info['max_qty']} para {symbol}. Reduza a margem ou alavancagem.")
            trade["status"] = "error"
            return

        if min_notional > 0 and intended_notional < min_notional:
            min_margin = round(min_notional / leverage, 2)
            add_log(f"Erro: ${usdc_amount} × {leverage}x = ${intended_notional} abaixo do mínimo Binance (${min_notional}). Aumente a margem para ${min_margin}.")
            trade["status"] = "error"
            return

        # Initialize Cycle Loop
        cycle = 0
        while trade.get("running"):
            cycle += 1

            # ---- Phase 1: entry order ----
            add_log(f"Ciclo {cycle}: {entry_side} {asset_qty} @ {entry_price_r}")
            add_log(f"-> Margem: ${usdc_amount} | Alavancagem: {leverage}x | Notional: ${round(usdc_amount * leverage, 2)}")
            try:
                order = place_limit_order(
                    base_url, api_key, secret, symbol, entry_side, asset_qty, entry_price_r,
                    position_side=position_side,
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

            tp_price_r = round_step(tp_price, info["tick_size"], info["pp"])
            add_log(f"Ciclo {cycle}: {tp_side} TP limit {asset_qty} @ {tp_price_r}")
            try:
                order2 = place_limit_order(
                    base_url, api_key, secret, symbol, tp_side, asset_qty, tp_price_r,
                    reduce_only=True, position_side=position_side,
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

            fill_ok = _wait_fill_trade(
                base_url, api_key, secret, symbol, order_id2, trade, add_log,
                close_on_stop=True, close_side=tp_side, close_qty=asset_qty,
                position_side=position_side,
            )
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
    close_on_stop: bool = False, close_side: str = None, close_qty: float = None,
    position_side: str = None,
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

    # Stop was requested — cancel the open order first
    cancel_order(base_url, api_key, secret, symbol, order_id)
    add_log("Parado – ordem cancelada.")

    # If entry was already filled (Phase 2), close the open position at market price
    if close_on_stop and close_side and close_qty:
        try:
            place_market_close_order(base_url, api_key, secret, symbol, close_side, close_qty,
                                     position_side=position_side)
            add_log(f"Posição fechada a mercado ({close_side} {close_qty}).")
        except Exception as e:
            add_log(f"AVISO: Falha ao fechar posição automaticamente – feche manualmente! Erro: {e}")

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


def _stop_and_clear_slot(slot_id: int):
    """Stop all running trades for a slot and remove them from active_trades."""
    to_remove = [tid for tid, t in active_trades.items() if t.get("slot_id") == slot_id]
    for tid in to_remove:
        active_trades[tid]["running"] = False
    for tid in to_remove:
        active_trades.pop(tid, None)


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Switch API credentials for a slot: stop all existing trades, then fetch balance."""
    data = request.json
    if not data:
        return jsonify({"error": "empty body"}), 400

    try:
        slot_id = int(data["slot"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Slot inválido"}), 400

    if not 0 <= slot_id <= 3:
        return jsonify({"error": "Slot inválido (0-3)"}), 400

    api_key = (data.get("api_key") or "").strip()
    secret = (data.get("secret") or "").strip()
    mode = (data.get("mode") or "live").strip().lower()

    if not api_key or not secret:
        return jsonify({"error": "API Key e Secret são obrigatórios"}), 400

    base_url = URL_TESTNET if mode == "demo" else URL_LIVE

    # Stop & clear all trades for this slot before switching credentials
    _stop_and_clear_slot(slot_id)
    api_credentials[slot_id] = {"api_key": api_key, "secret": secret, "mode": mode}
    save_settings()

    try:
        balances = get_account_balance(base_url, api_key, secret)
        return jsonify({"ok": True, "balances": balances, "mode": mode})
    except BinanceAPIError as e:
        api_credentials[slot_id] = None
        save_settings()
        return jsonify({"error": e.msg}), 400
    except Exception as e:
        api_credentials[slot_id] = None
        save_settings()
        return jsonify({"error": str(e)}), 500


@app.route("/api/clear-slot", methods=["POST"])
def api_clear_slot():
    """Stop all trades for a slot and clear its credentials (used on mode toggle)."""
    data = request.json
    if not data:
        return jsonify({"error": "empty body"}), 400

    try:
        slot_id = int(data["slot"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Slot inválido"}), 400

    if not 0 <= slot_id <= 3:
        return jsonify({"error": "Slot inválido (0-3)"}), 400

    _stop_and_clear_slot(slot_id)
    api_credentials[slot_id] = None
    save_settings()
    return jsonify({"ok": True})


@app.route("/api/balance", methods=["POST"])
def api_balance():
    """Fetch balance for a given API key pair (read-only, no side effects)."""
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
            "api_key": creds["api_key"] if creds else None,
            "secret": creds["secret"] if creds else None,
            "mode": creds["mode"] if creds else "live",
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
        "leverage": None,
        "mode": mode,
        "base_url": base_url,
        "entry_price": data.get("entry_price"),
        "running": True,
        "status": "starting",
        "cycles": 0,
        "log": [],
        "current_order": None,
    }

    def _run_and_cleanup():
        try:
            bot_worker(trade_id)
        finally:
            time.sleep(5)
            active_trades.pop(trade_id, None)

    t = threading.Thread(target=_run_and_cleanup, daemon=True)
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
# Settings persistence
# ---------------------------------------------------------------------------
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        for i, creds in enumerate(data.get("slots", [])[:4]):
            if creds:
                api_credentials[i] = creds
        log.info("Settings loaded from settings.json")
    except Exception as e:
        log.warning(f"Could not load settings.json: {e}")


def save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"slots": api_credentials}, f, indent=2)
        log.info(f"Settings saved to {SETTINGS_FILE}")
    except Exception as e:
        log.warning(f"Could not save settings.json: {e}")


# Load persisted credentials at import time (works with both python app.py and gunicorn)
load_settings()

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
