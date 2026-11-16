# -*- coding: utf-8 -*-
"""A股数据工具：多数据源容错。
- 实时行情：优先腾讯轻量接口（稳定不易限流），东财 akshare 兜底
- 全市场/选股：东财 spot_em，限流时降级新浪 spot
- K线/财务/新闻：akshare（东财）"""
import time
import traceback
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import requests

# 简单内存缓存：{key: (expire_at, data)}，避免盯盘高频轮询重复请求
_cache = {}


def _cached(key: str, ttl: int, fn):
    now = time.time()
    if key in _cache and _cache[key][0] > now:
        return _cache[key][1]
    data = fn()
    _cache[key] = (now + ttl, data)
    return data


def _fmt_df(df: pd.DataFrame, n: int = 30) -> list:
    df = df.tail(n)
    return json_safe(df.to_dict(orient="records"))


def json_safe(obj):
    """把 numpy/pandas 类型转为原生 Python 类型"""
    import numpy as np
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if pd.isna(v) else v
    if isinstance(obj, float) and pd.isna(obj):
        return None
    if np.isscalar(obj) and pd.isna(obj):
        return None
    return obj


# ---------------- 腾讯实时行情（轻量稳定，防限流首选源） ----------------

_TX_FIELDS = {
    1: "名称", 3: "最新价", 4: "昨收", 5: "今开", 6: "成交量",
    31: "涨跌额", 32: "涨跌幅", 33: "最高", 34: "最低", 36: "成交量",
    37: "成交额", 38: "换手率", 39: "市盈率", 43: "振幅",
    44: "流通市值", 45: "总市值", 46: "市净率",
}


def _tx_prefix(code: str) -> str:
    """腾讯代码前缀：6 开头 sh，其余 sz"""
    code = str(code).zfill(6)
    return ("sh" if code[0] in "569" else "sz") + code


def tencent_quotes(codes: list) -> list:
    """腾讯批量实时行情（单次最多 ~60 只，超出自动分批）"""
    out = []
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        url = "http://qt.gtimg.cn/q=" + ",".join(_tx_prefix(c) for c in batch)
        text = requests.get(url, timeout=10).text
        for line in text.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            fields = line.split("=", 1)[1].strip('"').split("~")
            if len(fields) < 40 or not fields[3]:
                continue
            q = {"代码": fields[2], "名称": fields[1]}
            for idx, name in _TX_FIELDS.items():
                try:
                    q[name] = float(fields[idx])
                except (ValueError, IndexError):
                    q[name] = fields[idx] if idx < len(fields) else None
            out.append(q)
    return out


def _spot_all_em() -> pd.DataFrame:
    """东财全市场行情（选股用）"""
    return ak.stock_zh_a_spot_em()


def _spot_all_sina() -> pd.DataFrame:
    """新浪全市场行情（东财限流时的降级源），列名映射为东财风格"""
    df = ak.stock_zh_a_spot()
    df = df.rename(columns={
        "code": "代码", "name": "名称", "trade": "最新价", "changepercent": "涨跌幅",
        "pricechange": "涨跌额", "settlement": "昨收", "open": "今开",
        "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额",
        "turnoverratio": "换手率"})
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    return df


def spot_all() -> pd.DataFrame:
    """全市场行情：东财优先，限流降级新浪"""
    return _cached("spot_all", 15, lambda: _try_sources([
        ("eastmoney", _spot_all_em), ("sina", _spot_all_sina)]))


def _try_sources(sources):
    last_err = None
    for name, fn in sources:
        try:
            return fn()
        except Exception as e:
            last_err = e
            print(f"[数据源 {name} 失败] {type(e).__name__}: {e}")
    raise RuntimeError(f"所有数据源均不可用，最后错误：{last_err}")


# ---------------- 行情 ----------------

def get_spot(symbol: str) -> dict:
    """单只股票实时行情（腾讯源优先）"""
    qs = tencent_quotes([str(symbol).zfill(6)])
    if qs:
        return qs[0]
    # 兜底：东财全量表
    df = spot_all()
    row = df[df["代码"] == str(symbol).zfill(6)]
    if row.empty:
        raise ValueError(f"未找到股票 {symbol}")
    return json_safe(row.iloc[0].to_dict())


