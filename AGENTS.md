# agents.md — Akasha-WeChat 项目指引

## 项目概述

微信 ↔ AstrBot 桥接器。WeFlow SSE 推送微信消息 → bridge 转 OneBot v11 事件 → AstrBot 调用 LLM 回复 → UIA 自动化发回微信。

```
微信 ←→ WeFlow ──SSE──→ bridge ──WebSocket──→ AstrBot (aiocqhttp)
                            ↑                    ↑
                     main.py 启动         反向 WS 客户端
                     Web 面板 :8766        ws://127.0.0.1:11229/ws
```

入口：`python wechat-weflow-bridge-ob11-public/main.py`
Python 3.10+，仅 Windows（依赖 UIAutomation / ctypes SendInput / CF_HDROP 剪贴板）。

---

## 模块清单

| 文件 | 职责 | 关键点 |
|------|------|--------|
| `main.py` | 入口：多线程 HTTP 服务 + 桥接生命周期 | DPI Awareness、鼠标漫游启停、`_ThreadingHTTPServer` |
| `state.py` | 共享全局变量（线程间） | `running`/`paused`/`run_lock`、`_ob_ws`/`_ob_ws_loop`/`_ob_ws_ready`、`bridge_instance`/`sender_instance`、`group_reply_mode` |
| `config.py` | 加载 `config.json` → 模块级常量 | 首次启动自动从 `config.example.json` 复制；所有值以 `UPPER_CASE` 暴露 |
| `bridge_core.py` | `WeFlowBridge` 类：SSE 监听、消息缓冲、图片/语音处理 | 核心最复杂模块，~700 行 |
| `ob_protocol.py` | OneBot v11 协议：构造事件、推送、处理 AstrBot API 请求 | `make_message_event()`、`push_event()`、`_handle_ob_api()`、`_extract_text()` |
| `ob_client.py` | WebSocket 客户端线程，连接 AstrBot | 独立事件循环（`asyncio.new_event_loop()`）、15s ping 保活、断线自动重连 |
| `senders.py` | `BaseSender` 基类、`WeFlowApiSender`、工厂 `create_sender()` | 引入 `UiaSender`；根据 `SEND_METHOD` 选择 |
| `uia_sender.py` | Windows UIAutomation 发送器 | ~800 行最复杂文件；SendInput 键盘模拟、CF_HDROP 剪贴板粘贴图片、UIA 搜索微信窗口 |
| `web_panel.py` | Web 控制面板 HTML/CSS/JS + API 端点 | 粉白主题；`WebHandler` 处理 GET/POST；在线编辑 config |
| `memory.py` | 对话记忆：`deque(maxlen=N)` + TTL 惰性淘汰 | 纯内存、无持久化、重启清空 |
| `mouse_wanderer.py` | 反风控鼠标漫游 | 独立线程、Bezier 曲线模拟真人轨迹 |
| `config.example.json` | 配置模板 | 所有可配置项的默认值和注释 |
| `requirements.txt` | Python 依赖 | requests、pyautogui、uiautomation、Pillow、websockets |
| `SETUP.md` | 从零搭建教程 | 面向终端用户的部署文档 |
| `README.md` | 项目说明 | 面向用户的功能介绍 |

---

## 数据流（完整路径）

### 入站（微信消息 → AI）

1. **WeFlow SSE** → `WeFlowBridge.listen_sse()` 后台线程持续接收
2. **消息过滤** → `should_ignore()` 丢弃系统消息、空消息、自回复
3. **图片消息** → `process_image_message()` 从 WeFlow REST API 下载原图 → 视觉模型描述（ollama/openai）→ 描述文本注入 `_pending_buffers`
4. **语音消息** → `process_voice_message()` 下载语音文件 → 以 OneBot `record` 段推送
5. **文本消息** → `add_to_buffer()` 入缓冲队列，`BUFFER_SECONDS` 秒后触发合并
6. **合并推送** → `process_sender()` 从 `_pending_buffers` 取合并消息 → 注入对话记忆 → 构造 OneBot 事件 → `push_event()` → WebSocket → AstrBot

### 出站（AI 回复 → 微信）

1. AstrBot 调用 `send_msg` API → WebSocket 消息到达 `ob_client._ob_client_main()` 消息循环
2. `_handle_ob_api(data)` 解析 API 请求 → 提取文本/图片 → 调用 `state.sender_instance.send_text()` / `send_image()`
3. `UiaSender`（默认）→ UIA 查找微信聊天窗口 → 粘贴文本/图片 → 模拟 Enter 发送
4. 回复记录写入 `memory.add_message()` 供后续上下文注入

### 关键数据结构

- `_pending_buffers[sender_id]` — `dict` 含 `messages`(list)、`timer`(threading.Timer)、`processing`(bool)、`image_pending`(bool)
- `_pending_image[talkerId]` — `dict` 含 `caption`(str|None)、`event`(threading.Event) — 图片描述线程写入后通知缓冲线程
- OneBot 事件格式：`{time, self_id, post_type, message_type, sub_type, user_id, group_id, message_id, message, raw_message, sender:{user_id, nickname, card}}`

---

## 线程模型

这是一个多线程 Python 程序，各线程间通过 `state.py` 共享变量通信：

