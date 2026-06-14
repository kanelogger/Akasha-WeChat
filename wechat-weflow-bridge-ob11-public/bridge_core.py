"""
桥接核心模块：WeFlowBridge 类。

职责：
1. 连接 WeFlow SSE 推送，接收微信消息
2. 消息缓冲合并（BUFFER_SECONDS）
3. 构造 OneBot 事件，推送给 AstrBot
4. 多层消息去重（rawid、内容、自回复）
"""

import json
import base64
import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict
from datetime import datetime

import requests

import state
import config
from ob_protocol import push_event, make_message_event

log = logging.getLogger("ob11-bridge")


# ============ 桥接核心 ============


class WeFlowBridge:
    """WeFlow ↔ AstrBot 桥接器（OneBot v11 版）。"""

    def __init__(self, sender):
        self.sender = sender
        self.processed_ids = set()
        self.start_timestamp = int(time.time())
        self.pending_buffers = {}
        self.buffer_lock = threading.Lock()
        self.chat_histories = defaultdict(list)
        self.contact_map = {}
        self._sse_session = None
        self._recent_seen = {}
        self._sent_recently = {}
        self._sse_event_keys = {}
        self._pending_image = {}  # talkerId → {"caption": None|str, "event": threading.Event()}

    def should_ignore(self, data):
        content = data.get("content", "")
        msg_type = data.get("type", 0) or data.get("msgType", 0)
        if data.get("sourceName", "") in config.BOT_NICKNAMES:
            return True
        if config.BOT_WXID and data.get("talkerId", "") == config.BOT_WXID:
            return True
        if content and ("[表情]" in content):
            return True
        if not content or content.strip() == "":
            return True
        return False

    def add_to_buffer(self, data):
        """将消息加入缓冲区，等待合并后统一推送给 AstrBot。"""
        content = data.get("content", "")
        source_name = data.get("sourceName", "") or data.get("talkerName", "") or "未知"

        if content == "[图片]":
            # 图片消息：即刻创建 buffer 占位条目（防竞态），后台线程下载+描述
            self._ensure_buffer_for_media(data, source_name)
            threading.Thread(target=self.process_image_message,
                           args=(data,), daemon=True).start()
            return

        if content == "[语音]" or data.get("type", 0) == 34:
            # 语音消息：即刻创建 buffer 占位条目，后台线程下载
            self._ensure_buffer_for_media(data, source_name)
            threading.Thread(target=self.process_voice_message,
                           args=(data,), daemon=True).start()
            return

        session_id_data = data.get("sessionId", "") or source_name
        group_name_raw = data.get("groupName", "")
        is_group = (data.get("sessionType", "") == "group") or bool(group_name_raw) or "@chatroom" in session_id_data

        now = time.time()
        if content and content in self._sent_recently and now - self._sent_recently[content] < 120:
            log.info(f"⏭️ 自回复去重跳过: {content[:30]}")
            return

        sender_in_group = data.get("senderName", "") or data.get("sender", "") or data.get("sourceName", "")

        if is_group:
            if state.group_reply_mode == "mention" and not any(f"@{n}" in content for n in config.BOT_NICKNAMES):
                return
            group_raw = group_name_raw or source_name
            base_name = re.sub(r'\s*\(\d+\)\s*$', '', group_raw).strip()
            contact = base_name
        else:
            contact = source_name

        if is_group and state.group_reply_mode == "batch":
            buffer_key = f"__batch__{base_name}"
        elif is_group and sender_in_group:
            buffer_key = f"{session_id_data}_{sender_in_group}"
        else:
            buffer_key = session_id_data

        with self.buffer_lock:
            if buffer_key not in self.pending_buffers:
                self.pending_buffers[buffer_key] = {
                    "messages": [],
                    "timer": None,
                    "timer_version": 0,
                    "processing": False,
                    "contact": contact,
                    "is_group": is_group,
                    "source_name": source_name,
                    "group_name": base_name if is_group else "",
                    "sender_in_group": sender_in_group if is_group else "",
                    "session_id_data": session_id_data,
                }
            entry = self.pending_buffers[buffer_key]
            if is_group and state.group_reply_mode == "batch" and sender_in_group:
                entry["messages"].append(f'成员"{sender_in_group}"在群"{base_name}"中对你说：{content}')
            else:
                entry["messages"].append(content)

            if not entry["processing"]:
                if entry["timer"]:
                    entry["timer"].cancel()
                entry["timer_version"] += 1
                version = entry["timer_version"]
                log.info(f"📩 收到来自 {contact} 的消息，等待 {config.BUFFER_SECONDS}s 后统一推送")
                timer = threading.Timer(config.BUFFER_SECONDS, lambda v=version, sid=buffer_key: self.process_sender(sid, v))
                timer.daemon = True
                timer.start()
                entry["timer"] = timer


    def _ensure_buffer_for_media(self, data, source_name):
        """为图片/语音消息创建 buffer 占位条目（防竞态：后续文字可合并）"""
        session_id_data = data.get("sessionId", "") or source_name
        group_name_raw = data.get("groupName", "")
        is_group = (data.get("sessionType", "") == "group") or bool(group_name_raw) or "@chatroom" in session_id_data
        sender_in_group = data.get("senderName", "") or data.get("sender", "") or data.get("sourceName", "")

        if is_group:
            group_raw = group_name_raw or source_name
            base_name = re.sub(r'\s*\(\d+\)\s*$', '', group_raw).strip()
            contact = base_name
        else:
            contact = source_name

        if is_group and state.group_reply_mode == "batch":
            buffer_key = f"__batch__{contact}"
        elif is_group and sender_in_group:
            buffer_key = f"{session_id_data}_{sender_in_group}"
        else:
            buffer_key = session_id_data

        with self.buffer_lock:
            if buffer_key not in self.pending_buffers:
                self.pending_buffers[buffer_key] = {
                    "messages": [],
                    "timer": None,
                    "timer_version": 0,
                    "processing": False,
                    "contact": contact,
                    "is_group": is_group,
                    "source_name": source_name,
                    "group_name": contact if is_group else "",
                    "sender_in_group": sender_in_group if is_group else "",
                    "session_id_data": session_id_data,
                }
            entry = self.pending_buffers[buffer_key]
            entry["image_pending"] = True

            if not entry["processing"]:
                if entry["timer"]:
                    entry["timer"].cancel()
                entry["timer_version"] += 1
                version = entry["timer_version"]
                timer = threading.Timer(config.BUFFER_SECONDS,
                                        lambda v=version, sid=buffer_key: self.process_sender(sid, v))
                timer.daemon = True
                timer.start()
                entry["timer"] = timer

    def process_sender(self, sender_id, version=None):
        """缓冲到期：通过 OneBot 事件推送给 AstrBot。"""
        with self.buffer_lock:
            if sender_id not in self.pending_buffers:
                return
            entry = self.pending_buffers[sender_id]
            if version is not None and entry.get("timer_version", 0) != version:
                return
            if not entry["messages"]:
                # 图片处理中？重新调度等待
                if entry.get("image_pending") and not entry.get("image_path"):
                    entry["timer_version"] += 1
                    version = entry["timer_version"]
                    timer = threading.Timer(config.BUFFER_SECONDS,
                                            lambda v=version, sid=sender_id: self.process_sender(sid, v))
                    timer.daemon = True
                    timer.start()
                    entry["timer"] = timer
                    log.info(f"⏳ 图片尚未就绪，等待 {config.BUFFER_SECONDS}s...")
                    return
                return
            msgs = entry["messages"].copy()
            entry["messages"] = []
            entry["processing"] = True
            if entry["timer"]:
                entry["timer"].cancel()
                entry["timer"] = None

        contact = entry.get("contact", sender_id)
        is_group = entry.get("is_group", False)
        combined = "\n".join(msgs)
        log.info(f"推送 {len(msgs)} 条消息 [{'群' if is_group else '私'}|{contact}]")

        # 构建 OneBot 事件（user_id 要用发言人身份，不能用群 sessionId）
        if is_group:
            sender_wxid = entry.get("session_id_data", "") + "_" + (entry.get("sender_in_group", "") or entry.get("source_name", ""))
        else:
            sender_wxid = entry.get("session_id_data", sender_id)
        user_id = state._wxid_to_int(sender_wxid)

        if is_group:
            group_id = state._wxid_to_int(entry.get("group_name", contact))
            sender_name = entry.get("sender_in_group", "") or entry.get("source_name", "未知")

            if state.group_reply_mode == "batch":
                # 批处理模式：消息已预格式化好，直接使用
                formatted = combined
            else:
                # 去掉消息中的 @机器人 纯文本，换为 OneBot at 元素
                clean_text = combined
                for nick in config.BOT_NICKNAMES:
                    at_pattern = f"@{nick}"
                    if at_pattern in clean_text:
                        clean_text = clean_text.replace(at_pattern, "").strip()

                formatted = clean_text
                if sender_name:
                    formatted = f'{sender_name}在群{entry.get("group_name", contact)}中说：{clean_text}'

            # 消息段：先 at 机器人（让 aiocqhttp 识别为 @），再发图片（如有），最后发文本
            msg_segments = [
                {"type": "at", "data": {"qq": str(state._self_id_int)}},
            ]
            image_path = entry.get("image_path")
            if image_path and os.path.exists(image_path):
                try:
                    with open(image_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    msg_segments.append({"type": "image", "data": {"file": f"base64://{b64}"}})
                    log.info(f"🖼️ 已附加图片到群聊事件")
                except Exception as e:
                    log.warning(f"读取图片文件失败: {e}")
            msg_segments.append({"type": "text", "data": {"text": f" {formatted}"}})
            event = make_message_event("group", user_id, msg_segments,
                                       group_id=group_id,
                                       group_name=entry.get("group_name", contact),
                                       nickname=sender_name)
        else:
            sender_name = entry.get("source_name", contact)
            msg_segments = [{"type": "text", "data": {"text": combined}}]
            image_path = entry.get("image_path")
            if image_path and os.path.exists(image_path):
                try:
                    with open(image_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    msg_segments.insert(0, {"type": "image", "data": {"file": f"base64://{b64}"}})
                    log.info(f"🖼️ 已附加图片到私聊事件")
                except Exception as e:
                    log.warning(f"读取图片文件失败: {e}")
            event = make_message_event("private", user_id,
                                       msg_segments,
                                       nickname=sender_name)
        if is_group:
            group_id = state._wxid_to_int(entry.get("group_name", contact))
            state._ob_id_to_contact[group_id] = contact
        else:
            state._ob_id_to_contact[user_id] = contact

        sent = push_event(event)
        if sent > 0:
            log.info(f"✅ 已推送至 {sent} 个 AstrBot 客户端 [{contact}]")
        else:
            log.warning(f"⚠️ 无 AstrBot 客户端在线 [{contact}]")

        with self.buffer_lock:
            if sender_id in self.pending_buffers:
                self.pending_buffers[sender_id]["processing"] = False

    def listen_sse(self):
        """连接 WeFlow SSE 推送。"""
        sse_url = f"{config.WE_FLOW_BASE_URL}/api/v1/push/messages?access_token={config.ACCESS_TOKEN}"
        log.info(f"连接 WeFlow 推送服务: {sse_url}")
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}

        try:
            self._sse_session = requests.get(sse_url, headers=headers, stream=True, timeout=None)
            if self._sse_session.status_code != 200:
                log.error(f"连接失败: HTTP {self._sse_session.status_code}")
                return
            log.info("✅ 已连接到 WeFlow 推送")

            for line in self._sse_session.iter_lines(decode_unicode=True):
                if not state.running:
                    break
                if not line:
                    continue
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                elif line.startswith("{"):
                    # WeFlow 部分版本 SSE 消息是裸 JSON 行（不以 data: 开头）
                    data_str = line
                else:
                    # event:, :ping 等 SSE 控制行，忽略
                    continue
                try:
                    data = json.loads(data_str)
                    # WeFlow SSE 版本字段适配
                    if "messageKey" in data and "rawid" not in data:
                        data["rawid"] = data["messageKey"]  # 唯一 ID
                    data.setdefault("timestamp", 0)
                    data.setdefault("talkerId", "")
                    msg_time = data.get("timestamp", 0)
                    # 没有 timestamp 字段的消息（msg_time==0）不跳过
                    if msg_time > 0 and msg_time < self.start_timestamp:
                        continue
                    raw_id = data.get("rawid", "")
                    if raw_id in self.processed_ids:
                        continue
                    self.processed_ids.add(raw_id)
                    if not self.should_ignore(data):
                        content = data.get('content', '')
                        source = data.get('sourceName', '')
                        log.info(f"📩 收到: {source} → {content[:50]}")
                        if content == "[图片]":
                            img_keys = [k for k in data.keys() if not k.startswith("_")]
                            img_vals = {k: data[k] for k in img_keys if k not in ("content", "rawid")}
                            log.info(f"🖼️ SSE图片字段: {json.dumps(img_vals, ensure_ascii=False)}")
                        self.add_to_buffer(data)
                except json.JSONDecodeError:
                    pass

        except requests.exceptions.ConnectionError:
            log.error("无法连接 WeFlow")
        except Exception as e:
            log.error(f"SSE 异常: {e}")
        finally:
            self._sse_session = None

    def _fetch_wechat_image(self, talker: str) -> str | None:
        """从 WeFlow REST API 获取最新图片并保存到本地"""
        try:
            url = f"{config.WE_FLOW_BASE_URL}/api/v1/messages"
            params = {
                "access_token": config.ACCESS_TOKEN,
                "talker": talker,
                "media": "true",
                "limit": 3,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                log.error(f"WeFlow 消息API: HTTP {resp.status_code}")
                return None

            data = resp.json()
            messages = data if isinstance(data, list) else data.get("messages", data.get("data", []))
            if not isinstance(messages, list):
                log.warning(f"消息API 返回非列表结构: keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
                messages = []

            # 诊断：打印前几条消息的字段，排查 mediaUrl 缺失原因
            for i, msg in enumerate(messages[:3]):
                mt = msg.get("mediaType", msg.get("media_type", ""))
                mu = msg.get("mediaUrl", msg.get("media_url", ""))
                keys = [k for k in msg.keys() if not k.startswith("_")][:10]
                log.info(f"  msg[{i}]: mediaType={mt!r} mediaUrl={'✓' if mu else '✗'} keys={keys}")

            found_image = False
            for msg in messages:
                media_type = msg.get("mediaType") or msg.get("media_type") or msg.get("type")
                media_url = msg.get("mediaUrl") or msg.get("media_url")
                if media_type == "image" and media_url:
                    found_image = True
                    sep = "&" if "?" in media_url else "?"
                    dl_url = f"{media_url}{sep}access_token={config.ACCESS_TOKEN}"

                    img_resp = requests.get(dl_url, timeout=30)
                    if img_resp.status_code != 200:
                        continue

                    # 根据 Content-Type 确定扩展名
                    ct = img_resp.headers.get("Content-Type", "")
                    ext = ".jpg"
                    if "png" in ct: ext = ".png"
                    elif "gif" in ct: ext = ".gif"
                    elif "webp" in ct: ext = ".webp"

                    filename = f"wechat_{int(time.time())}{ext}"
                    save_dir = os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_images")
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, filename)

                    with open(save_path, "wb") as f:
                        f.write(img_resp.content)

                    log.info(f"✅ 微信图片已保存: {save_path}")
                    return save_path

            if not found_image:
                log.warning(f"消息列表无图片 mediaUrl (talker={talker})")
            else:
                log.warning(f"图片 mediaUrl 下载失败 (talker={talker})")
            return None
        except Exception as e:
            log.error(f"获取微信图片异常: {e}")
            return None
    def _fetch_wechat_voice(self, talker: str) -> str | None:
        """从 WeFlow REST API 获取最新语音并保存到本地"""
        try:
            url = f"{config.WE_FLOW_BASE_URL}/api/v1/messages"
            params = {
                "access_token": config.ACCESS_TOKEN,
                "talker": talker,
                "media": "true",
                "limit": 3,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                log.error(f"WeFlow 消息API: HTTP {resp.status_code}")
                return None

            data = resp.json()
            messages = data if isinstance(data, list) else data.get("messages", data.get("data", []))
            if not isinstance(messages, list):
                messages = []

            for msg in messages:
                media_type = msg.get("mediaType", "")
                if media_type in ("voice", "audio") and msg.get("mediaUrl"):
                    media_url = msg["mediaUrl"]
                    sep = "&" if "?" in media_url else "?"
                    dl_url = f"{media_url}{sep}access_token={config.ACCESS_TOKEN}"

                    voice_resp = requests.get(dl_url, timeout=30)
                    if voice_resp.status_code != 200:
                        continue

                    ct = voice_resp.headers.get("Content-Type", "")
                    ext = ".amr"
                    if "mp3" in ct or "mpeg" in ct:
                        ext = ".mp3"
                    elif "ogg" in ct:
                        ext = ".ogg"
                    elif "wav" in ct:
                        ext = ".wav"
                    elif "silk" in ct:
                        ext = ".silk"

                    filename = f"wechat_{int(time.time())}{ext}"
                    save_dir = os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_voice")
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, filename)

                    with open(save_path, "wb") as f:
                        f.write(voice_resp.content)

                    log.info(f"✅ 微信语音已保存: {save_path}")
                    return save_path

            log.warning(f"消息列表无语音 mediaUrl (talker={talker})")
            return None
        except Exception as e:
            log.error(f"获取微信语音异常: {e}")
            return None

    def process_image_message(self, data):
        """处理图片消息：从 WeFlow 取图 → ollama 描述 → 注入缓冲区"""
        session_id = data.get("sessionId", "")
        source_name = data.get("sourceName", "") or "未知"
        group_name = data.get("groupName", "")
        rawid = data.get("rawid", "")

        log.info(f"🖼️ 收到图片: {source_name}" +
                 (f" (群:{group_name})" if group_name else ""))

        talker_id = data.get("talkerId", "") or data.get("sessionId", "")
        is_group = bool(group_name) or "@chatroom" in session_id
        sender_in_group = data.get("senderName", "") or data.get("sender", "") or data.get("sourceName", "")

        # 注册待处理的图片（ollama 完成前标记为 pending）
        img_event = threading.Event()
        self._pending_image[talker_id] = {"caption": None, "event": img_event}

        # 计算 buffer 查找 key，与 add_to_buffer 保持一致
        if is_group and state.group_reply_mode == "batch":
            buffer_lookup_key = None  # batch 模式走独立分支
        elif is_group and sender_in_group:
            buffer_lookup_key = f"{session_id}_{sender_in_group}"
        else:
            buffer_lookup_key = talker_id

        try:
            # 取图 + ollama 描述
            image_path = self._fetch_wechat_image(session_id)
            caption = None
            if image_path:
                caption = caption_image_via_ollama(image_path)

            caption_text = caption if caption else None
            if caption_text:
                log.info(f"📝 图片描述: {caption_text[:60]}...")
            else:
                log.info("⚠️ 图片描述失败")
                caption_text = "（图片内容无法描述）"

            # 注入图片描述到缓冲区（buffer 条目已由 _ensure_buffer_for_media 创建）
            with self.buffer_lock:
                self._pending_image[talker_id] = {"caption": caption_text, "event": img_event}

                # 批处理模式用群共享 key
                if is_group and state.group_reply_mode == "batch" and group_name:
                    g_base = re.sub(r'\s*\(\d+\)\s*$', '', group_name).strip()
                    batch_key = f"__batch__{g_base}"
                    if batch_key in self.pending_buffers:
                        entry = self.pending_buffers[batch_key]
                    else:
                        # 防御：buffer 不存在时创建（不应发生）
                        log.warning(f"批处理 buffer 缺失，创建: {batch_key}")
                        self.pending_buffers[batch_key] = {
                            "messages": [], "timer": None, "timer_version": 0,
                            "processing": False, "contact": group_name,
                            "is_group": True, "source_name": source_name,
                            "session_id_data": session_id, "group_name": group_name,
                            "sender_in_group": source_name,
                        }
                        entry = self.pending_buffers[batch_key]
                    entry["messages"].insert(0, f'成员"{source_name}"在群"{group_name}"中对你说：[图片: {caption_text}]')
                    if image_path:
                        entry["image_path"] = image_path
                    entry["image_pending"] = False
                    log.info(f"📝 图片已注入批处理队列")
                else:
                    lookup = buffer_lookup_key or talker_id
                    if lookup in self.pending_buffers:
                        entry = self.pending_buffers[lookup]
                    else:
                        # 防御：buffer 不存在时创建（不应发生）
                        log.warning(f"buffer 缺失，创建: {lookup}")
                        self.pending_buffers[lookup] = {
                            "messages": [], "timer": None, "timer_version": 0,
                            "processing": False,
                            "contact": group_name if is_group and group_name else source_name,
                            "is_group": is_group, "source_name": source_name,
                            "session_id_data": session_id,
                            "group_name": group_name if is_group else "",
                            "sender_in_group": sender_in_group if is_group else "",
                        }
                        entry = self.pending_buffers[lookup]
                    entry["messages"].insert(0, f"[图片: {caption_text}]")
                    if image_path:
                        entry["image_path"] = image_path
                    entry["image_pending"] = False
                    log.info(f"📝 图片已注入待处理文本队列")
        finally:
            # 确保 Event 被设置，且 image_pending 不会卡死 process_sender
            img_event.set()
            lookup = buffer_lookup_key or talker_id
            if lookup:
                with self.buffer_lock:
                    if lookup in self.pending_buffers:
                        self.pending_buffers[lookup]["image_pending"] = False


    def process_voice_message(self, data):
        """处理语音消息：从 WeFlow 下载语音 → 以 OneBot record 段推送"""
        session_id = data.get("sessionId", "")
        source_name = data.get("sourceName", "") or "未知"
        group_name = data.get("groupName", "")
        is_group = bool(group_name) or "@chatroom" in session_id

        log.info(f"🎤 收到语音: {source_name}" +
                 (f" (群:{group_name})" if group_name else ""))

        voice_path = self._fetch_wechat_voice(session_id)
        if not voice_path:
            log.warning(f"⚠️ 语音下载失败 [{source_name}]")
            return

        # 计算 user_id / group_id
        if is_group:
            sender_wxid = f"{session_id}_{source_name}"
            user_id = state._wxid_to_int(sender_wxid)
            group_id = state._wxid_to_int(group_name)
            msg_segments = [
                {"type": "record", "data": {"file": f"file:///{voice_path}"}},
            ]
            event = make_message_event("group", user_id, msg_segments,
                                       group_id=group_id,
                                       group_name=group_name,
                                       nickname=source_name)
            state._ob_id_to_contact[group_id] = group_name
        else:
            user_id = state._wxid_to_int(session_id)
            msg_segments = [
                {"type": "record", "data": {"file": f"file:///{voice_path}"}},
            ]
            event = make_message_event("private", user_id, msg_segments,
                                       nickname=source_name)
            state._ob_id_to_contact[user_id] = source_name

        sent = push_event(event)
        if sent > 0:
            log.info(f"✅ 语音已推送至 AstrBot [{source_name}]")
        else:
            log.warning(f"⚠️ 无 AstrBot 客户端在线 [{source_name}]")

def caption_image_via_ollama(image_path: str) -> str | None:
    """对图片进行文字描述，支持 ollama 和 OpenAI 兼容 API 两种后端。"""
    try:
        import base64
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        if config.IMAGE_CAPTION_PROVIDER == "openai":
            # OpenAI 兼容 API（mimo）
            resp = requests.post(
                f"{config.IMAGE_CAPTION_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.IMAGE_CAPTION_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.IMAGE_CAPTION_MODEL,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": config.IMAGE_CAPTION_PROMPT},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}"
                            }},
                        ],
                    }],
                    "max_tokens": 300,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                caption = resp.json()["choices"][0]["message"]["content"].strip()
                if caption:
                    log.info(f"🖼️ 图片描述: {caption[:80]}...")
                    return caption
            else:
                log.warning(f"mimo 返回 HTTP {resp.status_code}: {resp.text[:200]}")
        else:
            # ollama 原生 API
            resp = requests.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.IMAGE_CAPTION_MODEL,
                    "prompt": config.IMAGE_CAPTION_PROMPT,
                    "images": [img_b64],
                    "stream": False,
                },
                timeout=config.OLLAMA_TIMEOUT,
            )
            if resp.status_code == 200:
                caption = resp.json().get("response", "").strip()
                if caption:
                    log.info(f"🖼️ 图片描述: {caption[:80]}...")
                    return caption
            else:
                log.warning(f"ollama 返回 HTTP {resp.status_code}: {resp.text[:100]}")

    except requests.Timeout:
        log.warning(f"图片描述超时 (30s)")
    except Exception as e:
        log.warning(f"图片描述失败: {e}")
    return None
