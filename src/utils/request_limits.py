"""A ceiling on how large a request body may be, enforced before it is read.

Flask's ``MAX_CONTENT_LENGTH`` used to do this. It cannot any more: Connexion 3
reads and parses the body in ASGI middleware that runs before Flask, so by the
time the Flask configuration would be consulted the bytes are already in memory
-- which is the thing the limit exists to prevent.

This middleware sits at the outermost edge instead and refuses on the declared
``Content-Length`` alone, before a single byte of body is consumed. A client
that sends no length at all is let through: the parsers downstream still fail on
a body they cannot make sense of, and refusing every chunked upload would break
callers that have done nothing wrong.
"""

import json
from typing import Any, Callable, Dict

import structlog

logger = structlog.get_logger()


class MaxBodySizeMiddleware:
    """Reject a request whose declared body exceeds ``max_bytes`` with 413."""

    def __init__(self, app: Callable, max_bytes: int, error_body: Callable[[], Dict[str, Any]]):
        """
        Args:
            app: The ASGI application to wrap.
            max_bytes: Largest body accepted, in bytes.
            error_body: Builds the response body, so this module does not have
                to know the shape every other error in the app answers with.
        """
        self.app = app
        self.max_bytes = max_bytes
        self.error_body = error_body

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared = None
        for key, value in scope.get("headers") or []:
            if key.lower() == b"content-length":
                try:
                    declared = int(value)
                except (TypeError, ValueError):
                    declared = None
                break

        if declared is not None and declared > self.max_bytes:
            logger.warning("Request body over the limit",
                           declared=declared, limit=self.max_bytes,
                           path=scope.get("path"))
            payload = json.dumps(self.error_body()).encode()
            await send({
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": payload})
            return

        await self.app(scope, receive, send)
