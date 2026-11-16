# -*- coding: utf-8 -*-
"""FastAPI 后端：行情 / 选股 / 分析 / 盯盘 / 交易 / 设置 / AI 对话（SSE 流式）"""
import json
import os
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, audit_log, auto_trader, browser_trader, llm, settings, tools_astock, watcher
from . import plugins

app = FastAPI(title="AI 炒股 Agent")

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


@app.get("/")
def index():
    return FileResponse(os.path.join(UI_DIR, "index.html"))


# ---------------- 配置 ----------------

@app.get("/api/config")
def get_config():
    cfg = settings.load()
    # 脱敏：api_key 只回传是否已配置与尾号
    safe = json.loads(json.dumps(cfg, ensure_ascii=False))
    for section in ("llm",):
        key = safe.get(section, {}).get("api_key", "")
        safe[section]["api_key"] = (key[:4] + "***" + key[-4:]) if len(key) > 8 else ("***" if key else "")
    safe["_deps"] = {"browser_lib": browser_trader.available()}
    safe["_llm_ready"] = settings.llm_ready()
    return safe


def _validate_llm(data: dict):
    """LLM 配置防呆校验：防止误粘贴启动命令/含中文的内容导致请求层编码错误"""
    for field in ("api_key", "base_url", "model"):
        v = data.get(field)
        if not v:
            continue
        try:
            v.encode("ascii")
        except UnicodeEncodeError:
            raise HTTPException(400, f"{field} 不能包含中文或全角字符 —— 请检查是否粘贴了错误内容（API Key 应为服务商提供的一串英文数字）")
    if data.get("api_key") and (" " in data["api_key"] or "py " in data["api_key"].lower()):
        raise HTTPException(400, "API Key 格式不对：不应包含空格 —— 你可能粘贴了命令或其他文本，请粘贴服务商提供的 Key")
    if data.get("base_url") and not str(data["base_url"]).startswith(("http://", "https://")):
        raise HTTPException(400, "Base URL 必须以 http:// 或 https:// 开头")


@app.post("/api/config/{section}")
def update_config(section: str, data: dict):
    if section not in ("llm", "trading", "watch", "favorites", "auto_trade"):
        raise HTTPException(400, f"未知配置节 {section}")
    if section == "llm":
        _validate_llm(data)
    # 审计：配置变更（敏感字段脱敏，不记录 key 明文）
    safe = dict(data)
    if section == "llm" and "api_key" in safe:
        safe["api_key"] = f"<{len(str(safe.get('api_key') or ''))}字符，已隐藏>"
    audit_log.log("config_change", f"修改配置 [{section}]", safe)
    settings.update(section, data)
    if section == "watch":
        watcher.start()
    return {"ok": True}


@app.get("/api/audit_log")
def get_audit_log(n: int = 300, type: str = ""):
    """审计日志：用户操作 / AI 分析 / 工具调用 / 交易执行 / 盯盘触发"""
    events = audit_log.tail(n)
    if type:
        events = [e for e in events if e.get("type") == type]
    return {"events": events, "total": len(events)}


# ---------------- 全自动交易 ----------------

@app.get("/api/auto_trade/events")
def auto_trade_events():
    return {"events": auto_trader.get_events()[-50:]}


@app.post("/api/auto_trade/stop")
def auto_trade_stop():
    """急停：关闭全自动开关（Open 模式需到交易页单独关闭）"""
    settings.update("auto_trade", {"enabled": False})
    return {"ok": True}


# ---------------- 可选插件（安装/拆卸见 app/plugins/ 目录） ----------------

@app.get("/api/plugins")
def list_plugins():
    return plugins.list_plugins()


class PluginCfgBody(BaseModel):
    config: dict


@app.post("/api/plugins/{name}/config")
def save_plugin_config(name: str, body: PluginCfgBody):
    if name not in plugins.registry():
        raise HTTPException(404, f"插件 {name} 未安装")
    plugins.save_plugin_cfg(name, body.config)
    return {"ok": True}


