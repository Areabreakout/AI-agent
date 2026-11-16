# -*- coding: utf-8 -*-
"""配置管理：LLM API、模块开关、自选股、盯盘规则等，持久化到 config.json"""
import json
import os
import threading
import time

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

# 默认配置。所有参数均可通过设置页面或直接编辑 config.json 调整
DEFAULT_CONFIG = {
    "llm": {
        # OpenAI 兼容格式：换 base_url 即可切换 DeepSeek / GLM / 通义 / OpenAI 等
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.3,
        # 常用服务商快速模板（前端设置页一键填充）
        "presets": [
            {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
            {"name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
            {"name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
            {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            {"name": "Kimi", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"}
        ]
    },
    "trading": {
        # 自动交易安全机制：AI 仅给建议，须人工确认后才执行
        "require_manual_confirm": True,
        # Open 模式：AI 拥有极高权限可自主下单（免人工确认）。
        # 双保险设计：此开关开启 + 前端免责声明勾选后才能自主提交委托。
        "auto_mode": False,
        "default_quantity": 100,
        # 浏览器自动化：用户本地浏览器登录的券商网页交易地址
        "broker_url": "https://jywg.eastmoney.com/",
        "headless": False  # 必须可见，方便用户观察与手动确认
    },
    "watch": {
        # 盯盘轮询间隔（秒）
        "poll_interval": 30
    },
    "favorites": ["600519", "000001", "300750", "601318"],
    "watch_rules": []
}

_lock = threading.RLock()  # 可重入锁：load() 持锁时首次启动会调用 save()
_cache = None
_mtime = 0


def load():
    """读取配置（带文件变更检测，热更新）"""
    global _cache, _mtime
    with _lock:
        if os.path.exists(CONFIG_PATH):
            mtime = os.path.getmtime(CONFIG_PATH)
            if _cache is None or mtime != _mtime:
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        _cache = _deep_merge(json.load(f), DEFAULT_CONFIG)
                    _mtime = mtime
                except (json.JSONDecodeError, OSError):
                    _cache = json.loads(json.dumps(DEFAULT_CONFIG))
        else:
            _cache = json.loads(json.dumps(DEFAULT_CONFIG))
            save(_cache)
        return _cache


def _deep_merge(user_cfg: dict, default_cfg: dict) -> dict:
    """用户配置覆盖默认配置，保留默认模板等字段"""
    merged = json.loads(json.dumps(default_cfg))
    for k, v in user_cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(v, merged[k])
        else:
            merged[k] = v
    return merged


def save(cfg: dict):
    """保存配置到 config.json（保存前自动备份上一版到 config.json.bak，防误写丢失）"""
    global _cache, _mtime
    with _lock:
        if os.path.exists(CONFIG_PATH):
            try:
                import shutil
                shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".bak")
            except OSError:
                pass
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        _cache = json.loads(json.dumps(cfg))
        _mtime = os.path.getmtime(CONFIG_PATH)


def update(section: str, data: dict):
    """更新某个配置节并持久化"""
    cfg = load()
    # presets 为内置模板，不允许被覆盖丢失
    if section == "llm" and "presets" in data:
        data = {k: v for k, v in data.items() if k != "presets"}
    cfg[section] = _deep_merge(data, cfg.get(section, {})) if isinstance(data, dict) else data
    save(cfg)
    return cfg


def llm_ready() -> bool:
    """判断 LLM API 是否已配置可用"""
    llm = load()["llm"]
    return bool(llm.get("api_key") and llm.get("base_url") and llm.get("model"))
