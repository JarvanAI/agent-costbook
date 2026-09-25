from __future__ import annotations

import http.client
import io
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urljoin

MAX_BYTES = 2_000_000
MAX_HEADER_BYTES = 65_536
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 20.0
ADDRESS_BUDGET = 8.0
_LOCAL_NAMES = {"localhost", "localhost.localdomain"}


class FetchError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    body: bytes
    pinned_ip: str


@dataclass(frozen=True)
class _Reply:
    status: int
    headers: dict
    body: bytes


def _public_ip(value: str) -> bool:
    try:
        parsed = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    return bool(parsed.is_global) and not parsed.is_multicast


def fetch_public(
    url: str,
    *,
    resolve,
    opener,
    timeout: float = TIMEOUT_SECONDS,
    max_bytes: int = MAX_BYTES,
    _redirects: int = 0,
    _deadline: float | None = None,
) -> FetchResult:
    if _deadline is None:
        _deadline = time.monotonic() + timeout
    if time.monotonic() >= _deadline:
        raise FetchError("timeout")
    if _redirects > MAX_REDIRECTS:
        raise FetchError("too_many_redirects")
    parts = urlsplit(url)
    host = parts.hostname
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or not host
        or "\r" in url
        or "\n" in url
    ):
        raise FetchError("scheme")
    if host.lower().rstrip(".") in _LOCAL_NAMES or host.lower().endswith(".local"):
        raise FetchError("private_address")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [host] if literal is not None else list(resolve(host))
    if not addresses or any(not _public_ip(item) for item in addresses):
        raise FetchError("private_address")
    reply = None
    pinned = addresses[0]
    last_timeout: Exception | None = None
    for candidate in addresses:
        remaining = _deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("timeout")
        try:
            reply = opener(url, candidate, min(ADDRESS_BUDGET, remaining))
            pinned = candidate
            break
        except TimeoutError as exc:
            last_timeout = exc
        except FetchError as exc:
            if exc.code not in {"timeout", "transport"}:
                raise
            last_timeout = exc
    if reply is None:
        raise FetchError("timeout") from last_timeout
    status = reply.status
    headers = {str(key).lower(): value for key, value in dict(reply.headers).items()}
    if status in {301, 302, 303, 307, 308}:
        location = headers.get("location")
        if not location:
            raise FetchError("redirect")
        return fetch_public(
            urljoin(url, location),
            resolve=resolve,
            opener=opener,
            timeout=timeout,
            max_bytes=max_bytes,
            _redirects=_redirects + 1,
            _deadline=_deadline,
        )
    if status == 429:
        raise FetchError("http_429")
    if status >= 400:
        raise FetchError(f"http_{status}")
    body = reply.body
    if len(body) > max_bytes:
        raise FetchError("too_large")
    return FetchResult(url=url, status=status, body=body, pinned_ip=pinned)


def default_resolve(host: str) -> list[str]:
    found: list[str] = []
    for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        address = info[4][0]
        if address not in found:
            found.append(address)
    return found


def default_opener(url: str, pinned_ip: str, timeout: float, *, ssl_context=None) -> _Reply:
    if timeout <= 0:
        raise FetchError("timeout")
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    if " " in path or "\\" in path:
        raise FetchError("scheme")
    deadline = time.monotonic() + timeout
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "User-Agent: agent-costbook/0.3\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    remaining = deadline - time.monotonic()
    raw = socket.create_connection((pinned_ip, port), max(remaining, 0.01))
    try:
        context = ssl_context or ssl.create_default_context()
        tls = context.wrap_socket(raw, server_hostname=host)
        tls.sendall(request)
        return read_http_response(
            tls,
            deadline=deadline,
            max_bytes=MAX_BYTES,
            max_header_bytes=MAX_HEADER_BYTES,
        )
    except TimeoutError as exc:
        raw.close()
        raise FetchError("timeout") from exc
    except OSError as exc:
        raw.close()
        raise FetchError("timeout") from exc


class _LimitedSocket:
    def __init__(self, raw, deadline: float, max_header_bytes: int):
        self._raw = raw
        self._deadline = deadline
        self._max_header_bytes = max_header_bytes
        self._seen = 0
        self._headers_done = False

    def makefile(self, mode="rb", buffering=0, *, encoding=None, errors=None, newline=None):
        return io.BufferedReader(_LimitedRaw(self))

    def finish_headers(self) -> None:
        self._headers_done = True

    def close(self) -> None:
        self._raw.close()


class _LimitedRaw(io.RawIOBase):
    def __init__(self, owner: _LimitedSocket):
        self._owner = owner

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        owner = self._owner
        while True:
            remaining = owner._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("deadline")
            owner._raw.settimeout(min(1.0, remaining))
            try:
                size = owner._raw.recv_into(buffer)
            except TimeoutError:
                if time.monotonic() >= owner._deadline:
                    raise
                continue
            if not owner._headers_done:
                owner._seen += size
                if owner._seen > owner._max_header_bytes:
                    raise FetchError("too_large")
            return size


def read_http_response(sock, *, deadline: float, max_bytes: int, max_header_bytes: int) -> _Reply:
    """Read one HTTP response with the stdlib parser and one total deadline."""
    wrapped = _LimitedSocket(sock, deadline, max_header_bytes)
    response = http.client.HTTPResponse(wrapped)
    try:
        try:
            response.begin()
        except FetchError:
            raise
        except (http.client.HTTPException, TimeoutError, OSError) as exc:
            code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "transport"
            raise FetchError(code) from exc
        wrapped.finish_headers()
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            if time.monotonic() >= deadline:
                raise FetchError("timeout")
            try:
                piece = response.read(min(65536, max_bytes + 1 - total))
            except http.client.IncompleteRead as exc:
                raise FetchError("transport") from exc
            except TimeoutError as exc:
                raise FetchError("timeout") from exc
            if not piece:
                break
            chunks.append(piece)
            total += len(piece)
        if total > max_bytes:
            raise FetchError("too_large")
        headers = {key.lower(): value for key, value in response.getheaders()}
        return _Reply(response.status, headers, b"".join(chunks))
    finally:
        response.close()
