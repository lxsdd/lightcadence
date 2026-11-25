"""Loopback-only engine client. Credentials remain on the HA backend."""
import asyncio
import re

from aiohttp import ClientError, ClientTimeout


class EngineError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class EngineClient:
    def __init__(self, session, port, token):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid port")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
            raise ValueError("invalid local token")
        self.session = session
        self.base = f"http://127.0.0.1:{port}/v1"
        self._token = token

    async def _request(self, method, path, payload=None):
        try:
            async with self.session.request(
                method, self.base + path,
                headers={"Authorization": "Bearer " + self._token},
                json=payload, allow_redirects=False, timeout=ClientTimeout(total=5),
            ) as response:
                if response.status == 401:
                    raise EngineError("unauthorized")
                if response.status == 409:
                    body = await response.json()
                    code = body.get("error")
                    raise EngineError(code if code in ("device_conflict", "stale_revision") else "conflict")
                if response.status != 200:
                    raise EngineError("invalid_request" if response.status == 400 else "engine_error")
                body = await response.json()
                if not isinstance(body, dict) or body.get("schema_version") != 1 or body.get("output_mode") != "preview" or body.get("physical_output_enabled") is not False:
                    raise EngineError("incompatible_engine")
                return body
        except (ClientError, asyncio.TimeoutError, ValueError):
            raise EngineError("cannot_connect") from None

    async def state(self):
        return await self._request("GET", "/state")

    async def preview(self, payload):
        return await self._request("POST", "/preview", payload)
