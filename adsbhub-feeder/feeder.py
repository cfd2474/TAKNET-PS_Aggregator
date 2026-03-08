#!/usr/bin/env python3
"""
ADSBHub outbound feeder: read Beast from readsb and forward to data.adsbhub.org:5001.
Reconnects on disconnect. Run only when ADSBHUB_FEED_ENABLED=true.
"""

import asyncio
import os
import sys
import time

READSB_HOST = os.environ.get("READSB_HOST", "readsb")
READSB_PORT = int(os.environ.get("READSB_PORT", "30005"))
ADSBHUB_HOST = os.environ.get("ADSBHUB_FEED_HOST", "data.adsbhub.org")
ADSBHUB_PORT = int(os.environ.get("ADSBHUB_FEED_PORT", "5001"))
RECONNECT_DELAY = 15


async def forward():
    while True:
        try:
            reader_local, writer_local = await asyncio.open_connection(READSB_HOST, READSB_PORT)
            reader_remote, writer_remote = await asyncio.open_connection(ADSBHUB_HOST, ADSBHUB_PORT)
        except (OSError, asyncio.CancelledError) as e:
            print(f"[adsbhub-feeder] Connect failed: {e}", flush=True)
            await asyncio.sleep(RECONNECT_DELAY)
            continue
        try:
            print(f"[adsbhub-feeder] Connected: {READSB_HOST}:{READSB_PORT} -> {ADSBHUB_HOST}:{ADSBHUB_PORT}", flush=True)
            while True:
                chunk = await reader_local.read(8192)
                if not chunk:
                    break
                writer_remote.write(chunk)
                await writer_remote.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError, OSError) as e:
            print(f"[adsbhub-feeder] Disconnected: {e}", flush=True)
        finally:
            try:
                writer_remote.close()
                await writer_remote.wait_closed()
            except Exception:
                pass
            try:
                writer_local.close()
                await writer_local.wait_closed()
            except Exception:
                pass
        await asyncio.sleep(RECONNECT_DELAY)


def main():
    if os.environ.get("ADSBHUB_FEED_ENABLED", "").lower() not in ("1", "true", "yes"):
        print("[adsbhub-feeder] ADSBHUB_FEED_ENABLED not set; idling.", flush=True)
        while True:
            time.sleep(3600)
        return
    asyncio.run(forward())


if __name__ == "__main__":
    main()
