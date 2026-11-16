# -*- coding: utf-8 -*-
"""浏览器自动化交易：
- 使用 DrissionPage 启动本地 Chromium，用户在其中登录自己的券商网页交易系统
- Agent 仅"填充委托单"（代码/价格/数量），下单按钮由用户人工点击确认（安全机制）
- 券商网页版界面各不相同，内置东方财富证券模板，其他券商可手动填单
"""
import json
import os
import shutil
import threading
import time
import traceback
from datetime import datetime

from . import settings

# DrissionPage 为可选依赖：未安装时交易页降级为"仅建议模式"
try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except ImportError:
    ChromiumPage = None
    ChromiumOptions = None

_lock = threading.Lock()
_page = None  # 全局浏览器页面对象

# 独立浏览器实例：固定端口 + 独立用户数据目录
# 避免与用户日常使用的浏览器冲突（PageDisconnectedError 的常见根因），
# 同时保留券商登录状态（重启应用后可重连同一实例）
_DEBUG_PORT = 9333
_PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "browser_profile")


def available() -> bool:
    return ChromiumPage is not None


def _find_browser() -> str:
    """查找本机 Chromium 内核浏览器（优先 Chrome，其次 Edge）"""
    candidates = [
        shutil.which("chrome"), shutil.which("msedge"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return ""


def _get_or_launch():
    """获取已连接的浏览器；没有则启动独立实例（可见窗口，方便用户登录与观察）"""
    global _page
    with _lock:
        if _page is not None:
            try:
                _ = _page.url  # 探活
                return _page
            except Exception:
                _page = None
        cfg = settings.load()["trading"]
        co = ChromiumOptions()
        co.set_local_port(_DEBUG_PORT)        # 独立端口，不与用户日常浏览器冲突
        co.set_user_data_path(_PROFILE_DIR)   # 独立配置目录，保留券商登录状态
        co.headless(cfg.get("headless", False))  # 默认有界面：用户需要看到并确认下单
        co.set_argument("--start-maximized")
        browser_path = _find_browser()
        if browser_path:
            co.set_browser_path(browser_path)
        _page = ChromiumPage(co)
        # 导航容错：新启动的浏览器首次导航可能连接未就绪，重试一次
        target = cfg.get("broker_url", "https://jywg.eastmoney.com/")
        for attempt in range(2):
            try:
                _page.get(target)
                return _page
            except Exception:
                if attempt == 0:
                    time.sleep(2)
                else:
                    err = traceback.format_exc()
                    try:
                        _page.quit()
                    except Exception:
                        pass
                    _page = None
                    raise RuntimeError(f"浏览器页面连接失败，请确认本机已安装 Chrome 或 Edge。详情：{err[-300:]}")
        return _page


def open_broker(url: str = None):
    """打开券商登录页，等待用户在浏览器中登录自己的账号"""
    try:
        page = _get_or_launch()
        target = url or settings.load()["trading"].get("broker_url")
        if page.url != target:
            page.get(target)
        return {"ok": True, "url": page.url,
                "hint": "请在弹出的浏览器窗口中完成登录，登录后回到本页面操作"}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e),
                "hint": "浏览器启动失败：请确认已安装 Chrome/Edge，且没有被安全软件拦截"}


def status() -> dict:
    """浏览器连接状态（纯查询，不启动浏览器 —— 避免查询副作用）"""
    global _page
    if not available():
        return {"available": False, "running": False,
                "hint": "未安装 DrissionPage，交易功能不可用（pip install DrissionPage）"}
    if _page is None:
        return {"available": True, "running": False,
                "hint": "浏览器未启动，点击「打开浏览器登录」连接券商"}
    try:
        return {"available": True, "running": True, "url": _page.url, "title": _page.title}
    except Exception:
        _page = None  # 连接已断开，清理状态
        return {"available": True, "running": False,
                "hint": "浏览器连接已断开，请重新点击「打开浏览器登录」"}