# ---------------- 行情 ----------------

@app.get("/api/index_spot")
def index_spot():
    return tools_astock.get_index_spot()


@app.get("/api/favorites")
def favorites():
    cfg = settings.load()
    return tools_astock.get_multi_spot(cfg.get("favorites", []))


class FavBody(BaseModel):
    symbol: str
    action: str = "add"  # add / remove


@app.post("/api/favorites")
def update_fav(body: FavBody):
    cfg = settings.load()
    favs = cfg.get("favorites", [])
    s = str(body.symbol).zfill(6)
    if body.action == "add" and s not in favs:
        favs.append(s)
    elif body.action == "remove" and s in favs:
        favs.remove(s)
    settings.update("favorites", favs)
    return {"ok": True, "favorites": favs}


@app.get("/api/spot/{symbol}")
def spot(symbol: str):
    try:
        return tools_astock.get_spot(symbol)
    except Exception as e:
        raise HTTPException(404, str(e))


@app.get("/api/kline/{symbol}")
def kline(symbol: str, days: int = 60):
    return tools_astock.get_kline(symbol, days)


@app.get("/api/market_summary")
def market_summary():
    return tools_astock.market_summary()


# ---------------- 个股深度分析 ----------------

@app.post("/api/analyze")
def analyze(body: dict):
    symbol = str(body.get("symbol", "")).zfill(6)
    if not symbol.isdigit():
        raise HTTPException(400, "股票代码无效")
    audit_log.log("user_action", "请求个股分析", {"symbol": symbol})
    try:
        return agent.analyze_stock(symbol)
    except Exception as e:
        raise HTTPException(500, f"获取分析数据失败: {e}")


class ReportBody(BaseModel):
    symbol: str
    name: str = ""


