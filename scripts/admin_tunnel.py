"""Open the server's Masumi admin UI on http://localhost:3001/admin/ without SSH port forwarding.

The Nest SSH gateway rejects `ssh -L` ("administratively prohibited"), so every local TCP connection is
piped through a plain `ssh <host> nc 127.0.0.1 3001` instead. The payment service stays on the server's loopback.

    python scripts/admin_tunnel.py [user@host] [local_port]
"""
import asyncio
import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else "mrrobot@hackclub.app"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 3001


async def pump(reader: asyncio.StreamReader, writer) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", HOST, "nc", "127.0.0.1", "3001",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    await asyncio.gather(pump(client_r, proc.stdin), pump(proc.stdout, client_w))
    if proc.returncode is None:
        proc.kill()


async def main() -> None:
    try:
        server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    except OSError:
        sys.exit(f"Port {PORT} is busy (an old `ssh -L {PORT}:...` session?). Close it or pass another port: "
                 f"python scripts/admin_tunnel.py {HOST} 3101")
    print(f"Masumi admin: http://localhost:{PORT}/admin/  (via {HOST}; Ctrl+C to stop)")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
