"""HTTP transfer API accessible on the current local Wi-Fi only.

No cloud, account or third-party server. Each customer receives a random capability URL.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import socket
import threading
from aiohttp import web
from .transfer import CHUNK_SIZE, TransferError, TransferManager

LOG = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent / "web"


@web.middleware
async def guard(request: web.Request, handler):
    try:
        origin = request.headers.get("Origin")
        if origin:
            # Reject cross-site requests even if the browser accidentally sends a token.
            from urllib.parse import urlsplit
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.netloc.lower() != request.host.lower():
                raise TransferError("درخواست از مبدأ غیرمجاز است.", 403)
        result = await handler(request)
    except TransferError as exc:
        result = web.json_response({"error": str(exc)}, status=exc.status)
    except web.HTTPException as exc:
        raise exc
    except Exception:
        LOG.exception("Request error")
        result = web.json_response({"error": "خطای داخلی سرور."}, status=500)
    result.headers["Cache-Control"] = "no-store"
    result.headers["Referrer-Policy"] = "no-referrer"
    result.headers["X-Content-Type-Options"] = "nosniff"
    result.headers["X-Frame-Options"] = "DENY"
    result.headers["Content-Security-Policy"] = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    return result


def create_app(manager: TransferManager) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=CHUNK_SIZE + 1024 * 32)

    def token(request):
        return request.headers.get("X-Session-Token", "")

    async def home(request):
        manager.require(request.query.get("token"))
        return web.FileResponse(WEB / "index.html")

    async def static_file(request):
        filename = request.match_info["name"]
        if filename not in ("app.js", "style.css"):
            raise web.HTTPNotFound()
        return web.FileResponse(WEB / filename)

    async def session(request):
        manager.require(token(request))
        data = manager.snapshot()
        return web.json_response({"code": data["code"], "chunkSize": CHUNK_SIZE,
                                  "maxFileBytes": data["maxFileBytes"],
                                  "maxSessionBytes": data["maxSessionBytes"],
                                  "maxFiles": data["maxFiles"]})

    async def init(request):
        manager.require(token(request))
        data = await request.json()
        if not isinstance(data, dict):
            raise TransferError("درخواست نامعتبر است.")
        result = manager.init_file(token(request), data.get("name"), data.get("size"), data.get("key"))
        return web.json_response(result)

    async def status(request):
        return web.json_response(manager.get_file(token(request), request.match_info["id"]))

    async def put_chunk(request):
        manager.require(token(request))
        try:
            index = int(request.match_info["index"])
        except ValueError:
            raise TransferError("شماره بخش نامعتبر است.") from None
        # Bound in-memory buffering to a single chunk per in-flight request.
        data = await request.read()
        result = await asyncio.to_thread(manager.write_chunk, token(request), request.match_info["id"],
                                         index, data, request.headers.get("X-Chunk-SHA256"))
        return web.json_response(result)

    async def complete(request):
        result = await asyncio.to_thread(manager.finish, token(request), request.match_info["id"])
        return web.json_response(result)

    async def cancel(request):
        result = await asyncio.to_thread(manager.cancel, token(request), request.match_info["id"])
        return web.json_response(result)

    app.router.add_get("/", home)
    app.router.add_get("/static/{name}", static_file)
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/files", init)
    app.router.add_get("/api/files/{id}", status)
    app.router.add_put("/api/files/{id}/chunks/{index}", put_chunk)
    app.router.add_post("/api/files/{id}/complete", complete)
    app.router.add_delete("/api/files/{id}", cancel)
    return app


class LocalServer:
    """Owns an asyncio event loop in a daemon thread; tkinter stays on main thread."""
    def __init__(self, manager: TransferManager, port: int = 8765):
        self.manager = manager
        self.requested_port = port
        self.port = port
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.runner: web.AppRunner | None = None
        self.ready = threading.Event()
        self.error: Exception | None = None

    async def _serve(self):
        app = create_app(self.manager)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        # Try the predictable port first. Use another local port if occupied.
        for port in (self.requested_port, 0):
            try:
                site = web.TCPSite(self.runner, host="0.0.0.0", port=port)
                await site.start()
                self.port = site._server.sockets[0].getsockname()[1]
                return
            except OSError:
                if port == 0:
                    raise

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.ready.clear()
        self.error = None

        def main():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._serve())
            except Exception as exc:
                self.error = exc
                LOG.exception("Unable to start Wi-Fi server")
            finally:
                self.ready.set()
            if not self.error:
                self.loop.run_forever()
                self.loop.run_until_complete(self.runner.cleanup())
            self.loop.close()

        self.thread = threading.Thread(target=main, daemon=True, name="AniWiFi-HTTP")
        self.thread.start()
        if not self.ready.wait(timeout=10):
            raise RuntimeError("Local server start timeout")
        if self.error:
            raise RuntimeError(f"Local server failed: {self.error}")

    def stop(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)
