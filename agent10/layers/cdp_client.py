"""
layers/cdp_client.py — direct Chrome DevTools Protocol client.

Why this exists: cua-driver 0.5.3's `page` tool has a known issue where
the daemon proxy fails ("Resource temporarily unavailable (os error 35)")
and falls back to in-process AppleScript, which then times out trying
to use Apple Events to drive Chrome (even after
enable_javascript_apple_events). The CDP path we wanted is never used.

So we speak CDP directly. The page-tool surface the task hook emits
(execute_javascript / get_text / query_dom / click_element) maps 1:1
onto CDP methods. The session writeup's "Electron escape hatch" is
preserved — we still launch Chrome with --remote-debugging-port=9222
and drive its DOM through CDP — we just talk to CDP via websocket
instead of through cua-driver's wrapper.

The trade: this is one of the simplifications the session writeup §13
warns about. A real Session 9 V9 gateway would have a CDP transport
built in. We've built a tiny client here that does only what the three
tasks need.

Two functions:

    eval_js(port, expression) -> Any
        Run a JavaScript expression in the first non-extension page
        and return the result (parsed JSON).

    navigate(port, url) -> bool
        Load `url` in the first non-extension page. Waits for the
        document to reach "complete" readyState before returning.
"""
from __future__ import annotations

import json
import socket
import struct
import time
import urllib.error
import urllib.request
from typing import Any, Optional


class CDPError(RuntimeError):
    """Raised when CDP returns an error or the connection fails."""


def _ws_handshake(host: str, port: int, path: str) -> socket.socket:
    """Open a raw WebSocket connection. We only need a thin client —
    no continuation frames, no compression, no SSL — so writing it
    inline is cheaper than pulling in the `websockets` library.
    """
    s = socket.create_connection((host, port), timeout=10)
    # Minimal WS handshake. The Sec-WebSocket-Key value is just a
    # placeholder; CDP doesn't validate it.
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    s.sendall(req.encode("ascii"))
    # Read the server's response header (terminated by \r\n\r\n).
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise CDPError("CDP websocket closed during handshake")
        buf += chunk
    if b"101" not in buf.split(b"\r\n", 1)[0]:
        raise CDPError(f"CDP websocket handshake failed: {buf[:200]!r}")
    return s


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    """Send one TEXT frame with a masking key (RFC 6455 §5.3 requires
    client→server frames to be masked)."""
    length = len(payload)
    header = bytearray([0x81])  # FIN=1, opcode=1 (text)
    mask_bit = 0x80
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack(">H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", length)
    mask_key = b"\x12\x34\x56\x78"
    header += mask_key
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _recv_frame(sock: socket.socket) -> bytes:
    """Receive one TEXT frame (server→client are unmasked)."""
    def _read(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise CDPError("CDP websocket closed mid-frame")
            buf += chunk
        return buf

    header = _read(2)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read(8))[0]
    return _read(length)


def _list_pages(host: str, port: int) -> list[dict[str, Any]]:
    """GET /json — list every debuggable page (regular tabs, devtools,
    extensions...). Caller filters."""
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/json", timeout=5
        ) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise CDPError(
            f"cannot reach CDP at {host}:{port}: {e}. "
            "Is Chrome running with --remote-debugging-port?"
        )


def _pick_page(host: str, port: int) -> dict[str, Any]:
    """First non-extension, non-devtools, non-chrome-internal page."""
    pages = _list_pages(host, port)
    for p in pages:
        url = p.get("url", "")
        if (p.get("type") == "page"
                and not url.startswith("chrome-extension://")
                and not url.startswith("devtools://")):
            return p
    raise CDPError(
        f"no driveable page found in {len(pages)} debuggable targets"
    )


def _rpc(sock: socket.socket, msg_id: int, method: str,
         params: Optional[dict] = None,
         *, timeout: float = 15.0) -> Any:
    """Issue one Runtime.evaluate (or whatever) and wait for the matched
    response. CDP uses a single message stream; we have to filter by id."""
    sock.settimeout(timeout)
    body = {"id": msg_id, "method": method, "params": params or {}}
    _send_frame(sock, json.dumps(body).encode("utf-8"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = _recv_frame(sock)
        try:
            msg = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if msg.get("id") == msg_id:
            if "error" in msg:
                raise CDPError(
                    f"CDP {method} error: {msg['error']}"
                )
            return msg.get("result", {})
    raise CDPError(f"CDP {method} timed out waiting for id={msg_id}")


def eval_js(port: int, expression: str, *,
            host: str = "127.0.0.1",
            timeout: float = 15.0) -> Any:
    """Run `expression` in the first driveable page and return its value.

    The expression must be a single JS expression — typically wrapped
    in `(() => { ... })()`. Returns whatever Runtime.evaluate's
    `result.value` is (string / number / bool / serialised object).
    """
    page = _pick_page(host, port)
    ws_url = page["webSocketDebuggerUrl"]
    # ws_url is like ws://127.0.0.1:9222/devtools/page/<UUID>
    path = "/" + ws_url.split("/", 3)[3]
    sock = _ws_handshake(host, port, path)
    try:
        result = _rpc(sock, msg_id=1, method="Runtime.evaluate",
                      params={"expression": expression,
                              "returnByValue": True,
                              "awaitPromise": True},
                      timeout=timeout)
    finally:
        sock.close()
    res = result.get("result", {})
    if res.get("subtype") == "error":
        raise CDPError(f"JS threw: {res.get('description', '')[:200]}")
    return res.get("value")


def navigate(port: int, url: str, *,
             host: str = "127.0.0.1",
             timeout: float = 20.0) -> bool:
    """Load `url`, wait for document.readyState=='complete'. Returns
    True if the navigation finished cleanly."""
    # Trigger navigation via JS.
    eval_js(port, f"window.location.href = {url!r}; 'starting';",
            host=host, timeout=8.0)
    # Poll readyState. We re-evaluate each loop because the page swaps
    # contexts mid-navigation.
    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            state = eval_js(port, "document.readyState",
                            host=host, timeout=5.0)
            if state == "complete":
                return True
        except CDPError as e:
            last_err = e
    if last_err:
        raise last_err
    return False


__all__ = ["CDPError", "eval_js", "navigate"]
