# -*- coding: utf-8 -*-
"""加密货币插件（可拆卸模块）：

安装：本文件放在 app/plugins/ 目录下即可（依赖：pip install ccxt）。
拆卸：把本文件移出 app/plugins/ 目录（或删除），重启应用即彻底移除该功能，
      设置页开关、Agent 工具全部自动消失，无需改动其他任何文件。

默认关闭；启用后 AI 对话支持加密货币行情查询，交易走交易所 API（默认沙盒模拟盘）。
"""
import json

try:
    import ccxt
except ImportError:
    ccxt = None

from . import plugin_cfg

# ---- 插件协议 ----
NAME = "crypto"
DISPLAY_NAME = "加密货币"
DEFAULTS = {
    "enabled": False,      # 默认关闭
    "exchange": "binance",
    "api_key": "",
    "secret": "",
    "sandbox": True,       # 默认模拟盘，避免真实资金风险
}


def enabled() -> bool:
    return plugin_cfg(NAME).get("enabled", False) and ccxt is not None


def _exchange():
    cfg = plugin_cfg(NAME)
    ex = getattr(ccxt, cfg["exchange"])({
        "apiKey": cfg.get("api_key") or None,
        "secret": cfg.get("secret") or None,
        "enableRateLimit": True,
    })
    if cfg.get("sandbox"):
        ex.set_sandbox_mode(True)
    return ex


# ---------------- 数据函数 ----------------

def ticker(symbol: str = "BTC/USDT") -> dict:
    t = _exchange().fetch_ticker(symbol)
    return {"symbol": symbol, "last": t.get("last"), "change%": t.get("percentage"),
            "high": t.get("high"), "low": t.get("low"), "volume": t.get("baseVolume")}


def kline(symbol: str = "BTC/USDT", limit: int = 60) -> list:
    ohlcv = _exchange().fetch_ohlcv(symbol, timeframe="1d", limit=int(limit))
    return [{"timestamp": row[0], "open": row[1], "high": row[2],
             "low": row[3], "close": row[4], "volume": row[5]} for row in ohlcv]


# ---------------- Agent 工具注册 ----------------

def get_tools() -> list:
    if not enabled():
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": "crypto_ticker",
                "description": "获取加密货币实时行情（默认 BTC/USDT）",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string", "description": "交易对，如 BTC/USDT"}},
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "crypto_kline",
                "description": "获取加密货币日线K线（用于技术分析）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "交易对，如 ETH/USDT"},
                        "limit": {"type": "integer", "description": "K线数量，默认60"}
                    },
                    "required": ["symbol"]
                }
            }
        },
    ]


def handles(name: str) -> bool:
    return name in ("crypto_ticker", "crypto_kline")


def dispatch(name: str, args: dict) -> str:
    try:
        if name == "crypto_ticker":
            result = ticker(args.get("symbol", "BTC/USDT"))
        elif name == "crypto_kline":
            result = kline(args.get("symbol", "BTC/USDT"), args.get("limit", 60))
        else:
            result = {"error": f"未知工具 {name}"}
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
