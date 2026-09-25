import inspect
import socket
import threading
import time

from agent_costbook.fetch_policy import FetchError, fetch_public, read_http_response
from agent_costbook.settings import Settings


class _Response:
    def __init__(self, status, body=b"{}", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}


def test_public_https_is_pinned_and_private_answers_are_never_opened():
    calls = []

    def resolve(host):
        assert host == "catalog.example"
        return ["1.1.1.1", "10.0.0.1"]

    def opener(url, pinned_ip, timeout):
        calls.append((url, pinned_ip, timeout))
        return _Response(200, b"{}")

    try:
        fetch_public("https://catalog.example/models", resolve=resolve, opener=opener)
    except FetchError as exc:
        assert exc.code == "private_address"
    else:
        raise AssertionError("mixed public and private DNS answers were fetched")
    assert calls == []


def test_redirect_to_loopback_or_plaintext_does_not_open_the_target():
    calls = []

    def resolve(host):
        return ["8.8.8.8"]

    def opener(url, pinned_ip, timeout):
        calls.append(url)
        if url.endswith("/start"):
            return _Response(302, b"", {"location": "http://127.0.0.1/secret"})
        return _Response(200, b"secret")

    try:
        fetch_public("https://catalog.example/start", resolve=resolve, opener=opener)
    except FetchError as exc:
        assert exc.code in {"scheme", "private_address"}
    else:
        raise AssertionError("redirect left https")
    assert calls == ["https://catalog.example/start"]


def test_rebinding_redirect_is_resolved_again_and_blocked():
    calls = []

    def resolve(host):
        if host == "catalog.example":
            return ["8.8.8.8"]
        return ["192.168.1.40"]

    def opener(url, pinned_ip, timeout):
        calls.append((url, pinned_ip))
        return _Response(302, b"", {"location": "https://internal.example/admin"})

    try:
        fetch_public("https://catalog.example/models", resolve=resolve, opener=opener)
    except FetchError as exc:
        assert exc.code == "private_address"
    else:
        raise AssertionError("rebinding redirect was followed")
    assert calls == [("https://catalog.example/models", "8.8.8.8")]


def test_a_timed_out_address_falls_through_to_the_next_public_address():
    calls = []

    def resolve(host):
        return ["8.8.8.8", "1.1.1.1"]

    def opener(url, pinned_ip, timeout):
        calls.append(pinned_ip)
        if pinned_ip == "8.8.8.8":
            raise TimeoutError("slow")
        return _Response(200, b"{}")

    result = fetch_public("https://catalog.example/models", resolve=resolve, opener=opener)
    assert result.pinned_ip == "1.1.1.1"
    assert result.body == b"{}"
    assert calls == ["8.8.8.8", "1.1.1.1"]


def test_oversize_body_and_timeout_fail_closed():
    def resolve(host):
        return ["8.8.8.8"]

    def oversized(url, pinned_ip, timeout):
        return _Response(200, b"x" * 32)

    try:
        fetch_public(
            "https://catalog.example/models",
            resolve=resolve,
            opener=oversized,
            max_bytes=8,
        )
    except FetchError as exc:
        assert exc.code == "too_large"
    else:
        raise AssertionError("oversized body was accepted")

    def slow(url, pinned_ip, timeout):
        raise TimeoutError("slow")

    try:
        fetch_public("https://catalog.example/models", resolve=resolve, opener=slow)
    except FetchError as exc:
        assert exc.code == "timeout"
    else:
        raise AssertionError("timeout was accepted")


def _serve(handler):
    gate = socket.socket()
    gate.bind(("127.0.0.1", 0))
    gate.listen(1)
    port = gate.getsockname()[1]

    def run():
        connection, _ = gate.accept()
        try:
            handler(connection)
        finally:
            connection.close()
            gate.close()

    thread = threading.Thread(target=run)
    thread.start()
    return port, thread


def _talk(port: int, deadline: float, *, max_header_bytes: int = 8192):
    client = socket.create_connection(("127.0.0.1", port), 2)
    client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    return read_http_response(
        client,
        deadline=deadline,
        max_bytes=1000,
        max_header_bytes=max_header_bytes,
    )


def test_stdlib_parser_accepts_chunked_body_and_rejects_slow_or_truncated():
    def chunked(connection):
        connection.recv(4096)
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"
        )

    port, thread = _serve(chunked)
    reply = _talk(port, time.monotonic() + 2)
    thread.join(2)
    assert reply.status == 200
    assert reply.body == b"hello"

    def slow(connection):
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\n")
        time.sleep(0.3)
        connection.sendall(b"x")
        time.sleep(1)

    port, thread = _serve(slow)
    try:
        _talk(port, time.monotonic() + 0.45)
    except FetchError as exc:
        assert exc.code == "timeout"
    else:
        raise AssertionError("a slow body finished inside the deadline")
    thread.join(2)

    def truncated(connection):
        connection.recv(4096)
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello"
        )

    port, thread = _serve(truncated)
    try:
        _talk(port, time.monotonic() + 2)
    except FetchError as exc:
        assert exc.code == "transport"
    else:
        raise AssertionError("truncated chunked body was accepted")
    thread.join(2)

    def huge(connection):
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 OK\r\nX-Big: " + (b"a" * 4000) + b"\r\n\r\n")

    port, thread = _serve(huge)
    try:
        _talk(port, time.monotonic() + 2, max_header_bytes=512)
    except FetchError as exc:
        assert exc.code == "too_large"
    else:
        raise AssertionError("oversized headers were accepted")
    thread.join(2)


def test_redirect_chain_shares_one_deadline():
    seen = []

    def resolve(host):
        return ["8.8.8.8"]

    def opener(url, pinned_ip, timeout):
        seen.append(timeout)
        time.sleep(0.15)
        if url.endswith("/start"):
            return _Response(302, b"", {"location": "https://catalog.example/next"})
        return _Response(200, b"ok")

    result = fetch_public("https://catalog.example/start", resolve=resolve, opener=opener, timeout=5)
    assert result.body == b"ok"
    assert seen[1] < seen[0] - 0.1


def test_production_fetch_has_no_private_network_switch():
    assert "allow_private" not in inspect.signature(fetch_public).parameters
    assert set(Settings.__dataclass_fields__) == {"db_path", "admin_token"}