# 东方财富证券网页交易的元素定位模板（常见委托下单页结构）
# 若券商改版或使用其他券商，可在此调整选择器
# 注意：每个字段是候选选择器列表（依次尝试），DrissionPage 不支持逗号分隔多选
EM_TEMPLATES = {
    "buy": {
        "code_input": ["#stockcode", "[name='stockcode']", "#input_code"],
        "price_input": ["#price", "[name='price']", "#input_price"],
        "amount_input": ["#amount", "[name='amount']", "#input_amount"],
        "submit_btn": ["#buy", "[name='buy']", "#tradebtn", ".buybtn", "tag:button@@text():买入"],
    },
    "sell": {
        "code_input": ["#stockcode", "[name='stockcode']", "#input_code"],
        "price_input": ["#price", "[name='price']", "#input_price"],
        "amount_input": ["#amount", "[name='amount']", "#input_amount"],
        "submit_btn": ["#sell", "[name='sell']", "#tradebtn", ".sellbtn", "tag:button@@text():卖出"],
    },
}


def _try_ele(page, selectors, timeout=3):
    """依次尝试候选选择器，返回 (元素, 命中的选择器)；全部失败返回 (None, None)"""
    for sel in selectors:
        try:
            ele = page.ele(sel, timeout=timeout)
        except Exception:
            ele = None
        if ele is not None:
            return ele, sel
    return None, None

# 交易日志（留痕追溯，Open 模式必备）
TRADE_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trade_log.json")


def append_trade_log(entry: dict):
    """追加交易日志（含 Open 模式自主下单，便于追溯）"""
    logs = []
    if os.path.exists(TRADE_LOG_PATH):
        try:
            with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except (json.JSONDecodeError, OSError):
            logs = []
    entry["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logs.append(entry)
    with open(TRADE_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(logs[-500:], f, ensure_ascii=False, indent=2)  # 最多保留500条


def get_trade_log(n: int = 50) -> list:
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    try:
        with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)[-n:]
    except (json.JSONDecodeError, OSError):
        return []


def _is_disconnect(e: Exception) -> bool:
    """判断是否为浏览器连接断开类异常（窗口被关闭/进程退出）"""
    name = type(e).__name__
    return "Disconnect" in name or "NotConnected" in name or "no browser" in str(e).lower()


def _fill_page(page, symbol: str, price: float, quantity: int, tpl: dict) -> dict:
    """在券商页面实际执行填充（不点下单按钮）。tpl 每字段为候选选择器列表，依次尝试。"""
    filled = {}
    for field, selectors in tpl.items():
        if field == "submit_btn":
            continue
        ele, hit = _try_ele(page, selectors)
        if ele is None:
            return {"ok": False,
                    "error": f"未找到输入框 {field}（已尝试 {selectors}），请确认已进入委托下单页，或手动填单"}
        value = str(symbol).zfill(6) if field == "code_input" else (
            f"{price:.2f}" if field == "price_input" else str(int(quantity)))
        ele.clear()
        ele.input(value)
        filled[field] = value
    return {"ok": True, "filled": filled}


def fill_order(symbol: str, price: float, quantity: int, side: str = "buy",
               submit: bool = False) -> dict:
    """在已登录的券商页面自动填充委托单。
    submit=False（默认）：只填充，由用户人工点击下单按钮确认。
    submit=True（Open 模式）：填充后自动点击下单按钮提交委托。
    浏览器窗口被关闭导致连接断开时，自动重启独立实例并重试一次（登录态保留）。"""
    global _page
    tpl = EM_TEMPLATES.get(side)
    if not tpl:
        return {"ok": False, "error": f"未知方向 {side}"}
    last_err = None
    for attempt in range(2):
        try:
            page = _get_or_launch()
        except Exception as e:
            return {"ok": False, "error": str(e),
                    "hint": "浏览器未连接：请先点击「打开浏览器登录」并完成券商登录"}
        try:
            result = _fill_page(page, symbol, price, quantity, tpl)
            if not result.get("ok"):
                return result

            if submit:
                # Open 模式：自动点击下单按钮（候选选择器依次尝试）
                btn, hit = _try_ele(page, tpl["submit_btn"])
                if btn is None:
                    result.update({"submitted": False,
                                   "error": f"未找到下单按钮（已尝试 {tpl['submit_btn']}），请手动点击提交",
                                   "hint": "委托单已填充"})
                    return result
                btn.click()
                result["submitted"] = True
                result["mode"] = "open"
                result["hint"] = "Open 模式：AI 已自动提交委托（详见交易日志），请注意查看券商回报"
            else:
                result["mode"] = "manual_confirm"
                result["hint"] = "委托单已自动填充，请在浏览器中核对后手动点击【买入/卖出】确认提交"
            return result
        except Exception as e:
            last_err = e
            if _is_disconnect(e):
                # 窗口被关闭/连接断开：清理失效连接，重启独立实例（配置目录保留登录态）
                with _lock:
                    try:
                        if _page is not None:
                            _page.quit()
                    except Exception:
                        pass
                    _page = None
                if attempt == 0:
                    continue
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "hint": "浏览器重启后仍连接失败：请手动点击「打开浏览器登录」检查券商页面状态后重试"}
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "hint": "自动填充失败，请手动填单（建议信息已在左侧生成）"}
    return {"ok": False, "error": f"{type(last_err).__name__}: {last_err}",
            "hint": "自动填充失败，请手动填单（建议信息已在左侧生成）"}


