#!/usr/bin/env python3
"""OpenSSH ProxyCommand using the loopback CONNECT relay; no credentials."""
import os
import socket
import sys
import threading

host, port = sys.argv[1:3]
if host != "ssh.github.com" or port != "443":
    raise SystemExit("This proxy command is restricted to GitHub SSH on port 443")
s = socket.create_connection(("127.0.0.1", 20080), timeout=30)
s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
header = b""
while b"\r\n\r\n" not in header:
    byte = s.recv(1)
    if not byte or len(header) > 8192:
        raise SystemExit("Incomplete proxy response")
    header += byte
if header.split(b"\r\n", 1)[0].split()[1] != b"200":
    raise SystemExit("GitHub SSH CONNECT failed")
s.settimeout(None)

def upload():
    try:
        while data := os.read(0, 65536):
            s.sendall(data)
        s.shutdown(socket.SHUT_WR)
    except OSError:
        pass

threading.Thread(target=upload, daemon=True).start()
while data := s.recv(65536):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
