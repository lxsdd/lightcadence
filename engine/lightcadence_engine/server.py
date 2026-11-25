"""Authenticated loopback-only engine API, version 1."""
import asyncio
import hmac
import json
import os
from pathlib import Path
import re

from aiohttp import web

from .runtime import PhysicalRuntime, PreviewRuntime
from .sessions import Conflict, StaleRevision

RUNTIME = web.AppKey("runtime", PreviewRuntime)


def valid_token(token):
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,}", token) is not None


def error(code, status, **extra):
    return web.json_response({"error": code, **extra}, status=status)


def create_app(token, runtime=None):
    if not valid_token(token):
        raise ValueError("configure a local random token of at least 32 URL-safe characters")
    runtime = runtime if runtime is not None else PreviewRuntime()
    if not isinstance(runtime, PreviewRuntime):
        raise ValueError("invalid runtime")
    physical = isinstance(runtime, PhysicalRuntime)
    expected = ("Bearer " + token).encode("ascii")

    @web.middleware
    async def security(request, handler):
        # No CORS or public listener. Reject browser-origin requests even with a token.
        if "Origin" in request.headers:
            return error("forbidden_origin", 403)
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode("utf-8"), expected):
            return error("unauthorized", 401)
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(middlewares=[security], client_max_size=4 * 1024 * 1024)
    app[RUNTIME] = runtime

    async def lifecycle(_app):
        task = asyncio.create_task(runtime.run())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                await runtime.close()

    async def state(_request):
        return web.json_response(runtime.state())

    async def command(request):
        if request.content_type != "application/json":
            return error("invalid_content_type", 415)
        try:
            payload = await request.json()
            result = await runtime.command(payload) if physical else runtime.command(payload)
        except Conflict as exc:
            return error("device_conflict", 409, owners=exc.owners)
        except StaleRevision:
            return error("stale_revision", 409)
        except OSError:
            return error("transport_unavailable", 503)
        except (ValueError, TypeError, OverflowError):
            return error("invalid_request", 400)
        return web.json_response(result)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/v1/state", state)
    # Separate route is registered only by explicit in-process composition.
    # The installed CLI constructs the preview runtime and cannot switch modes.
    app.router.add_post("/v1/physical" if physical else "/v1/preview", command)
    return app


def main():
    # File supplied by the local owner, never a repository secret or CLI argument.
    token = Path(os.environ["LIGHTCADENCE_TOKEN_FILE"]).read_text(encoding="ascii").strip()
    port = int(os.environ.get("LIGHTCADENCE_PORT", "17842"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid local port")
    web.run_app(create_app(token), host="127.0.0.1", port=port, access_log=None, print=None)


if __name__ == "__main__":
    main()
