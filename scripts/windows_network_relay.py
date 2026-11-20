#!/usr/bin/env python3
"""Memory-only HTTPS relay from the user's Windows computer to rented SSH host.

Configuration (including SSH password) arrives on stdin; no credentials or
downloaded artifacts are written on the Windows computer. Run as a detached
process. TLS remains end-to-end between server tools and upstream sites.
"""
import base64
import hashlib
import json
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

import paramiko

cfg = json.load(sys.stdin)
deadline = time.monotonic() + cfg.get("lifetime_seconds", 172800)
allowed_suffixes = (
    "github.com", "githubusercontent.com", "githubassets.com",
    "blob.core.windows.net", "lean-lang.org", "lean-cache.cloud",
)


def configured_proxy():
    """Honor the Windows/environment HTTPS proxy, without printing credentials."""
    proxies = urllib.request.getproxies()
    value = cfg.get("upstream_proxy") or proxies.get("https") or proxies.get("http")
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("An HTTP CONNECT upstream proxy is required")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ValueError("Invalid upstream proxy port") from None
    if not 1 <= port <= 65535:
        raise ValueError("Invalid upstream proxy port")
    return parsed, port


upstream_proxy = configured_proxy()


def no_delay(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (AttributeError, OSError):
        pass


def open_upstream(host, port):
    """Return a tunnel socket and any data following its CONNECT response."""
    if upstream_proxy is None:
        sock = socket.create_connection((host, port), timeout=30)
        no_delay(sock)
        return sock, b""
    proxy, proxy_port = upstream_proxy
    sock = socket.create_connection((proxy.hostname, proxy_port), timeout=30)
    try:
        no_delay(sock)
        if proxy.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=proxy.hostname)
        authority = f"{host}:{port}"
        headers = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
        if proxy.username is not None:
            user = urllib.parse.unquote(proxy.username)
            password = urllib.parse.unquote(proxy.password or "")
            auth = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
            headers.append("Proxy-Authorization: Basic " + auth)
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            data = sock.recv(4096)
            if not data:
                raise OSError("Upstream proxy closed before CONNECT response")
            response += data
            if len(response) > 16384:
                raise OSError("Oversized upstream proxy response")
        header, extra = response.split(b"\r\n\r\n", 1)
        status = header.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or not status[0].startswith(b"HTTP/") or status[1] != b"200":
            raise OSError("Upstream proxy refused CONNECT")
        return sock, extra
    except Exception:
        sock.close()
        raise


class PinnedHost(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        actual = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if actual != cfg["host_fingerprint"]:
            raise RuntimeError("SSH host key does not match pinned fingerprint")


def pipe(source, target):
    try:
        while True:
            data = source.recv(131072)
            if not data:
                break
            target.sendall(data)
    except (OSError, EOFError, paramiko.SSHException):
        pass
    finally:
        # One direction ending must not truncate a response still flowing back.
        # The owning serve() closes both endpoints after the response direction ends.
        try:
            target.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def serve(channel):
    upstream = None
    try:
        channel.settimeout(60)
        request = b""
        while b"\r\n\r\n" not in request:
            block = channel.recv(4096)
            if not block:
                return
            request += block
            if len(request) > 16384:
                raise ValueError("oversized CONNECT request")
        header, extra = request.split(b"\r\n\r\n", 1)
        method, authority, protocol = header.split(b"\r\n", 1)[0].decode("ascii").split()
        host, port = authority.rsplit(":", 1)
        host = host.lower().rstrip(".")
        if method != "CONNECT" or port != "443" or not any(
            host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes
        ):
            channel.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        upstream, buffered = open_upstream(host, int(port))
        # A download's request direction can be idle for hours while its response
        # remains active. Per-direction inactivity is not a tunnel failure.
        upstream.settimeout(None)
        channel.settimeout(None)
        channel.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if buffered:
            channel.sendall(buffered)
        if extra:
            upstream.sendall(extra)
        outbound = threading.Thread(target=pipe, args=(channel, upstream), daemon=True)
        outbound.start()
        pipe(upstream, channel)
        outbound.join(timeout=1)
    except Exception:
        try:
            channel.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        except Exception:
            pass
    finally:
        channel.close()
        if upstream is not None:
            upstream.close()


while time.monotonic() < deadline:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHost())
    try:
        client.connect(cfg["host"], port=cfg["port"], username=cfg["user"],
                       password=cfg["password"], timeout=10, banner_timeout=10,
                       auth_timeout=10, allow_agent=False, look_for_keys=False)
        transport = client.get_transport()
        no_delay(transport.sock)
        transport.set_keepalive(15)
        transport.request_port_forward("127.0.0.1", cfg.get("remote_port", 20080))
        while transport.is_active() and time.monotonic() < deadline:
            channel = transport.accept(2)
            if channel is not None:
                threading.Thread(target=serve, args=(channel,), daemon=True).start()
    except Exception:
        pass
    finally:
        client.close()
    if time.monotonic() < deadline:
        time.sleep(min(5, max(0, deadline - time.monotonic())))
