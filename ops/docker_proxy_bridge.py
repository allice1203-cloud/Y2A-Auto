#!/usr/bin/env python3
"""Expose a loopback-only HTTP proxy to local Docker networks."""

from __future__ import annotations

import asyncio
import os


LISTEN_HOST = os.environ.get("LISTEN_HOST", "172.17.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "17890"))
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "7890"))


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            UPSTREAM_HOST,
            UPSTREAM_PORT,
        )
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()
        return

    await asyncio.gather(
        copy_stream(client_reader, upstream_writer),
        copy_stream(upstream_reader, client_writer),
    )


async def main() -> None:
    server = await asyncio.start_server(
        handle_client,
        LISTEN_HOST,
        LISTEN_PORT,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
