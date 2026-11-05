"""Console response envelope (frontend contract C-1).

Every authenticated console JSON response is::

    {"data": <payload>, "server_time_ms": 123, "deadline_ms": 25000}

``deadline_ms`` is present only when the endpoint handed out a server budget
(C-3). Errors stay flat ``{code, message, retryable}`` and are produced by
``core.errors`` (C-2), so this route never touches a non-2xx body.

The wrapper is implemented as an ``APIRoute`` subclass instead of editing every
return statement: one place enforces the contract, and raw binary/text/file
responses (file download, installer scripts, package zips) pass through
untouched. Public installer endpoints keep their 0.2.x flat shape because the
installer -- not the console SPA -- consumes them.
"""

from __future__ import annotations

import json
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from ..core.clock import now_ms

log = logging.getLogger(__name__)


class ConsoleRoute(APIRoute):
    def get_route_handler(self):  # type: ignore[override]
        original = super().get_route_handler()

        async def envelope_handler(request: Request):
            response = await original(request)
            if response.status_code >= 400:
                return response
            content_type = response.headers.get("content-type", "")
            if not isinstance(response, JSONResponse) and "application/json" not in content_type:
                return response
            if "/public/" in request.url.path or request.url.path.startswith("/api/v1/public/"):
                return response
            try:
                body = json.loads(response.body)
            except (ValueError, TypeError) as exc:
                handler_name = getattr(self.endpoint, "__name__", "?")
                log.warning("console response for %s is not JSON, not wrapping: %s", handler_name, exc)
                return response

            if (
                isinstance(body, dict)
                and set(body).issubset({"data", "server_time_ms", "deadline_ms"})
                and "data" in body
                and "server_time_ms" in body
            ):
                return response

            server_time_ms = now_ms()
            deadline_ms = None
            if isinstance(body, dict):
                payload = dict(body)
                stamped = payload.pop("server_time_ms", None)
                if isinstance(stamped, int):
                    server_time_ms = stamped
                budget = payload.pop("deadline_ms", None)
                if isinstance(budget, int):
                    deadline_ms = budget
            else:
                payload = body

            envelope: dict[str, object] = {"data": payload, "server_time_ms": server_time_ms}
            if deadline_ms is not None:
                envelope["deadline_ms"] = deadline_ms
            rendered = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            response.body = rendered
            response.headers["content-length"] = str(len(rendered))
            if "content-type" not in response.headers:
                response.headers["content-type"] = "application/json"
            return response

        return envelope_handler


__all__ = ["ConsoleRoute"]