| 线程 | 来源 | 职责 |
|------|------|------|
| 主线程 | `main.py` HTTP Server | `serve_forever()` 处理 Web 面板请求 |
| 桥接线程 | `_bridge_loop()` | 持有 `WeFlowBridge` 实例：SSE 监听循环 + 缓冲管理 |
| OB WS 线程 | `ob_client._run_ob_client()` | 独立 asyncio 事件循环、WebSocket 连接维护 |
| 图片描述线程 | `bridge_core.process_image_message()` | `threading.Thread` 每个图片一条，完成后写入 `_pending_image` 并 set Event |
| 缓冲到期 | `threading.Timer` | 每条新消息创建一个 Timer，到期后调用 `process_sender()` |
| 鼠标漫游 | `mouse_wanderer._run_loop()` | 可选，`WANDERER_ENABLED=true` 时启动 |

**并发安全规则：**
- `state.run_lock` 保护启停操作
- `state._ob_ws_ready` (Event) 用于等待 WS 连接就绪
- `bridge_core._buffer_lock` (Lock) 保护缓冲区操作
- `bridge_core._event_lock` (Lock) 保护事件推送时的 contact 映射
- `memory._lock` 保护记忆存储
- `bridge_core._pending_image` 通过 `threading.Event` 在线程间同步图片描述结果

---

## 约定与模式

### 日志
- 统一 logger name: `"ob11-bridge"`（主模块）/ `"weflow-bridge"`（uia_sender、mouse_wanderer）
- 格式：`%(asctime)s [%(levelname)s] %(message)s`，同时输出到 `bridge.log` 和 stderr

### 配置
- 所有配置从 `config.json` 加载，`config.py` 模块级暴露为 `UPPER_CASE` 常量
- 运行时可变配置（如 `group_reply_mode`）存在 `state.py` 而非 `config.py`
- Web 面板在线编辑直接写入 `config.json` 文件，需重启生效（部分需手动重启桥接）

### 消息发送
- `state.sender_instance` 是全局发送器单例
- 发送器有两种：`UiaSender`（默认，UIA 自动化）和 `WeFlowApiSender`（HTTP API）
- 图片发送走 CF_HDROP 剪贴板格式（非 Ctrl+V 位图粘贴），微信原生支持

### 群聊模式
- `mention` — 仅当消息包含 `@bot_nickname` 或 `@所有人` 时才推送
- `all` — 所有群消息都推送
- `batch` — 推送给 AstrBot 但不立即回复，稍后批量处理

### 消息去重
- 多层去重：rawid（消息唯一 ID）→ 内容哈希 → 自回复检测
- `_processed_rawids` 集合记录已处理的 rawid
- 自回复防护：检查消息发送者 wxid 是否匹配 `BOT_WXID`

---

## 关键不变量

1. **bridge 实例是单例** — `state.bridge_instance` 同时只存在一个，`bridge_lock` 保护
2. **WebSocket 连接是单例** — `state._ob_ws` 保存当前连接，推送前检查 `state._ob_ws_ready`
3. **contact 映射双向** — `_ob_id_to_contact` 维护 OneBot user_id → 微信联系名的映射
4. **缓冲到期仅触发一次** — `processing` 标志防止重复处理
5. **图片描述先于文本推送** — `image_pending` 标志 + `threading.Event` 确保描述文本在合并消息前就绪

---

## 依赖与环境

- **OS**: Windows only（`ctypes.windll`、UIAutomation COM、SendInput API）
- **Python**: 3.10+
- **外部服务**: WeFlow（端口 5031）、AstrBot（WS 端口 11229）、可选 ollama（端口 61000）
- **pip 依赖**: requests, pyautogui, pyperclip, pygetwindow, uiautomation, Pillow, websockets

---

## 常见修改场景

### 新增配置项
1. `config.example.json` 添加默认值
2. `config.py` 添加 `CONFIG_KEY = config.get("config_key", default)`
3. `web_panel.py` HTML 表单添加对应输入控件 + JS 读写逻辑
4. 使用者模块 `import config` 后直接用 `config.CONFIG_KEY`

### 新增消息类型处理
1. `bridge_core.WeFlowBridge` 添加 `process_xxx_message()` 方法
2. 在 `listen_sse()` 的消息分发处添加 `elif msg_type == "xxx":`
3. 如需新 OneBot 消息段，在 `ob_protocol.make_message_event()` 中构造

### 修改发送逻辑
1. 主要修改 `uia_sender.py` 的 `UiaSender` 类
2. 文本发送：`_send_text()` → 查找窗口 → 输入框聚焦 → 粘贴 → Enter
3. 图片发送：`_send_image()` → 查找窗口 → CF_HDROP 复制 → 粘贴 → Enter
4. 注意 `_send_button` 缓存：微信窗口关闭后需重新搜索

### 修改 Web 面板
1. HTML/CSS/JS 全部在 `web_panel.py` 的 `PAGE` 字符串中
2. API 端点通过 `self.path` 路由分发
3. 配置编辑走 `/api/config` GET（读）和 `/api/config/save` POST（写）
4. 日志推送走 `/api/log/stream` SSE 端点

---

## 注意事项

- **不要引入异步与线程混合的新模式** — 现有模型：除了 OB WS 客户端有独立 asyncio 循环外，其余全是同步线程
- **UIA 操作可能失败** — `uia_sender.py` 大量 try/except，UI 元素可能因微信版本更新而变化
- **config.json 可能被 Web 面板并发写入** — `config.py` 的 `load_config()` 确保原子读取
- **图片描述是阻塞的** — 使用独立线程避免阻塞 SSE 消息循环
- **CF_HDROP 剪贴板操作需要清理** — `GlobalFree` 释放全局内存，`EmptyClipboard`/`CloseClipboard` 配对
