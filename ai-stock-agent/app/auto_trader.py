# -*- coding: utf-8 -*-
"""全自动交易引擎：盯盘规则触发 → AI 复核决策 → Open 模式下自动下单 → 事件留痕

安全机制：
1. 双开关：auto_trade.enabled 与 trading.auto_mode（Open 模式）必须同时开启才会真正下单
2. 单笔金额上限（默认 5000 元），数量按 A 股一手=100 股取整
3. 每日自动交易次数上限（默认 3 次）
4. AI 决策置信度阈值（默认 0.7）
5. 同一股票决策冷却（默认 30 分钟，防止规则反复触发导致重复下单）
6. 每一步（触发/决策/执行/跳过）都写入事件列表可回溯
"""
import json
import math
import re
import threading
import time
import traceback
from datetime import datetime

from . import browser_trader, llm, settings

DEFAULTS = {
    "enabled": False,          # 总开关（还需 Open 模式同时开启才真正交易）
    "max_amount": 5000,        # 单笔金额上限（元）
    "max_daily_trades": 3,     # 每日最大自动交易次数
    "min_confidence": 0.7,     # AI 置信度阈值（0~1）
    "cooldown_minutes": 30,    # 同一股票决策冷却（分钟）
}

# 内存态
_events = []              # 决策/执行事件（前端轮询）
_lock = threading.Lock()
_last_decision = {}       # symbol -> 上次决策时间戳
_daily = {"date": "", "count": 0}


def _cfg() -> dict:
    cfg = settings.load().get("auto_trade") or {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return merged


def get_events() -> list:
    with _lock:
        return list(_events)


def _push_event(kind: str, symbol: str, name: str, detail: str, extra: dict = None):
    with _lock:
        _events.append({"kind": kind, "symbol": symbol, "name": name, "detail": detail,
                        "time": datetime.now().strftime("%H:%M:%S"),
                        **(extra or {})})
        if len(_events) > 200:
            del _events[:-100]
    # 审计：全自动交易事件同步落盘
    from . import audit_log
    audit_log.log("auto_trade", f"[{kind}] {symbol} {name}", {"detail": detail, **(extra or {})})


def _reset_daily_if_needed():
    today = datetime.now().strftime("%Y-%m-%d")
    if _daily["date"] != today:
        _daily.update({"date": today, "count": 0})


def on_trigger(rule: dict, quote: dict):
    """watcher 规则触发时的钩子。在独立线程中执行，避免阻塞盯盘轮询。"""
    threading.Thread(target=_handle, args=(rule, quote), daemon=True,
                     name=f"auto-{rule.get('symbol')}").start()


def _handle(rule: dict, quote: dict):
    try:
        code = str(rule["symbol"]).zfill(6)
        name = quote.get("名称", "")
        acfg = _cfg()
        cfg = settings.load()

        # 前置条件检查：只记录不交易
        if not acfg["enabled"]:
            return  # 全自动开关未开：静默忽略（盯盘页已有普通提醒）
        if not cfg.get("trading", {}).get("auto_mode"):
            _push_event("blocked", code, name, "Open 模式未开启，仅提醒不交易（触发: %s）"
                        % _rule_desc(rule))
            return

        # 冷却检查
        now = time.time()
        if now - _last_decision.get(code, 0) < acfg["cooldown_minutes"] * 60:
            return
        _last_decision[code] = now

        # 每日次数检查
        _reset_daily_if_needed()
        if _daily["count"] >= acfg["max_daily_trades"]:
            _push_event("blocked", code, name,
                        f"已达今日自动交易上限（{acfg['max_daily_trades']} 次）")
            return

        if not settings.llm_ready():
            _push_event("error", code, name, "LLM 未配置，无法决策")
            return

        # ---- AI 复核决策 ----
        _push_event("trigger", code, name, "规则触发: %s，AI 复核中…" % _rule_desc(rule))
        decision = _ai_decide(rule, quote, acfg)
        if decision is None:
            _push_event("error", code, name, "AI 决策解析失败，本次跳过")
            return

        action = decision.get("action")
        confidence = float(decision.get("confidence") or 0)
        reason = decision.get("reason", "")

        if action not in ("buy", "sell"):
            _push_event("skip", code, name, f"AI 建议观望: {reason}", {"confidence": confidence})
            return
        if confidence < acfg["min_confidence"]:
            _push_event("skip", code, name,
                        f"置信度 {confidence:.2f} 低于阈值 {acfg['min_confidence']}，放弃执行。理由: {reason}",
                        {"confidence": confidence})
            return

        # ---- 计算委托参数 ----
        price = float(decision.get("price") or quote.get("最新价") or 0)
        if price <= 0:
            _push_event("error", code, name, "AI 未给出有效价格，本次跳过")
            return
        qty = int(math.floor(acfg["max_amount"] / price / 100) * 100)  # A股一手100股
        if qty < 100:
            _push_event("blocked", code, name,
                        f"单笔上限 {acfg['max_amount']} 元不足买入一手（需 {price * 100:.0f} 元）")
            return

        # ---- 执行 ----
        _push_event("decision", code, name,
                    f"AI 决策: {'买入' if action == 'buy' else '卖出'} @ {price:.2f} x {qty} 股"
                    f"（置信度 {confidence:.2f}）。{reason}", {"confidence": confidence})
        result = browser_trader.fill_order(code, price, qty, side=action, submit=True)
        if result.get("ok") and result.get("submitted"):
            _daily["count"] += 1
            browser_trader.append_trade_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": "auto", "symbol": code, "name": name, "side": action,
                "price": price, "quantity": qty,
                "source": f"全自动交易（置信度 {confidence:.2f}）",
            })
            _push_event("executed", code, name,
                        f"已自动提交{'买入' if action == 'buy' else '卖出'}委托 @ {price:.2f} x {qty} 股，请查看券商回报")
        else:
            _push_event("error", code, name,
                        "下单失败: %s %s" % (result.get("error", ""), result.get("hint", "")))
    except Exception:
        traceback.print_exc()
        _push_event("error", rule.get("symbol", "?"), "", "异常: " + traceback.format_exc(limit=1))


