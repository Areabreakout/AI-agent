# -*- coding: utf-8 -*-
"""盯盘监控：后台线程轮询自选股行情，触发价格/涨跌幅规则后推送提醒"""
import threading
import time
import traceback
from datetime import datetime

from . import audit_log, auto_trader, settings, tools_astock

_events = []          # 提醒事件列表（前端轮询获取）
_lock = threading.Lock()
_running = False
_thread = None


def start():
    """启动盯盘线程（幂等）"""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True, name="watcher")
    _thread.start()


def stop():
    global _running
    _running = False


def _loop():
    while _running:
        try:
            cfg = settings.load()
            rules = cfg.get("watch_rules") or []
            if rules:
                symbols = list({str(r["symbol"]).zfill(6) for r in rules})
                quotes = tools_astock.evaluate_rules(symbols)
                now = datetime.now().strftime("%H:%M:%S")
                for r in rules:
                    code = str(r["symbol"]).zfill(6)
                    q = quotes.get(code)
                    if not q:
                        continue
                    price = q.get("最新价")
                    pct = q.get("涨跌幅")
                    triggered = None
                    # 规则类型：price_above / price_below / pct_above / pct_below
                    if r["type"] == "price_above" and price and price >= r["threshold"]:
                        triggered = f"价格 {price} 已上穿 {r['threshold']}"
                    elif r["type"] == "price_below" and price and price <= r["threshold"]:
                        triggered = f"价格 {price} 已跌破 {r['threshold']}"
                    elif r["type"] == "pct_above" and pct is not None and pct >= r["threshold"]:
                        triggered = f"涨幅 {pct}% 超过 {r['threshold']}%"
                    elif r["type"] == "pct_below" and pct is not None and pct <= r["threshold"]:
                        triggered = f"跌幅 {pct}% 超过 {r['threshold']}%"
                    if triggered and not _recently_fired(r, code):
                        _push(code, q.get("名称", ""), triggered, price, pct, now)
                        r["_last_fired"] = time.time()
                        # 全自动交易钩子：规则触发 → AI 复核 → （Open 模式下）自动下单
                        auto_trader.on_trigger(r, q)
            interval = cfg.get("watch", {}).get("poll_interval", 30)
        except Exception:
            traceback.print_exc()
            interval = 60
        time.sleep(max(5, interval))


def _recently_fired(rule: dict, code: str) -> bool:
    """同一规则 10 分钟内不重复触发"""
    return time.time() - rule.get("_last_fired", 0) < 600


def _push(code, name, msg, price, pct, ts):
    with _lock:
        _events.append({"symbol": code, "name": name, "msg": msg,
                        "price": price, "pct": pct, "time": ts,
                        "ai_comment": None})
        if len(_events) > 200:
            del _events[:-100]
    # 审计：盯盘规则触发
    audit_log.log("watch_trigger", f"盯盘触发 {code}",
                  {"name": name, "msg": msg, "price": price, "pct": pct})


def pop_events(since: int = 0):
    """前端拉取提醒事件"""
    with _lock:
        return [e for i, e in enumerate(_events) if i >= since]


def get_events():
    with _lock:
        return list(_events)


def comment_event(index: int, comment: str):
    """为提醒事件附加 AI 点评"""
    with _lock:
        if 0 <= index < len(_events):
            _events[index]["ai_comment"] = comment
