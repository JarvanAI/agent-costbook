from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit, urljoin

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 5.0
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
) -> FetchResult:
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
        try:
            reply = opener(url, candidate, timeout)
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


def default_opener(url: str, pinned_ip: str, timeout: float) -> _Reply:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    if " " in path or "\\" in path:
        raise FetchError("scheme")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "User-Agent: agent-costbook/0.3\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    raw = socket.create_connection((pinned_ip, port), timeout)
    try:
        raw.settimeout(timeout)
        tls = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        try:
            tls.sendall(request)
            chunks: list[bytes] = []
            total = 0
            while total <= MAX_BYTES + 8192:
                piece = tls.recv(65536)
                if not piece:
                    break
                chunks.append(piece)
                total += len(piece)
        finally:
            tls.close()
    except TimeoutError:
        raw.close()
        raise
    except OSError as exc:
        raw.close()
        raise FetchError("timeout") from exc
    blob = b"".join(chunks)
    head, separator, body = blob.partition(b"\r\n\r\n")
    if not separator:
        raise FetchError("transport")
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError) as exc:
        raise FetchError("transport") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _unchunk(body)
    elif headers.get("content-length", "").isdigit():
        body = body[: int(headers["content-length"])]
    return _Reply(status, headers, body)


def _unchunk(body: bytes) -> bytes:
    chunks: list[bytes] = []
    rest = body
    while rest:
        line, separator, rest = rest.partition(b"\r\n")
        if not separator:
            break
        size_text = line.split(b";", 1)[0].strip()
        if not size_text:
            break
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise FetchError("transport") from exc
        if size == 0:
            break
        chunks.append(rest[:size])
        rest = rest[size + 2 :] if rest[size : size + 2] == b"\r\n" else rest[size:]
    return b"".join(chunks)
