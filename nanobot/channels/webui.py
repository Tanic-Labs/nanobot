"""Web UI channel implementation using WebSockets."""

import asyncio
import json
import uuid
from pathlib import Path

import websockets
from websockets.asyncio.server import serve, ServerConnection
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


# Path to the static HTML file
STATIC_DIR = Path(__file__).parent.parent / "static"


class WebUIChannel(BaseChannel):
    """
    Web UI channel that serves a chat interface over WebSocket.

    Provides:
    - A WebSocket endpoint for real-time bidirectional chat
    - An HTTP endpoint that serves the chat UI HTML page
    """

    name = "webui"

    def __init__(self, config, bus: MessageBus):
        super().__init__(config, bus)
        self._server = None
        self._clients: dict[str, ServerConnection] = {}  # session_id -> ws connection

    async def start(self) -> None:
        """Start the WebSocket server."""
        host = getattr(self.config, "host", "0.0.0.0")
        port = getattr(self.config, "port", 18791)

        self._running = True

        self._server = await serve(
            self._ws_handler,
            host,
            port,
            process_request=self._http_handler,
        )

        logger.info(f"WebUI channel listening on http://{host}:{port}")

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message to the connected web client."""
        session_id = msg.chat_id
        ws = self._clients.get(session_id)
        if not ws:
            logger.warning(f"WebUI client {session_id} not connected")
            return

        try:
            payload = json.dumps({
                "type": "message",
                "content": msg.content,
            })
            await ws.send(payload)
        except websockets.ConnectionClosed:
            logger.debug(f"WebUI client {session_id} disconnected during send")
            self._clients.pop(session_id, None)

    async def _ws_handler(self, ws: ServerConnection) -> None:
        """Handle a WebSocket connection."""
        session_id = str(uuid.uuid4())[:8]
        self._clients[session_id] = ws

        logger.info(f"WebUI client connected: {session_id}")

        # Send welcome with session ID
        await ws.send(json.dumps({
            "type": "connected",
            "session_id": session_id,
        }))

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "message":
                    content = data.get("content", "").strip()
                    if not content:
                        continue

                    # Send typing indicator
                    await ws.send(json.dumps({"type": "typing"}))

                    # Forward to the message bus
                    await self._handle_message(
                        sender_id=f"webui:{session_id}",
                        chat_id=session_id,
                        content=content,
                        metadata={"channel": "webui"},
                    )
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.pop(session_id, None)
            logger.info(f"WebUI client disconnected: {session_id}")

    async def _http_handler(self, connection, request):
        """Serve the static HTML UI for non-WebSocket HTTP requests."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None  # Let websockets handle it

        path = request.path
        if path == "/" or path == "/index.html":
            html_path = STATIC_DIR / "index.html"
            if html_path.exists():
                body = html_path.read_bytes()
                return websockets.http11.Response(
                    200,
                    "OK",
                    websockets.datastructures.Headers({
                        "Content-Type": "text/html; charset=utf-8",
                        "Content-Length": str(len(body)),
                    }),
                    body,
                )

        return websockets.http11.Response(
            404,
            "Not Found",
            websockets.datastructures.Headers({"Content-Type": "text/plain"}),
            b"Not Found",
        )
