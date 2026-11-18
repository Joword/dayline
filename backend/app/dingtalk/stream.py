"""DingTalk Stream mode wiring (no public callback URL needed).

The official SDK keeps the long connection and acknowledges each message. We ack at
once and run the command in a background task (model calls can take a while), posting a
short "processing" note first. Replies use our async client - the SDK's own reply helpers
are synchronous and would block the event loop.

    dl dingtalk bot      (compose service `dingtalk`; run exactly one instance)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import dingtalk_stream

from app.dingtalk.bot import BotService, Incoming, deliver

log = logging.getLogger("dayline.dingtalk.stream")


class _Handler(dingtalk_stream.ChatbotHandler):
    def __init__(self, bot: BotService, client: Any):
        super().__init__()
        self.bot = bot
        self.client = client
        self._tasks: set[asyncio.Task] = set()

    async def process(self, callback: dingtalk_stream.CallbackMessage):  # type: ignore[override]
        m = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        incoming = Incoming(
            msg_id=m.message_id or callback.headers.message_id or "",
            text=(m.text.content if m.text else "") or "",
            sender_staff_id=m.sender_staff_id,
            sender_nick=m.sender_nick or "",
            conversation_type=str(m.conversation_type or "1"),
            session_webhook=m.session_webhook,
        )
        task = asyncio.create_task(self._run(incoming))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    async def _run(self, incoming: Incoming) -> None:
        async def progress(text: str) -> None:
            if incoming.session_webhook:
                try:
                    await self.client.reply(
                        incoming.session_webhook, {"msgtype": "text", "text": {"content": text}}
                    )
                except Exception:
                    log.warning("progress reply failed", exc_info=True)

        try:
            replies = await self.bot.handle(incoming, progress=progress)
            await deliver(self.client, incoming, replies)
        except Exception:
            log.exception("bot message failed", extra={"msg_id": incoming.msg_id})


async def run_bot(services: Any) -> None:
    st = services.settings
    if services.dingtalk is None:
        raise RuntimeError("DingTalk is not configured (DAYLINE_DINGTALK_APP_KEY / _APP_SECRET)")
    credential = dingtalk_stream.Credential(st.dingtalk_app_key, st.dingtalk_app_secret.get_secret_value())
    client = dingtalk_stream.DingTalkStreamClient(credential, logger=logging.getLogger("dingtalk_stream"))
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC, _Handler(BotService(services), services.dingtalk)
    )
    log.info("DingTalk Stream bot starting")
    await client.start()  # reconnects by itself