def _rule_desc(rule: dict) -> str:
    t = {"price_above": "价格上穿", "price_below": "价格跌破",
         "pct_above": "涨幅超过", "pct_below": "跌幅超过"}.get(rule["type"], rule["type"])
    unit = "%" if "pct" in rule["type"] else " 元"
    return f"{t} {rule['threshold']}{unit}"


def _ai_decide(rule: dict, quote: dict, acfg: dict):
    """调用 LLM 复核触发信号，返回决策 dict（action/confidence/price/reason）"""
    prompt = f"""你是全自动交易决策引擎。一条盯盘规则刚刚触发，请基于实时数据复核并给出决策。

规则: {json.dumps(rule, ensure_ascii=False, default=str)}
实时行情: {json.dumps({k: quote.get(k) for k in ('名称', '最新价', '涨跌幅', '今开', '最高', '最低', '成交量', '成交额', '换手率', '市盈率')}, ensure_ascii=False, default=str)}
风控约束: 单笔金额上限 {acfg['max_amount']} 元；置信度低于 {acfg['min_confidence']} 不会执行。

决策要求：
- action: "buy"（买入）/ "sell"（卖出）/ "skip"（观望不动）
- confidence: 0~1 的数字，你对该决策的把握
- price: 委托价格（数字，buy 时不高于现价，sell 时不低于现价）
- reason: 一句话理由

只输出一个 JSON 对象，格式：
{{"action": "buy", "confidence": 0.8, "price": 12.34, "reason": "..."}}"""
    try:
        msg = llm.chat([{"role": "user", "content": prompt}])
        text = (msg.content or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        if "action" not in d:
            return None
        return d
    except Exception:
        traceback.print_exc()
        return None
