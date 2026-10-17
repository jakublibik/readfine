"""CSRF middleware that replaces a csrftoken cookie it cannot verify.

starlette_csrf issues a cookie only when the browser has none. A cookie signed
with a different secret (a rotated SECRET_KEY, or another Readfine on the same
host: cookies ignore the port, so the demo on localhost:8000 inherits the dev
server's) is kept forever and fails every POST with 403, with nothing the user
can do short of clearing cookies.

Here such a cookie counts as missing, the way Django treats a malformed one: the
response carries a fresh token. The request that brought the bad cookie still
gets its 403; the page reads the cookie at request time, so the next one passes.
"""

import functools
import http.cookies

from itsdangerous import BadSignature
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send
from starlette_csrf import CSRFMiddleware


class ReissuingCSRFMiddleware(CSRFMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            cookie = Request(scope).cookies.get(self.cookie_name)
            # A missing cookie is the parent's job; it sets a fresh one itself.
            if cookie is not None and not self._cookie_is_valid(cookie):
                # Wrap the outgoing channel rather than override send(): the
                # parent writes its 403 straight to it, bypassing send().
                send = functools.partial(self._send_fresh_cookie, send=send)
        await super().__call__(scope, receive, send)

    async def _send_fresh_cookie(self, message: Message, send: Send) -> None:
        if message["type"] == "http.response.start":
            message.setdefault("headers", [])
            cookie: http.cookies.BaseCookie = http.cookies.SimpleCookie()
            cookie[self.cookie_name] = self._generate_csrf_token()
            cookie[self.cookie_name]["path"] = self.cookie_path
            cookie[self.cookie_name]["secure"] = self.cookie_secure
            cookie[self.cookie_name]["httponly"] = self.cookie_httponly
            cookie[self.cookie_name]["samesite"] = self.cookie_samesite
            if self.cookie_domain is not None:
                cookie[self.cookie_name]["domain"] = self.cookie_domain
            MutableHeaders(scope=message).append("set-cookie", cookie.output(header="").strip())
        await send(message)

    def _cookie_is_valid(self, cookie: str) -> bool:
        try:
            self.serializer.loads(cookie)
        except BadSignature:
            return False
        return True
