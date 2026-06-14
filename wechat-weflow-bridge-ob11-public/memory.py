"""
对话记忆模块：纯内存临时 buffer，按条数上限 + TTL 双保险。

deque(maxlen=N) 自动踢旧；get_history 时惰性淘汰过期条目。
无文件 I/O，无后台线程，重启即清空。
"""

import logging
import threading
import time
from collections import deque

import config

log = logging.getLogger("ob11-bridge")

_store: dict[str, deque] = {}  # conv_key → deque of {"role","content","timestamp"}
_lock = threading.Lock()


def add_message(conv_key: str, role: str, content: str) -> None:
    """添加一条消息。deque maxlen 自动限制条数。"""
    max_count = config.MEMORY_MAX_MESSAGES
    with _lock:
        q = _store.get(conv_key)
        if q is None:
            q = deque(maxlen=max_count)
            _store[conv_key] = q
        q.append({"role": role, "content": content, "timestamp": time.time()})


def _clean(conv_key: str) -> None:
    """惰性淘汰超过 TTL 的旧条目。"""
    q = _store.get(conv_key)
    if q is None:
        return
    cutoff = time.time() - config.MEMORY_TTL_MINUTES * 60
    while q and q[0]["timestamp"] < cutoff:
        q.popleft()


def get_history(conv_key: str, max_count: int | None = None) -> list[dict]:
    """获取会话历史（时间升序），先惰性清理过期条目。"""
    with _lock:
        if conv_key not in _store:
            return []
        _clean(conv_key)
        q = _store[conv_key]
        if not q:
            _store.pop(conv_key, None)
            return []
        items = list(q)
    if max_count is not None:
        items = items[-max_count:]
    return [{"role": m["role"], "content": m["content"]} for m in items]


def format_history(history: list[dict]) -> str | None:
    """将历史记录格式化为注入文本。空历史返回 None。"""
    if not history:
        return None
    lines = ["【对话历史】"]
    for msg in history:
        role_label = "用户" if msg["role"] == "user" else "你"
        lines.append(f"{role_label}：{msg['content']}")
    lines.append("【最新消息】")
    return "\n".join(lines)


def clear_history(conv_key: str | None = None) -> None:
    """清除指定会话历史（conv_key=None 则清除全部）。"""
    with _lock:
        if conv_key is None:
            count = len(_store)
            _store.clear()
            log.info(f"[Memory] 已清除全部对话记忆（{count} 个会话）")
        else:
            _store.pop(conv_key, None)
            log.info(f"[Memory] 已清除会话记忆: {conv_key}")