@app.post("/api/ai_report")
def ai_report(body: ReportBody):
    """AI 生成个股分析报告（流式 SSE）"""
    if not settings.llm_ready():
        raise HTTPException(400, "请先在【设置】中配置 LLM API")

    def gen():
        try:
            data = agent.analyze_stock(body.symbol)
            prompt = f"""请基于以下实时数据，对股票 {body.symbol} {body.name} 做深度分析报告。
用 Markdown 输出，包含：## 技术面（结合均线/量能/涨跌幅）## 基本面 ## 消息面 ## 综合评估 ## 操作建议 ## 风险提示。

【重要】"操作建议"部分必须给出明确、可执行的结论，禁止模棱两可（如"可适当关注""谨慎乐观"），
严格按以下格式输出：

## 操作建议
**明确评级：买入**（或：卖出 / 持有 / 观望，四选一）

| 项目 | 建议 |
|---|---|
| 建议买入价 | xx.xx ~ xx.xx 元（结合支撑位给出区间）|
| 目标价 | xx.xx 元（预期涨幅 +x%，说明依据）|
| 止损价 | xx.xx 元（跌破即离场，说明依据）|
| 建议仓位 | 轻仓（1-2成）/ 半仓 / 重仓，说明理由 |
| 持有周期 | 短线（x个交易日）/ 中线（x周）/ 长期持有（x个月以上）|

评级判定标准（供参考，按技术面+基本面综合打分）：
- 买入：趋势向上 + 量价配合 + 估值合理
- 卖出：趋势破位 + 放量滞涨或基本面恶化
- 持有：已持仓且趋势未破坏
- 观望：方向不明或风险收益比不佳

【评级纪律 —— 严禁无脑看多】
a. 买入是最苛刻的评级：必须同时满足 ①均线趋势向上 ②量能配合 ③估值不贵 ④大盘环境不差，任何一条不满足即降级为观望。
b. 综合评估里必须先写"反方论据"（本次看空的理由至少 2 条），反方论据未被数据明确推翻时，禁止给买入。
c. 风险收益比硬约束：(目标价-现价)/(现价-止损价) < 2 时不得给买入。
d. 以下情形默认观望：近期已大涨处于高位、放量滞涨、跌破关键均线、大盘普跌、数据不足。
e. 观望是完全正当的结论——市场大部分时间都不值得动手。不要为了显得有用而输出买入。

最后注明"仅供参考，不构成投资建议"。

【必须】报告最后一行输出机器可读的价格信号块（供一键填单/盯盘功能解析，格式固定）：
<!--SIGNALS:{{"rating":"买入","buy_low":买入价下限数字,"buy_high":买入价上限数字,"target":目标价数字,"stop":止损价数字,"horizon":"短线"}}-->
其中 rating 取值：买入/卖出/持有/长期持有/观望；价格均为纯数字（元）。若评级为观望可不给 buy_low/buy_high。

实时行情：{json.dumps(data['spot'], ensure_ascii=False, default=str)[:1500]}
技术指标：{json.dumps(data['indicators'], ensure_ascii=False)}
基本面：{json.dumps(data['profile'], ensure_ascii=False)[:800]}
近期K线（最近15日）：{json.dumps(data['kline_tail'][-15:], ensure_ascii=False, default=str)[:1500]}
相关新闻：{json.dumps(data['news'], ensure_ascii=False, default=str)[:1200]}"""
            report_text = []
            for chunk in llm.chat_stream([{"role": "user", "content": prompt}]):
                report_text.append(chunk)
                yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
            # 审计：AI 分析结论（解析信号块中的评级与价格）
            import re as _re
            full = "".join(report_text)
            m = _re.search(r"<!--SIGNALS:(.*?)-->", full, _re.S)
            if m:
                try:
                    sig = json.loads(m.group(1))
                    audit_log.log("ai_analysis", f"AI报告完成 {body.symbol}",
                                  {"rating": sig.get("rating"), "buy_low": sig.get("buy_low"),
                                   "buy_high": sig.get("buy_high"), "target": sig.get("target"),
                                   "stop": sig.get("stop"), "horizon": sig.get("horizon")})
                except Exception:
                    audit_log.log("ai_analysis", f"AI报告完成 {body.symbol}", {"note": "信号块解析失败"})
            else:
                audit_log.log("ai_analysis", f"AI报告完成 {body.symbol}", {"note": "未输出信号块"})
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- 智能选股 ----------------

@app.post("/api/screen")
def screen(body: dict):
    strategy = body.get("strategy", "volume_break")
    pool_size = int(body.get("pool_size", 20))
    try:
        return {"strategy": strategy, "stocks": tools_astock.screen_stocks(strategy, pool_size)}
    except Exception as e:
        raise HTTPException(500, f"选股失败: {e}")


# ---------------- AI 对话助手（SSE 流式 + 工具调用） ----------------

class ChatBody(BaseModel):
    messages: list  # [{"role": "user"/"assistant", "content": "..."}]


