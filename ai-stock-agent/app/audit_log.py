# -*- coding: utf-8 -*-
"""审计日志：记录 用户操作 / AI 分析结论 / 工具调用 / 交易执行 / 盯盘触发 / 配置变更。
追加写 JSONL（每行一个事件），文件 audit_log.jsonl，可供程序化审计与回溯。"""
import json
import os
import threading
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit_log.jsonl")
_lock = threading.Lock()

# 事件类型: user_action / ai_analysis / ai_tool_call / trade_exec / watch_trigger / auto_trade / config_change


def log(event_type: str, action: str, detail: dict = None):
    rec = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "type": event_type, "action": action, "detail": detail or {}}
    with _lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass


def tail(n: int = 300) -> list:
    """读取最近 n 条事件（旧→新）"""
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        out = []
        for x in lines[-n:]:
            try:
                out.append(json.loads(x))
            except Exception:
                pass
        return out
    except Exception:
        return []