def get_index_spot() -> list:
    """主要指数实时行情（腾讯源）"""
    idx = [("sh000001", "上证指数"), ("sz399001", "深证成指"),
           ("sz399006", "创业板指"), ("sh000688", "科创50")]
    try:
        text = requests.get("http://qt.gtimg.cn/q=" + ",".join(c for c, _ in idx), timeout=10).text
        out = []
        for line in text.split(";"):
            if "=" not in line:
                continue
            f = line.split("=", 1)[1].strip('"').split("~")
            if len(f) < 40:
                continue
            out.append({"名称": f[1], "代码": f[2], "最新价": float(f[3]),
                        "涨跌额": float(f[31]), "涨跌幅": float(f[32])})
        return out
    except Exception:
        # 兜底：东财指数接口
        df = ak.stock_zh_index_spot_em(symbol="上证系列指数")
        want = ["000001"]
        r = df[df["代码"].isin(want)]
        return json_safe(r.to_dict(orient="records"))


def get_multi_spot(symbols: list) -> list:
    """批量实时行情（自选股盯盘用，腾讯源）"""
    if not symbols:
        return []
    codes = [str(s).zfill(6) for s in symbols]
    return tencent_quotes(codes)


# ---------------- K线 ----------------

def get_kline(symbol: str, days: int = 120, period: str = "daily") -> list:
    """历史K线（默认日线、前复权），用于 AI 技术面分析。
    东财接口优先，限流时降级新浪。"""
    code = str(symbol).zfill(6)

    def _from_em():
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=int(days) * 2)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=code, period=period,
                                start_date=start, end_date=end, adjust="qfq")
        cols = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "涨跌幅", "换手率"]
        return df[[c for c in cols if c in df.columns]]

    def _from_sina():
        prefix = "sh" if code[0] in "569" else "sz"
        df = ak.stock_zh_a_daily(symbol=prefix + code, adjust="qfq")
        df = df.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                                "high": "最高", "low": "最低", "volume": "成交量",
                                "amount": "成交额", "turnover": "换手率"})
        df["日期"] = df["日期"].astype(str)
        # 新浪源无涨跌幅列，自行计算
        if "收盘" in df.columns:
            df["涨跌幅"] = (df["收盘"].pct_change() * 100).round(2)
        cols = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "涨跌幅", "换手率"]
        return df[[c for c in cols if c in df.columns]]

    df = _try_sources([("eastmoney_hist", _from_em), ("sina_hist", _from_sina)])
    return _fmt_df(df, n=int(days))


def calc_indicators(kline: list) -> dict:
    """计算常用技术指标：均线、量比、近期涨跌幅"""
    if not kline or len(kline) < 5:
        return {}
    closes = [k["收盘"] for k in kline if k.get("收盘") is not None]
    vols = [k.get("成交量") or 0 for k in kline]
    out = {"最新收盘": closes[-1] if closes else None}
    for n in (5, 10, 20, 60):
        if len(closes) >= n:
            out[f"MA{n}"] = round(sum(closes[-n:]) / n, 2)
    if len(vols) >= 5 and sum(vols[-6:-1]) > 0:
        out["量比(5日)"] = round(vols[-1] / (sum(vols[-6:-1]) / 5), 2)
    if len(closes) >= 5:
        out["近5日涨跌%"] = round((closes[-1] / closes[-6] - 1) * 100, 2)
    if len(closes) >= 20:
        out["近20日涨跌%"] = round((closes[-1] / closes[-21] - 1) * 100, 2)
    return out


# ---------------- 基本面 / 新闻 ----------------

def get_profile(symbol: str) -> dict:
    """个股基本信息（行业、市值、市盈率等）。东财限流时降级腾讯行情兜底。"""
    code = str(symbol).zfill(6)
    try:
        df = _cached(f"profile_{code}", 3600, lambda: ak.stock_individual_info_em(symbol=code))
        return json_safe(dict(zip(df["item"], df["value"])))
    except Exception as e:
        print(f"[get_profile 东财失败，降级腾讯] {type(e).__name__}: {e}")
        q = get_spot(code)
        return {
            "股票简称": q.get("名称"),
            "总市值": q.get("总市值"),
            "流通市值": q.get("流通市值"),
            "市盈率-动态": q.get("市盈率"),
            "市净率": q.get("市净率"),
            "数据源": "腾讯行情（东财限流自动降级）",
        }


