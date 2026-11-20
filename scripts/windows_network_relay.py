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
import sys
import threading
import time

import paramiko

cfg = json.load(sys.stdin)
deadline = time.monotonic() + cfg.get("lifetime_seconds", 172800)
allowed_suffixes = (
    "github.com", "githubusercontent.com", "githubassets.com",
    "blob.core.windows.net", "lean-lang.org", "lean-cache.cloud",
)


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
    except (OSError, EOFError):
        pass
    finally:
        for obj in (source, target):
            try:
                obj.shutdown(2)
            except Exception:
                pass
            obj.close()


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
        upstream = socket.create_connection((host, int(port)), timeout=30)
        upstream.settimeout(180)
        channel.settimeout(180)
        channel.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
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
        transport.set_keepalive(15)
        transport.request_port_forward("127.0.0.1", cfg.get("remote_port", 20080))
        next_stop_check = time.monotonic() + 60
        while transport.is_active() and time.monotonic() < deadline:
            channel = transport.accept(2)
            if channel is not None:
                threading.Thread(target=serve, args=(channel,), daemon=True).start()
            if time.monotonic() >= next_stop_check:
                _, stdout, _ = client.exec_command(
                    "test -f /root/autodl-tmp/rh-check/audit/relay-stop", timeout=10)
                if stdout.channel.recv_exit_status() == 0:
                    sys.exit(0)
                next_stop_check = time.monotonic() + 60
    except Exception:
        time.sleep(5)
    finally:
        client.close()
