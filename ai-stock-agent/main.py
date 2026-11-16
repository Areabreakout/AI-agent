# -*- coding: utf-8 -*-
"""AI 炒股 Agent —— 桌面应用入口
启动 FastAPI 本地服务（127.0.0.1:8730），再用 pywebview 打开桌面窗口。
也可以直接用浏览器访问 http://127.0.0.1:8730 使用。

启动：py -3.11 main.py
"""
import threading
import webbrowser

import uvicorn

from app.api import app

HOST, PORT = "127.0.0.1", 8730
URL = f"http://{HOST}:{PORT}"


def run_server():
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def main():
    server = threading.Thread(target=run_server, daemon=True)
    server.start()

    # 优先使用 pywebview 桌面窗口；未安装时自动回退到默认浏览器
    try:
        import webview
        webview.create_window("AI 炒股 Agent", URL, width=1380, height=880,
                              min_size=(1080, 700))
        webview.start()
    except ImportError:
        print("未安装 pywebview，使用系统浏览器打开……（pip install pywebview 可获得桌面窗口体验）")
        webbrowser.open(URL)
        print(f"服务已启动：{URL}，按 Ctrl+C 退出")
        try:
            server.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