def get_news(symbol: str, n: int = 8) -> list:
    """个股相关新闻资讯（东财限流时返回提示而不是抛错）"""
    code = str(symbol).zfill(6)
    try:
        df = _cached(f"news_{code}", 600, lambda: ak.stock_news_em(symbol=code))
        return json_safe(df.head(n).to_dict(orient="records"))
    except Exception as e:
        print(f"[get_news 东财失败] {type(e).__name__}: {e}")
        return [{"关键词": code, "新闻内容": f"新闻源暂时受限（{type(e).__name__}），请稍后重试"}]


# ---------------- 智能选股 ----------------

def screen_stocks(strategy: str = "volume_break", pool_size: int = 30) -> list:
    """本地策略选股，返回候选池（供 AI 进一步分析或用户盯盘）
    策略：
      volume_break  放量突破：量比>2、涨幅>3%、站上20日线
      oversold      超跌反弹：近20日跌幅>15%、当日转涨
      ma_bull       均线多头：MA5>MA10>MA20 且多头排列
      hot           热门人气：成交额排名前列
    """
    spot = spot_all()
    df = spot.copy()
    # 基础过滤：剔除 ST、退市、新股与超低价股
    df = df[~df["名称"].astype(str).str.contains("ST|退|N ", na=False)]
    df = df[df["最新价"].notna() & (df["最新价"] > 2)]

    if strategy == "hot":
        picked = df.nlargest(pool_size, "成交额")
        return json_safe(picked.head(pool_size).to_dict(orient="records"))

    # 策略预筛（减少逐股拉K线的数量）
    if strategy == "volume_break":
        pre = df[(df["涨跌幅"] > 3) & (df["换手率"] > 2)].nlargest(60, "成交额")
    elif strategy == "oversold":
        pre = df[(df["涨跌幅"] > 0) & (df["涨跌幅"] < 5)].nlargest(60, "成交额")
    elif strategy == "ma_bull":
        pre = df[(df["涨跌幅"] > 0)].nlargest(60, "成交额")
    else:
        raise ValueError(f"未知策略 {strategy}")

    results = []
    for _, row in pre.head(40).iterrows():
        try:
            k = get_kline(row["代码"], days=70)
            closes = [x["收盘"] for x in k if x.get("收盘") is not None]
            vols = [x.get("成交量") or 0 for x in k]
            if len(closes) < 25:
                continue
            ma5 = sum(closes[-5:]) / 5
            ma10 = sum(closes[-10:]) / 10
            ma20 = sum(closes[-20:]) / 20
            vol_ratio = vols[-1] / (sum(vols[-6:-1]) / 5) if sum(vols[-6:-1]) > 0 else 0
            ok = False
            if strategy == "volume_break":
                ok = vol_ratio > 2 and closes[-1] > ma20 and row["涨跌幅"] > 3
            elif strategy == "oversold":
                ok = (closes[-1] / closes[-21] - 1) < -0.15 and row["涨跌幅"] > 0
            elif strategy == "ma_bull":
                ok = ma5 > ma10 > ma20 and closes[-1] > ma5
            if ok:
                results.append(json_safe(row.to_dict()))
                if len(results) >= pool_size:
                    break
        except Exception:
            continue
    return results


def market_summary() -> dict:
    """市场概览：指数 + 涨跌家数，供 AI 每日总结"""
    idx = get_index_spot()
    try:
        df = spot_all()
        up = int((df["涨跌幅"] > 0).sum())
        down = int((df["涨跌幅"] < 0).sum())
        amount = round(df["成交额"].sum() / 1e8, 0)
    except Exception:
        up = down = None
        amount = None
    return {"指数": idx, "上涨家数": up, "下跌家数": down, "总成交额(亿)": amount}


# ---------------- 盯盘引擎回调 ----------------

def evaluate_rules(symbols: list) -> dict:
    """批量取行情，供 watcher 判断规则（价格/涨跌幅阈值）"""
    return {s["代码"]: s for s in get_multi_spot(symbols)}


if __name__ == "__main__":
    print(get_spot("600519"))
