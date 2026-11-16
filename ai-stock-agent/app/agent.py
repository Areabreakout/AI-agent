# -*- coding: utf-8 -*-
"""AI Agent 核心：基于 Function Calling 的工具调用循环。
不训练模型 —— 智能来自用户配置的 LLM API，本模块负责把行情工具暴露给模型并调度执行。"""
import json
import traceback

from . import plugins, settings, tools_astock

# ---------------- 工具注册表 ----------------

def get_tools() -> list:
    """返回 OpenAI function calling 格式的工具定义"""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_spot",
                "description": "获取A股单只股票的实时行情（最新价、涨跌幅、成交量、换手率等）",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string", "description": "6位股票代码，如 600519"}},
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_kline",
                "description": "获取A股历史K线数据（日K、前复权），用于技术面分析",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "6位股票代码"},
                        "days": {"type": "integer", "description": "返回最近多少根K线，默认60"}
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_profile",
                "description": "获取股票基本信息：行业、总市值、市盈率、市净率等基本面数据",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string", "description": "6位股票代码"}},
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_news",
                "description": "获取个股最新相关新闻资讯列表",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "6位股票代码"},
                        "n": {"type": "integer", "description": "新闻条数，默认8"}
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "screen_stocks",
                "description": "按策略智能选股。策略：volume_break(放量突破) / oversold(超跌反弹) / ma_bull(均线多头) / hot(热门人气)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "strategy": {"type": "string", "enum": ["volume_break", "oversold", "ma_bull", "hot"]},
                        "pool_size": {"type": "integer", "description": "返回数量，默认20"}
                    },
                    "required": ["strategy"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "market_summary",
                "description": "获取A股市场整体概览：主要指数、涨跌家数、总成交额",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_index_spot",
                "description": "获取主要指数（上证、深成、创业板、科创50）实时行情",
                "parameters": {"type": "object", "properties": {}}
            }
        },
    ]
    # Open 模式（极高权限）：注册自主交易工具，AI 可直接向券商提交委托
    if settings.load()["trading"].get("auto_mode"):
        tools.append({
            "type": "function",
            "function": {
                "name": "execute_trade",
                "description": "直接执行交易委托（仅 Open 模式可用）：自动填充并提交委托单到已登录的券商浏览器页面。使用前必须已通过交易页连接并登录券商。用户已签署免责声明并承担全部交易风险。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "6位股票代码"},
                        "price": {"type": "number", "description": "委托价格"},
                        "quantity": {"type": "integer", "description": "委托数量（股，A股为100的整数倍）"},
                        "side": {"type": "string", "enum": ["buy", "sell"], "description": "buy=买入 sell=卖出"}
                    },
                    "required": ["symbol", "price", "quantity", "side"]
                }
            }
        })
    # 可选插件工具（如加密货币）：插件文件放入 app/plugins/ 即自动生效
    tools.extend(plugins.get_all_tools())
    return tools


def dispatch(name: str, args: dict) -> str:
    """根据工具名执行对应函数，返回 JSON 字符串结果"""
    try:
        if name == "get_spot":
            result = tools_astock.get_spot(args["symbol"])
        elif name == "get_kline":
            result = tools_astock.get_kline(args["symbol"], args.get("days", 60))
        elif name == "get_profile":
            result = tools_astock.get_profile(args["symbol"])
        elif name == "get_news":
            result = tools_astock.get_news(args["symbol"], args.get("n", 8))
        elif name == "screen_stocks":
            result = tools_astock.screen_stocks(args["strategy"], args.get("pool_size", 20))
        elif name == "market_summary":
            result = tools_astock.market_summary()
        elif name == "get_index_spot":
            result = tools_astock.get_index_spot()
        elif name == "execute_trade":
            # Open 模式：AI 自主下单（双保险校验：配置层 auto_mode 必须开启）
            if not settings.load()["trading"].get("auto_mode"):
                result = {"error": "Open 模式未开启，拒绝执行。请先在【交易】页开启（需同意免责声明）"}
            else:
                from . import browser_trader
                result = browser_trader.fill_order(
                    args["symbol"], float(args["price"]), int(args["quantity"]),
                    args.get("side", "buy"), submit=True)
                if result.get("ok"):
                    browser_trader.append_trade_log({
                        "symbol": str(args["symbol"]).zfill(6), "side": args.get("side", "buy"),
                        "price": args["price"], "quantity": args["quantity"],
                        "submitted": result.get("submitted", False),
                        "mode": "open", "origin": "ai_agent"})
        else:
            # 尝试分发给可选插件（如加密货币）
            result = plugins.dispatch_tool(name, args)
            if result is None:
                return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)
        # 控制返回长度，避免撑爆上下文
        text = json.dumps(result, ensure_ascii=False, default=str)
        # 审计：Agent 工具调用（谁在什么时候调了什么工具、参数与成败）
        from . import audit_log
        audit_log.log("ai_tool_call", f"工具调用 {name}",
                      {"args": {k: v for k, v in args.items() if k != "api_key"},
                       "ok": "error" not in text[:60]})
        return text[:20000]
    except Exception as e:
        traceback.print_exc()
        from . import audit_log
        audit_log.log("ai_tool_call", f"工具调用 {name} 失败",
                      {"args": args, "error": f"{type(e).__name__}: {e}"})
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


