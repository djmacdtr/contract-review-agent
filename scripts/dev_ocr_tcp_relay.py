"""Local-only TCP relay for Docker Desktop OCR development.

The relay listens on Windows loopback and forwards raw TCP bytes to the OCR
service reachable from the Windows host. It does not parse or log payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib


async def _copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


async def _handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    target_host: str,
    target_port: int,
) -> None:
    target_writer: asyncio.StreamWriter | None = None
    try:
        target_reader, target_writer = await asyncio.open_connection(target_host, target_port)
        tasks = {
            asyncio.create_task(_copy_stream(client_reader, target_writer)),
            asyncio.create_task(_copy_stream(target_reader, client_writer)),
        }
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except Exception as exc:
        print(f"relay connection failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        for writer in (target_writer, client_writer):
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()


async def _run(args: argparse.Namespace) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: _handle_connection(
            reader,
            writer,
            target_host=args.target_host,
            target_port=args.target_port,
        ),
        host=args.listen_host,
        port=args.listen_port,
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
    print(
        f"OCR TCP relay listening on {addresses}; target={args.target_host}:{args.target_port}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=18017)
    parser.add_argument("--target-host", default="10.50.11.17")
    parser.add_argument("--target-port", type=int, default=80)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
