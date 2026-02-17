"""WebUI channel: browser-based chat over WebSocket."""

import asyncio
import json
from pathlib import Path
from typing import Any

import websockets
from websockets.http11 import Request, Response
from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WebUIConfig


# Locate the bundled static/index.html
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class WebUIChannel(BaseChannel):
    """
    WebSocket-based chat channel that also serves a single-page web UI.

    - GET / serves static/index.html
    - WebSocket connections on any path handle real-time chat
    """

    name = "webui"

    def __init__(self, config: WebUIConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.host = config.host
        self.port = config.port
        self._server: Any = None
        self._clients: dict[websockets.WebSocketServerProtocol, str] = {}  # ws -> chat_id

    # ------------------------------------------------------------------
    # HTTP handler — serves index.html for plain GET requests
    # ------------------------------------------------------------------

    async def _http_handler(self, connection, request: Request) -> Response | None:
        """Handle HTTP requests (serve static files). Return None to upgrade to WS."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None  # let websockets handle it
        # Serve index.html
        index = _STATIC_DIR / "index.html"
        if index.exists():
            body = index.read_bytes()
            return Response(200, "OK", websockets.Headers({"Content-Type": "text/html"}), body)
        return Response(404, "Not Found", websockets.Headers(), b"Not found")

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _ws_handler(self, ws: websockets.WebSocketServerProtocol) -> None:
        chat_id = f"webui:{id(ws)}"
        self._clients[ws] = chat_id
        logger.info(f"WebUI client connected: {chat_id}")

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    content = data.get("message") or data.get("content", "")
                    if not content:
                        continue
                    await self._handle_message(
                        sender_id="webui-user",
                        chat_id=chat_id,
                        content=content,
                    )
                except json.JSONDecodeError:
                    # Treat plain text as message
                    await self._handle_message(
                        sender_id="webui-user",
                        chat_id=chat_id,
                        content=str(raw),
                    )
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.pop(ws, None)
            logger.info(f"WebUI client disconnected: {chat_id}")

    # ------------------------------------------------------------------
    # BaseChannel interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._server = await websockets.serve(
            self._ws_handler,
            self.host,
            self.port,
            process_request=self._http_handler,
        )
        logger.info(f"WebUI channel listening on {self.host}:{self.port}")
        await self._server.wait_closed()

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send message back to the matching WebSocket client."""
        payload = json.dumps({"type": "message", "content": msg.content})
        closed = []
        for ws, chat_id in self._clients.items():
            if chat_id == msg.chat_id:
                try:
                    await ws.send(payload)
                except websockets.ConnectionClosed:
                    closed.append(ws)
        for ws in closed:
            self._clients.pop(ws, None)
