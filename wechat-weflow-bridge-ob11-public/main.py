"""
入口与生命周期管理模块。

负责桥接的启动、停止、主循环重连逻辑，以及命令行入口。
"""

import json
import logging
import os
import sys
import threading
import time
import ctypes

import requests

import state
import config
from senders import create_sender
from ob_client import _run_ob_client
from bridge_core import WeFlowBridge
from web_panel import WebHandler, PAGE
from http.server import HTTPServer
from socketserver import ThreadingMixIn

log = logging.getLogger("ob11-bridge")

# ── DPI Awareness ──
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ── 鼠标漫游（反风控）──
if config.WANDERER_ENABLED:
    from mouse_wanderer import MouseWanderer
    _mouse_wanderer = MouseWanderer(
        min_interval=config.WANDERER_MIN_INTERVAL,
        max_interval=config.WANDERER_MAX_INTERVAL,
        wander_times_range=(config.WANDERER_TIMES_MIN, config.WANDERER_TIMES_MAX),
    )
else:
    _mouse_wanderer = None

# ── 多线程 HTTP 服务器 ──
class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ============ 启动 / 停止 ============


def _start_bridge():
    with state.run_lock:
        if state.running:
            return
        state.running = True
    state.paused.clear()
    state.sender_instance = create_sender()

    if not state.ob_client_started:
        t = threading.Thread(target=_run_ob_client, daemon=True, name="ob11-client")
        t.start()
        state.ob_client_started = True

    state.bridge_thread = threading.Thread(target=_bridge_loop, daemon=True, name="bridge")
    state.bridge_thread.start()
    log.info("[Web] 已启动")
    # 启动鼠标漫游（反风控）
    if _mouse_wanderer is not None:
        _mouse_wanderer.start()


def _stop_bridge():
    # 停止鼠标漫游
    if _mouse_wanderer is not None:
        _mouse_wanderer.stop()

    with state.run_lock:
        state.running = False

    # 切断 SSE 长连接，让 _bridge_loop 的 listen_sse() 从阻塞中退出
    with state.bridge_lock:
        if state.bridge_instance and state.bridge_instance._sse_session:
            try:
                state.bridge_instance._sse_session.close()
                log.info("[Web] SSE 连接已断开")
            except Exception as e:
                log.warning(f"[Web] 断开 SSE 异常: {e}")

    # 关闭 WebSocket 连接，让 _ob_client_main 从 async for 中退出
    _ws = state._ob_ws
    _loop = state._ob_ws_loop
    if _ws:
        try:
            if _loop and _loop.is_running():
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    _ws.close(), _loop
                )
                log.info("[Web] WebSocket 连接已关闭")
        except Exception as e:
            log.warning(f"[Web] 关闭 WebSocket 异常: {e}")

    state._ob_ws_ready.clear()

    # 重置启动标记，让下次 start 能重新拉起 WebSocket 客户端线程
    state.ob_client_started = False
    state._ob_ws_loop = None

    log.info("[Web] 已停止")


def _bridge_loop():
    import ctypes
    ctypes.windll.ole32.CoInitialize(None)

    if not config.ACCESS_TOKEN:
        log.error("❌ 未配置 access_token")
        state.running = False
        return

    log.info(f"Bridge | WeFlow: {config.WE_FLOW_BASE_URL} | OB11: {config.ASTRBOT_OB_URL} | 发送: {config.SEND_METHOD}")

    bridge = WeFlowBridge(state.sender_instance)
    with state.bridge_lock:
        state.bridge_instance = bridge

    try:
        # 用 SSE 端点做探活（stream=True 只检查状态码，不读 body）
        r = requests.get(
            f"{config.WE_FLOW_BASE_URL}/api/v1/push/messages?access_token={config.ACCESS_TOKEN}",
            stream=True, timeout=5
        )
        r.close()
        if r.status_code == 200:
            log.info("✅ WeFlow API 正常")
        elif r.status_code == 401:
            log.error("❌ Access Token 无效")
            state.running = False
            return
        # 其他错误（403 等）不阻断——SSE 循环内部有自动重试
    except requests.exceptions.ConnectionError:
        log.error("❌ 无法连接 WeFlow")
        state.running = False
        return

    while state.running:
        try:
            bridge.listen_sse()
        except Exception as e:
            log.error(f"SSE: {e}")
        if not state.running:
            break
        log.warning("⚠️ SSE 断开，10s 后重连")
        for _ in range(10):
            if not state.running:
                break
            time.sleep(1)

    with state.bridge_lock:
        state.bridge_instance = None


def start_web():
    server = _ThreadingHTTPServer(("127.0.0.1", config.WEB_PORT), WebHandler)
    log.info(f"Web: http://127.0.0.1:{config.WEB_PORT}")
    server.serve_forever()


# ============ 入口 ============

if __name__ == "__main__":
    # 从 config 初始化 state 中需要计算的值
    state._self_id_int = state._wxid_to_int(config.BOT_WXID or "wechat_bot")
    state.group_reply_mode = config.GROUP_REPLY_MODE

    PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.pid")

    def pid_exists(pid):
        try:
            import ctypes
            from ctypes import wintypes
            h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return True

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            if pid_exists(old_pid):
                log.error("⚠️ bridge.pid 已存在")
                sys.exit(1)
            else:
                os.remove(PID_FILE)
        except (ValueError, OSError):
            os.remove(PID_FILE)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        log.info("=" * 50)
        log.info(" WeFlow 微信桥接 (OneBot v11)")
        log.info("=" * 50)
        log.info("Bridge 版本: 2026-06-03 OB11")
        _start_bridge()
        start_web()
    finally:
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
