"""WeChat Article Agent — dual-stack launcher for macOS.

uvicorn -h 0.0.0.0 only binds IPv4; on macOS Safari prefers IPv6 for
'localhost', so http://localhost:8080 fails. This launcher creates both
IPv4+IPv6 sockets so all browsers work.
"""

import os
import sys
import socket
import asyncio
import uvicorn


def _create_listen_socket(family, addr, port, backlog=2048):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind((addr, port))
    s.listen(backlog)
    s.setblocking(False)
    return s


async def main_async():
    port = int(os.getenv("PORT", "8080"))

    socks = [_create_listen_socket(socket.AF_INET, "0.0.0.0", port)]
    try:
        socks.append(_create_listen_socket(socket.AF_INET6, "::", port))
    except OSError:
        pass

    print(f"[launcher] Bound {len(socks)} socket(s) on port {port} (IPv4+IPv6)")

    config = uvicorn.Config("api.app:app", host=None, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve(sockets=socks)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
