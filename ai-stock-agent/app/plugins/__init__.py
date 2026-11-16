# -*- coding: utf-8 -*-
"""可选插件加载器：自动发现并加载本目录下的插件模块。

安装：把插件 .py 文件放进本目录，重启应用即自动生效。
拆卸：把插件 .py 文件移出/删除，重启应用即彻底移除该功能。

插件协议（每个插件文件需暴露）：
  NAME            str   插件标识（config.json 中 plugins.{NAME} 配置节）
  DISPLAY_NAME    str   显示名称
  DEFAULTS        dict  配置默认值（含 enabled 开关）
  enabled()       bool 插件是否启用
  get_tools()     list OpenAI function calling 工具定义
  handles(name)   bool 是否处理该工具名
  dispatch(name, args) 执行工具，返回 JSON 字符串
"""
import importlib
import os

from .. import settings

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

_registry = None


def registry(refresh: bool = False) -> dict:
    """发现插件：扫描目录中的 .py 文件（下划线开头除外）并导入"""
    global _registry
    if _registry is not None and not refresh:
        return _registry
    _registry = {}
    if not os.path.isdir(PLUGIN_DIR):
        return _registry
    for fn in sorted(os.listdir(PLUGIN_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        name = fn[:-3]
        try:
            mod = importlib.import_module(f".{name}", __package__)
            for attr in ("NAME", "DISPLAY_NAME", "DEFAULTS", "enabled",
                         "get_tools", "handles", "dispatch"):
                if not hasattr(mod, attr):
                    raise AttributeError(f"缺少协议属性 {attr}")
            _registry[mod.NAME] = {"module": mod, "error": None}
        except Exception as e:
            # 导入失败（如依赖未装）：仍列出，前端显示失败原因
            _registry[name] = {"module": None, "error": f"{type(e).__name__}: {e}"}
    return _registry


def plugin_cfg(name: str) -> dict:
    """读取插件配置（默认值 + 用户配置合并）"""
    entry = registry().get(name)
    if not entry or entry["module"] is None:
        return {}
    mod = entry["module"]
    merged = dict(mod.DEFAULTS)
    merged.update(settings.load().get("plugins", {}).get(name, {}))
    return merged


def save_plugin_cfg(name: str, data: dict):
    """保存插件配置到 config.json 的 plugins.{name} 节"""
    cfg = settings.load()
    plugins = cfg.setdefault("plugins", {})
    section = dict(plugins.get(name, {}))
    section.update({k: v for k, v in data.items() if k in plugin_cfg(name)})
    plugins[name] = section
    settings.save(cfg)


def list_plugins() -> list:
    """插件清单（供前端显示可选模块状态）"""
    out = []
    for name, entry in registry().items():
        if entry["module"] is None:
            out.append({"name": name, "installed": False, "error": entry["error"],
                        "display": name, "enabled": False})
            continue
        mod = entry["module"]
        cfg = plugin_cfg(name)
        # 密码类字段脱敏
        safe = dict(cfg)
        for k in ("api_key", "secret"):
            if safe.get(k):
                v = str(safe[k])
                safe[k] = (v[:4] + "***" + v[-4:]) if len(v) > 8 else "***"
        out.append({"name": name, "installed": True, "error": None,
                    "display": mod.DISPLAY_NAME, "enabled": cfg.get("enabled", False),
                    "config": safe})
    return out


def get_all_tools() -> list:
    """汇总所有已启用插件的 Agent 工具"""
    tools = []
    for name, entry in registry().items():
        mod = entry["module"]
        if mod is not None and plugin_cfg(name).get("enabled"):
            tools.extend(mod.get_tools())
    return tools


def dispatch_tool(tool_name: str, args: dict):
    """把工具调用分发给声明处理它的插件"""
    for name, entry in registry().items():
        mod = entry["module"]
        if mod is not None and plugin_cfg(name).get("enabled") and mod.handles(tool_name):
            return mod.dispatch(tool_name, args)
    return None
