"""Loopback-only engine client. Credentials remain on the HA backend."""
import asyncio
import re

from aiohttp import ClientError, ClientTimeout


class EngineError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class EngineClient:
    def __init__(self, session, port, token, physical=False):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid port")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
            raise ValueError("invalid local token")
        if type(physical) is not bool:
            raise ValueError("invalid physical mode")
        self.physical = physical
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
                    raise EngineError(code if code in ("device_conflict", "stale_revision", "stale_instance") else "conflict")
                if response.status != 200:
                    raise EngineError("invalid_request" if response.status == 400 else "engine_error")
                body = await response.json()
                mode = "physical_candidate" if self.physical else "preview"
                if (not isinstance(body, dict) or body.get("schema_version") != 1
                        or body.get("output_mode") != mode
                        or body.get("physical_output_enabled") is not self.physical
                        or (self.physical and (body.get("hardware_qualified") is not False
                            or not isinstance(body.get("enrolled_devices"), list)
                            or not all(isinstance(d, str) for d in body["enrolled_devices"])) )):
                    raise EngineError("incompatible_engine")
                return body
        except (ClientError, asyncio.TimeoutError, ValueError):
            raise EngineError("cannot_connect") from None

    async def state(self):
        return await self._request("GET", "/state")

    async def preview(self, payload):
        if self.physical:
            raise EngineError("incompatible_engine")
        return await self._request("POST", "/preview", payload)

    async def physical_command(self, payload):
        if not self.physical:
            raise EngineError("incompatible_engine")
        return await self._request("POST", "/physical", payload)