MOCK_URL = "http://127.0.0.1:8730/mock_broker.html"


def run_control_test(side: str = "buy") -> dict:
    """交易控制链路自检（零风险）：在本地模拟券商页验证
    ① 浏览器启动 ② 元素定位 ③ 填单 ④ 按钮点击，全部通过说明交易控制正常。"""
    steps = []

    def ok(name, detail=""):
        steps.append({"step": name, "ok": True, "detail": detail})

    def fail(name, detail):
        steps.append({"step": name, "ok": False, "detail": detail})
        return {"passed": False, "steps": steps}

    symbol, price, qty = "600519", 1400.0, 100
    # ① 浏览器启动/连接
    try:
        page = _get_or_launch()
        ok("浏览器启动", "独立实例已连接（端口 9333）")
    except Exception as e:
        return fail("浏览器启动", str(e))

    # ② 打开模拟下单页
    try:
        page.get(MOCK_URL)
        if "mock_broker" not in (page.url or ""):
            raise RuntimeError(f"页面导航异常: {page.url}")
        ok("打开模拟下单页", page.url)
    except Exception as e:
        return fail("打开模拟下单页", f"{type(e).__name__}: {e}")

    # ③④ 填单 + 点击按钮（submit=True；模拟页按钮只记录不交易）
    r = fill_order(symbol, price, qty, side=side, submit=True)
    if not r.get("ok"):
        return fail("自动填单", r.get("error", "未知错误") + " " + r.get("hint", ""))
    ok("自动填单", f"代码 {symbol} / 价格 {price:.2f} / 数量 {qty} 已填入输入框")
    if not r.get("submitted"):
        return fail("点击买卖按钮", r.get("error", "按钮未点击") + " " + r.get("hint", ""))

    # ⑤ 回读模拟页记录，验证按钮真的被点击且值正确
    try:
        time.sleep(0.5)
        text = page.ele("#test-result", timeout=5).text
        expected = f"CLICKED:{side}|{symbol}|{price:.2f}|{qty}"
        if text != expected:
            return fail("点击结果验证", f"页面记录 [{text}] 与预期 [{expected}] 不一致")
        ok("点击结果验证", f"按钮已真实点击，页面记录: {text}")
    except Exception as e:
        return fail("点击结果验证", f"无法回读页面记录: {e}")

    return {"passed": True, "steps": steps,
            "hint": "✅ 交易控制链路完全正常：能定位输入框、正确填单、真实点击买卖按钮。"
                    "真实券商页面只要元素结构一致即可同样控制（建议先在真实页用「仅填充」模式试一次）。"}


def close():
    global _page
    with _lock:
        if _page is not None:
            try:
                _page.quit()
            except Exception:
                pass
            _page = None
