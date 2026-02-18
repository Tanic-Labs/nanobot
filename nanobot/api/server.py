"""HTTP REST API server for nanobot."""

import asyncio
import json
import os
import signal
import subprocess
from dataclasses import asdict
from typing import Any

from aiohttp import web
from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.config.schema import APIConfig, Config


class APIServer:
    """
    Standalone HTTP REST API server using aiohttp.

    Provides endpoints for external systems (e.g. Supabase Edge Functions)
    to interact with the nanobot agent.
    """

    def __init__(
        self,
        config: Config,
        agent: Any,  # AgentLoop
        cron: Any,  # CronService
    ):
        self.config = config
        self.api_config: APIConfig = config.api
        self.agent = agent
        self.cron = cron
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _check_auth(self, request: web.Request) -> bool:
        """Return True if the request is authorized."""
        token = self.api_config.token
        if not token:
            return True  # auth disabled
        auth = request.headers.get("Authorization", "")
        return auth == f"Bearer {token}"

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # /health is public
        if request.path == "/health":
            return await handler(request)
        if not self._check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def _get_version(self) -> str:
        """Read version from .version file or git."""
        repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        version_file = os.path.join(repo_dir, ".version")
        if os.path.exists(version_file):
            return open(version_file).read().strip()
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "unknown"

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": self._get_version()})

    async def _send(self, request: web.Request) -> web.Response:
        body = await request.json()
        message = body.get("message", "")
        session_key = body.get("session_key", "api:direct")
        stream = body.get("stream", False)

        if not message:
            return web.json_response({"error": "message is required"}, status=400)

        if stream:
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
            await response.prepare(request)

            async for event in self.agent.process_direct_stream(
                content=message,
                session_key=session_key,
                channel="api",
                chat_id=session_key,
            ):
                await response.write(f"data: {event}\n\n".encode())

            return response
        else:
            result = await self.agent.process_direct(
                content=message,
                session_key=session_key,
                channel="api",
                chat_id=session_key,
            )
            return web.json_response({"response": result, "session_key": session_key})

    async def _history(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        session = self.agent.sessions.get_or_create(key)
        return web.json_response({"session_key": key, "messages": session.messages})

    async def _get_config(self, _request: web.Request) -> web.Response:
        """Return config with secrets redacted."""
        return web.json_response({
            "model": self.config.agents.defaults.model,
            "temperature": self.config.agents.defaults.temperature,
            "max_tokens": self.config.agents.defaults.max_tokens,
            "max_tool_iterations": self.config.agents.defaults.max_tool_iterations,
        })

    async def _patch_config(self, request: web.Request) -> web.Response:
        body = await request.json()
        defaults = self.config.agents.defaults
        if "model" in body:
            defaults.model = body["model"]
        if "temperature" in body:
            defaults.temperature = float(body["temperature"])
        if "maxTokens" in body or "max_tokens" in body:
            defaults.max_tokens = int(body.get("maxTokens") or body.get("max_tokens"))

        # Update channel configurations
        if "channels" in body:
            channels_data = body["channels"]
            channels_cfg = self.config.channels
            for ch_name, ch_update in channels_data.items():
                if not isinstance(ch_update, dict):
                    continue
                ch_obj = getattr(channels_cfg, ch_name, None)
                if ch_obj is None:
                    continue
                for key, value in ch_update.items():
                    # Convert camelCase to snake_case for pydantic fields
                    snake_key = key
                    for attr in ("allow_from", "bridge_url", "bridge_token",
                                 "app_id", "app_secret", "encrypt_key",
                                 "verification_token"):
                        if key == attr.replace("_", "") or key == attr:
                            snake_key = attr
                            break
                    if hasattr(ch_obj, snake_key):
                        setattr(ch_obj, snake_key, value)

        from nanobot.config.loader import save_config
        save_config(self.config)
        return web.json_response({"status": "ok"})

    async def _list_crons(self, _request: web.Request) -> web.Response:
        jobs = self.cron.list_jobs(include_disabled=True)
        result = []
        for j in jobs:
            result.append({
                "id": j.id,
                "name": j.name,
                "enabled": j.enabled,
                "schedule": asdict(j.schedule),
                "payload": asdict(j.payload),
                "state": asdict(j.state),
            })
        return web.json_response(result)

    async def _add_cron(self, request: web.Request) -> web.Response:
        body = await request.json()
        from nanobot.cron.types import CronSchedule
        sched_data = body.get("schedule", {})
        schedule = CronSchedule(
            kind=sched_data.get("kind", "every"),
            at_ms=sched_data.get("at_ms") or sched_data.get("atMs"),
            every_ms=sched_data.get("every_ms") or sched_data.get("everyMs"),
            expr=sched_data.get("expr"),
            tz=sched_data.get("tz"),
        )
        job = self.cron.add_job(
            name=body.get("name", "unnamed"),
            schedule=schedule,
            message=body.get("message", ""),
            deliver=body.get("deliver", False),
            channel=body.get("channel"),
            to=body.get("to"),
        )
        return web.json_response({"id": job.id, "name": job.name}, status=201)

    async def _delete_cron(self, request: web.Request) -> web.Response:
        job_id = request.match_info["id"]
        if self.cron.remove_job(job_id):
            return web.json_response({"status": "ok"})
        return web.json_response({"error": "not found"}, status=404)

    async def _list_sessions(self, _request: web.Request) -> web.Response:
        """List all conversation sessions."""
        sessions = self.agent.sessions.list_sessions()
        return web.json_response(sessions)

    async def _list_tools(self, _request: web.Request) -> web.Response:
        """List all registered tools."""
        tools = []
        for name, tool in self.agent.tools._tools.items():
            tools.append({
                "name": name,
                "description": getattr(tool, "description", ""),
            })
        return web.json_response(tools)

    async def _get_memory(self, _request: web.Request) -> web.Response:
        """Read MEMORY.md content."""
        memory = MemoryStore(self.agent.workspace)
        content = memory.read_long_term()
        return web.json_response({"content": content})

    async def _patch_memory(self, request: web.Request) -> web.Response:
        """Update MEMORY.md content."""
        body = await request.json()
        content = body.get("content")
        if content is None:
            return web.json_response({"error": "content is required"}, status=400)
        memory = MemoryStore(self.agent.workspace)
        memory.write_long_term(content)
        return web.json_response({"status": "ok"})

    async def _update(self, _request: web.Request) -> web.Response:
        """Pull latest code from git and restart."""
        logger.info("API: update requested")
        repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "pull", "--ff-only"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            git_output = result.stdout.strip() or result.stderr.strip()
            if result.returncode != 0:
                return web.json_response({"error": "git pull failed", "output": git_output}, status=500)

            pip_result = await asyncio.to_thread(
                subprocess.run,
                ["pip", "install", "-e", "."],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            pip_output = pip_result.stdout.strip() or pip_result.stderr.strip()

            # Schedule restart after response is sent
            async def _delayed_restart():
                await asyncio.sleep(1)
                os.kill(os.getpid(), signal.SIGTERM)
            asyncio.get_event_loop().create_task(_delayed_restart())

            return web.json_response({
                "status": "updating",
                "git": git_output,
                "pip": "ok" if pip_result.returncode == 0 else pip_output,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _restart(self, _request: web.Request) -> web.Response:
        """Send SIGTERM for container restart."""
        logger.info("API: restart requested")
        os.kill(os.getpid(), signal.SIGTERM)
        return web.json_response({"status": "restarting"})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/health", self._health)
        app.router.add_post("/api/sessions/send", self._send)
        app.router.add_get("/api/sessions/{key}/history", self._history)
        app.router.add_get("/api/config", self._get_config)
        app.router.add_patch("/api/config", self._patch_config)
        app.router.add_get("/api/crons", self._list_crons)
        app.router.add_post("/api/crons", self._add_cron)
        app.router.add_delete("/api/crons/{id}", self._delete_cron)
        app.router.add_get("/api/sessions", self._list_sessions)
        app.router.add_get("/api/tools", self._list_tools)
        app.router.add_get("/api/memory", self._get_memory)
        app.router.add_patch("/api/memory", self._patch_memory)
        app.router.add_post("/api/update", self._update)
        app.router.add_post("/api/restart", self._restart)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            self.api_config.host,
            self.api_config.port,
        )
        await site.start()
        logger.info(f"API server listening on {self.api_config.host}:{self.api_config.port}")

        # Keep running until cancelled
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("API server stopped")