# ---------------- 系统提示词 ----------------

SYSTEM_PROMPT = """你是"AI炒股助手"，一位专业的A股投资分析Agent。你可以调用工具获取实时行情、K线、基本面与新闻数据。

工作准则：
1. 涉及具体股票的数据必须先调用工具获取实时数据，绝不凭记忆编造价格。
2. 技术面分析时先取K线，关注均线系统（MA5/10/20/60）、量能变化、近期涨跌幅。
3. 分析要结构化：技术面 / 基本面 / 消息面 / 风险提示 / 操作建议。
4. 【结论必须明确】涉及买卖判断时，必须给出明确评级：买入 / 卖出 / 持有 / 长期持有 / 观望（选其一，
   禁止"可适当关注""谨慎乐观"等模糊表述），并给出具体价格：建议买入（卖出）价区间、目标价、止损价，
   以及建议仓位和持有周期（短线/中线/长期）。价格要结合支撑位、压力位和均线给出依据。
5. 【评级纪律 —— 严禁无脑看多】买入是最苛刻的评级，必须同时满足：①均线趋势向上 ②量能配合 ③估值不贵
   ④大盘环境不差，任何一条不满足即降级为观望。给出买入前必须先陈述至少 2 条反方论据（看空理由），
   反方论据未被数据明确推翻时禁止买入。风险收益比 (目标涨幅/止损跌幅) < 2 时不得给买入。
   近期已大涨、放量滞涨、跌破关键均线、大盘普跌、数据不足 → 默认观望。
   观望是完全正当的结论，不要为了显得有用而输出买入。
6. 所有建议必须附带风险提示，说明"仅供参考，不构成投资建议"。
7. 用户要求交易时：
   - 若工具列表中存在 execute_trade（Open 模式已开启，用户已签署免责声明并承担全部风险），
     可在给出决策依据后直接调用该工具执行委托，执行后告知用户结果并提醒关注券商回报。
   - 否则给出具体的委托建议（代码、方向、价格、数量），并提醒用户到【交易】页面人工确认执行。
8. 回答简洁专业，用中文，善用列表和分节。"""


def make_tool_callback():
    """生成供 llm.chat_stream 使用的工具回调"""
    def callback(call=None):
        if call is None:  # 无参调用：返回工具定义
            return get_tools()
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        return dispatch(call["name"], args)
    return callback


def analyze_stock(symbol: str, name: str = "") -> dict:
    """个股深度分析：一次拉齐数据（行情+K线+指标+基本面+新闻），返回给前端，
    再由前端把数据交给 LLM 生成报告（或直接展示原始数据）。
    行情走腾讯源、K线东财失败自动降级新浪；基本面/新闻为辅助数据，
    东财限流时降级兜底，不阻塞整体分析。"""
    spot = tools_astock.get_spot(symbol)
    kline = tools_astock.get_kline(symbol, days=60)
    indicators = tools_astock.calc_indicators(kline)
    # 基本面：东财接口限流时用腾讯行情的估值字段兜底
    try:
        profile = tools_astock.get_profile(symbol)
    except Exception:
        profile = {"市盈率": spot.get("市盈率"), "市净率": spot.get("市净率"),
                   "流通市值": spot.get("流通市值"), "总市值": spot.get("总市值"),
                   "备注": "东财接口临时限流，估值数据来自腾讯行情源"}
    # 新闻为辅助信息，失败时置空即可
    try:
        news = tools_astock.get_news(symbol, 6)
    except Exception:
        news = []
    return {"spot": spot, "indicators": indicators, "profile": profile,
            "news": news, "kline_tail": kline[-30:]}