@app.post("/api/chat")
def chat(body: ChatBody):
    if not settings.llm_ready():
        raise HTTPException(400, "请先在【设置】中配置 LLM API")
    msgs = [m for m in body.messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs:
        raise HTTPException(400, "消息为空")
    # 审计：记录用户最新一条消息（截断防刷屏）
    last_user = [m for m in msgs if m["role"] == "user"]
    if last_user:
        audit_log.log("user_action", "AI 对话提问",
                      {"message": str(last_user[-1]["content"])[:200]})

    def gen():
        try:
            cb = agent.make_tool_callback()
            for chunk in llm.chat_stream(msgs, on_tool_call=cb):
                yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- 盯盘 ----------------

@app.get("/api/watch/events")
def watch_events(since: int = 0):
    return {"events": watcher.get_events()[since:] if since else watcher.get_events()[-50:],
            "total": len(watcher.get_events())}


class RuleBody(BaseModel):
    symbol: str
    name: str = ""
    type: str  # price_above / price_below / pct_above / pct_below
    threshold: float


@app.post("/api/watch/rules")
def add_rule(body: RuleBody):
    audit_log.log("user_action", "添加盯盘规则",
                  {"symbol": str(body.symbol).zfill(6), "name": body.name,
                   "type": body.type, "threshold": body.threshold})
    cfg = settings.load()
    rules = cfg.get("watch_rules", [])
    rules.append({"symbol": str(body.symbol).zfill(6), "name": body.name,
                  "type": body.type, "threshold": body.threshold})
    settings.update("watch_rules", rules)
    watcher.start()
    return {"ok": True, "rules": rules}


@app.delete("/api/watch/rules/{index}")
def del_rule(index: int):
    cfg = settings.load()
    rules = cfg.get("watch_rules", [])
    if 0 <= index < len(rules):
        r = rules[index]
        audit_log.log("user_action", "删除盯盘规则", r)
        rules.pop(index)
        settings.update("watch_rules", rules)
    return {"ok": True, "rules": rules}


# ---------------- 交易（浏览器自动化） ----------------

class OpenBody(BaseModel):
    url: str = ""


@app.post("/api/trade/open")
def trade_open(body: OpenBody):
    return browser_trader.open_broker(body.url or None)


@app.get("/mock_broker.html")
def mock_broker():
    """本地模拟券商下单页（交易控制自检用，零风险）"""
    return FileResponse(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "ui", "mock_broker.html"))


@app.post("/api/trade/test")
def trade_test(body: dict):
    """交易控制链路自检：模拟页上验证 填单→点击 全链路"""
    side = body.get("side", "buy")
    audit_log.log("user_action", "交易控制自检", {"side": side})
    return browser_trader.run_control_test(side)


@app.get("/api/trade/status")
def trade_status():
    return browser_trader.status()


class FillBody(BaseModel):
    symbol: str
    price: float
    quantity: int
    side: str = "buy"
    auto: bool = False  # Open 模式：请求自主提交（还需配置层 auto_mode 同时开启，双保险）


@app.post("/api/trade/fill")
def trade_fill(body: FillBody):
    """自动填充委托单。
    默认仅填充，由用户在浏览器中人工点击下单按钮；
    Open 模式（config.auto_mode=True 且请求 auto=True）下自动提交委托，并记录交易日志。"""
    cfg = settings.load()["trading"]
    submit = bool(body.auto and cfg.get("auto_mode"))  # 双保险：两层都要开
    result = browser_trader.fill_order(body.symbol, body.price, body.quantity,
                                       body.side, submit=submit)
    # 审计：用户发起的填单/提交（来源=用户）
    audit_log.log("trade_exec", "用户发起交易填单",
                  {"symbol": str(body.symbol).zfill(6), "side": body.side,
                   "price": body.price, "quantity": body.quantity,
                   "submit": submit, "ok": result.get("ok", False),
                   "submitted": result.get("submitted", False),
                   "error": result.get("error")})
    # 留痕：填充与自主下单均记入交易日志
    if result.get("ok"):
        browser_trader.append_trade_log({
            "symbol": str(body.symbol).zfill(6), "side": body.side,
            "price": body.price, "quantity": body.quantity,
            "submitted": result.get("submitted", False),
            "mode": result.get("mode", "manual_confirm")})
    return result


@app.get("/api/trade/log")
def trade_log(n: int = 50):
    """交易日志（Open 模式自主下单留痕）"""
    return {"logs": browser_trader.get_trade_log(n)}


@app.post("/api/trade/close")
def trade_close():
    browser_trader.close()
    return {"ok": True}


# ---------------- 启动钩子 ----------------

@app.on_event("startup")
def on_startup():
    # 启动盯盘线程（如有规则）
    if settings.load().get("watch_rules"):
        watcher.start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8730)
