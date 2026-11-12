"""Small local transport journal; no DishHelper business or model logic."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit


class BridgeError(RuntimeError):
    def __init__(self, message, *, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def loopback_url(value):
    parts = urlsplit(str(value))
    if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost")
            or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        raise ValueError("桥接地址只能为本机 http://127.0.0.1:端口 或 http://localhost:端口。")
    if parts.port is None or not 1 <= parts.port <= 65535:
        raise ValueError("请填写有效的本机端口。")
    return value.rstrip("/")


def split_reply(text, limit=1500):
    """Preserve all text and prefer existing paragraph/line boundaries."""
    if not isinstance(text, str):
        raise BridgeError("回答正文格式无效。", permanent=True)
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit + 1)
        if cut < limit // 3:
            cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 3:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:]
    if text:
        parts.append(text)
    return parts


class BridgeClient:
    def __init__(self, base_url, token):
        self.base_url = loopback_url(base_url)
        self.token = str(token).strip()
        if not self.token:
            raise ValueError("请填写与 DishHelper 一致的连接密钥。")
        self.session = None

    async def request(self, method, path, payload=None):
        import aiohttp
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False)
        try:
            async with self.session.request(method, self.base_url + path,
                    headers={"Authorization": "Bearer " + self.token}, json=payload,
                    allow_redirects=False) as response:
                if response.status >= 400 or 300 <= response.status < 400:
                    raise BridgeError("DishHelper 本机接口拒绝请求。",
                                      permanent=300 <= response.status < 500 and response.status not in (408, 429))
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > 1_048_576:
                        raise BridgeError("DishHelper 返回内容过大。", permanent=True)
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    raise ValueError("object required")
                return parsed
        except BridgeError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise BridgeError("DishHelper 本机接口暂不可用。") from None
        except (ValueError, UnicodeError):
            raise BridgeError("DishHelper 本机接口返回格式无效。", permanent=True) from None

    async def close(self):
        if self.session is not None:
            await self.session.close()
            self.session = None


class DeliveryJournal:
    """Only pending transport data and per-message delivery checkpoints."""
    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("records"), dict):
                raise ValueError("DishHelper 插件收发记录格式无效，请在电脑检查插件数据目录。")
            self.records = data["records"]
        else:
            self.records = {}
        recovered = False
        for record in self.records.values():
            if not isinstance(record, dict):
                raise ValueError("DishHelper 插件收发记录格式无效。")
            # The platform may already have accepted this part before interruption.
            if record.get("in_flight") is not None:
                record["status"] = "suspended"
                record["error"] = "delivery_unknown"
                recovered = True
            elif record.get("status") == "paused":
                # Reload after an explicit configuration fix resumes API work,
                # never an uncertain platform send.
                record["status"] = "polling" if record.get("job_id") else "submitting"
                recovered = True
        if recovered:
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "records": self.records}, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def register(self, origin, message_id, text):
        if not all(isinstance(value, str) and value.strip() for value in (origin, message_id, text)):
            raise ValueError("消息来源、编号或文字为空。")
        key = hashlib.sha256(json.dumps([origin, message_id], ensure_ascii=False).encode()).hexdigest()
        if key in self.records:
            return key
        self.records[key] = {"origin": origin, "message_id": message_id, "text": text,
            "job_id": None, "status": "submitting", "next_part": 0, "in_flight": None}
        self.save()
        return key


class DeliveryManager:
    def __init__(self, client, journal, origin, sender, *, chunk_size=1500):
        self.client, self.journal, self.origin, self.sender = client, journal, origin, sender
        self.chunk_size = max(200, min(int(chunk_size), 1500))

    def register(self, origin, message_id, text):
        if origin != self.origin:
            raise ValueError("非绑定会话。")
        return self.journal.register(origin, message_id, text)

    async def tick(self):
        for record in list(self.journal.records.values()):
            if record.get("origin") != self.origin or record.get("status") not in (
                    "submitting", "polling", "sending", "ack_pending"):
                continue
            try:
                await self._advance(record)
                if record.get("status") != "suspended":
                    record.pop("error", None)
            except BridgeError as exc:
                record["error"] = "bridge_unavailable"
                if exc.permanent:
                    record["status"] = "paused"
                self.journal.save()

    async def _advance(self, record):
        if record["status"] == "submitting":
            result = await self.client.request("POST", "/bridge/v1/messages", {
                "message_id": record["message_id"], "origin": record["origin"], "text": record["text"]})
            if not isinstance(result.get("id"), str) or not result["id"]:
                raise BridgeError("任务编号无效。", permanent=True)
            record.update(job_id=result["id"], status="polling")
            record.pop("text", None)
            self.journal.save()
        job_path = "/bridge/v1/jobs/" + quote(record["job_id"], safe="")
        if record["status"] == "polling":
            result = await self.client.request("GET", job_path)
            if result.get("id") != record["job_id"]:
                raise BridgeError("任务编号不匹配。", permanent=True)
            if result.get("delivery") == "sent":
                record.update(status="done", in_flight=None)
                self.journal.save()
                return
            status = result.get("status")
            if status in ("queued", "running"):
                return
            if status not in ("succeeded", "failed", "interrupted"):
                raise BridgeError("任务状态无效。", permanent=True)
            if status == "succeeded":
                reply = result.get("reply")
                if not isinstance(reply, str) or not reply.strip():
                    raise BridgeError("任务回答为空。", permanent=True)
            else:
                reply = "DishHelper 未完成这条消息，可在电脑查看任务记录；本次没有自动重新生成回答。"
            record.update(parts=split_reply(reply, self.chunk_size), status="sending")
            self.journal.save()
        if record["status"] == "sending":
            while record["next_part"] < len(record["parts"]):
                record["in_flight"] = record["next_part"]
                self.journal.save()
                try:
                    sent = await self.sender(record["origin"], record["parts"][record["next_part"]])
                    if sent is False:
                        raise RuntimeError("platform rejected")
                except Exception:
                    record.update(status="suspended", error="delivery_unknown")
                    self.journal.save()
                    try:
                        await self.client.request("POST", job_path + "/delivery", {"status": "failed"})
                    except BridgeError:
                        pass
                    return
                record.update(next_part=record["next_part"] + 1, in_flight=None)
                self.journal.save()
            record["status"] = "ack_pending"
            self.journal.save()
        if record["status"] == "ack_pending":
            await self.client.request("POST", job_path + "/delivery", {"status": "sent"})
            record.update(status="done", in_flight=None)
            record.pop("parts", None)
            self.journal.save()

    def resend(self, job_id=""):
        matches = [record for record in self.journal.records.values()
                   if record.get("origin") == self.origin and record.get("job_id")
                   and (not job_id or record["job_id"] == job_id)]
        if not matches:
            raise ValueError("没有找到可恢复的收发任务。")
        record = matches[-1]
        if record["status"] == "done":
            raise ValueError("该任务已确认发送，不重复发送。")
        if record["status"] not in ("suspended", "paused"):
            raise ValueError("任务仍在处理或投递中。")
        record.update(in_flight=None, status="sending" if record.get("parts") else "polling")
        record.pop("error", None)
        self.journal.save()
        return record["job_id"]
