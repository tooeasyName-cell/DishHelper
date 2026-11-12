"""AstrBot supplies platform transport; DishHelper owns routing and answers."""
import asyncio
from contextlib import suppress

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .bridge import BridgeClient, BridgeError, DeliveryJournal, DeliveryManager


@register("astrbot_plugin_dishhelper", "DishHelper", "DishHelper 本机消息桥接", "0.1.0")
class DishHelperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.manager = None
        self.client = None
        self.worker = None
        self.wake = asyncio.Event()

    async def initialize(self):
        if not self.config.get("enabled", False):
            return
        origin = str(self.config.get("bound_origin", "")).strip()
        if not origin or ":GroupMessage:" in origin:
            logger.error("DishHelper 插件需要绑定一个私聊 UMO。")
            return
        try:
            self.client = BridgeClient(self.config.get("base_url", "http://127.0.0.1:8765"), self.config.get("api_token", ""))
            journal = DeliveryJournal(StarTools.get_data_dir("astrbot_plugin_dishhelper") / "transport.json")
        except (ValueError, OSError):
            # Keep the event handler loaded to prevent a configuration failure
            # from silently falling through to AstrBot's default LLM.
            logger.error("DishHelper 插件配置或收发记录无效，请在电脑检查后重载。")
            return
        self.manager = DeliveryManager(self.client, journal, origin, self._send,
                                       chunk_size=self.config.get("chunk_size", 1500))
        self.worker = asyncio.create_task(self._run())

    async def _send(self, origin, text):
        return await self.context.send_message(origin, MessageChain().message(text))

    async def _run(self):
        while True:
            try:
                await self.manager.tick()
            except Exception:
                # Persisted in-flight sends become suspended on the next load.
                logger.error("DishHelper 收发状态处理失败，请在电脑检查插件数据；未自动重新发送。")
                return
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2)
            except TimeoutError:
                pass

    def _bound(self, event):
        return (self.config.get("enabled", False)
                and event.unified_msg_origin == str(self.config.get("bound_origin", "")).strip())

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=10)
    async def receive(self, event: AstrMessageEvent):
        if not self._bound(event):
            return
        event.stop_event()
        if event.message_str.lstrip("/ ").startswith("dishhelper_resend"):
            await event.send(event.plain_result("重发操作需由 AstrBot 管理员执行 /dishhelper_resend。"))
            return
        if self.manager is None or self.worker is None or self.worker.done():
            await event.send(event.plain_result("DishHelper 桥接未就绪，请在电脑检查插件配置并重载。"))
            return
        message_id = getattr(event.message_obj, "message_id", "")
        if not isinstance(message_id, str) or not message_id.strip():
            await event.send(event.plain_result("这条消息缺少平台编号，未提交到 DishHelper。"))
            return
        if not isinstance(event.message_str, str) or not event.message_str.strip():
            await event.send(event.plain_result("DishHelper 手机入口目前接收文字消息。"))
            return
        try:
            self.manager.register(event.unified_msg_origin, message_id, event.message_str)
        except (ValueError, OSError):
            await event.send(event.plain_result("这条消息暂未登记，请在电脑检查插件收发记录。"))
            return
        self.wake.set()

    @filter.command("dishhelper_resend", priority=100)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def resend(self, event: AstrMessageEvent, job_id: str = ""):
        if not self._bound(event):
            return
        event.stop_event()
        if self.manager is None or self.worker is None or self.worker.done():
            await event.send(event.plain_result("DishHelper 桥接未就绪，请在电脑检查并重载插件。"))
            return
        try:
            self.manager.resend(job_id)
        except ValueError as exc:
            await event.send(event.plain_result(str(exc)))
            return
        self.wake.set()
        await event.send(event.plain_result("已恢复已有结果的投递；未重新调用回答模型。未确认的上一段可能已送达。"))

    async def terminate(self):
        if self.worker is not None:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
        if self.client is not None:
            await self.client.close()
