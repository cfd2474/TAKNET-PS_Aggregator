#!/usr/bin/env python3
"""
ADSBHub outbound feeder: read SBS (port 30003) from readsb, send CLIENTKEY as first line,
then forward SBS stream to data.adsbhub.org:5001. Reconnects on disconnect.
Run only when ADSBHUB_FEED_ENABLED=true. Requires ADSBHUB_CLIENT_KEY in env.
See ADSBHub Connection Guide: feed format is SBS/BaseStation text; CLIENTKEY must be
sent as the first line after connection.
"""

import asyncio
import json
import os
import time

READSB_HOST = os.environ.get("READSB_HOST", "readsb")
READSB_SBS_PORT = int(os.environ.get("READSB_SBS_PORT", "30003"))
ADSBHUB_HOST = os.environ.get("ADSBHUB_FEED_HOST", "data.adsbhub.org")
ADSBHUB_PORT = int(os.environ.get("ADSBHUB_FEED_PORT", "5001"))
CLIENTKEY = os.environ.get("ADSBHUB_CLIENT_KEY", "").strip()
RECONNECT_DELAY = 15
STATUS_DIR = os.environ.get("ADSBHUB_STATUS_DIR", "/status")


def _write_status(connected: bool):
    try:
        path = os.path.join(STATUS_DIR, "feed.json")
        data = {"connected": connected, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


async def forward():
    while True:
        if not CLIENTKEY:
            _write_status(False)
            print("[adsbhub-feeder] ADSBHUB_CLIENT_KEY not set; cannot feed.", flush=True)
            await asyncio.sleep(RECONNECT_DELAY)
            continue
        try:
            reader_local, writer_local = await asyncio.open_connection(
                READSB_HOST, READSB_SBS_PORT
            )
            writer_remote = None
            try:
                writer_remote = (await asyncio.open_connection(ADSBHUB_HOST, ADSBHUB_PORT))[1]
                # CLIENTKEY must be the first line after connection (Connection Guide)
                writer_remote.write((CLIENTKEY + "\r\n").encode("ascii"))
                await writer_remote.drain()
                _write_status(True)
                print(
                    f"[adsbhub-feeder] Connected: {READSB_HOST}:{READSB_SBS_PORT} -> {ADSBHUB_HOST}:{ADSBHUB_PORT} (CLIENTKEY sent)",
                    flush=True,
                )
                while True:
                    line = await reader_local.readline()
                    if not line:
                        break
                    # Ensure \r\n line endings for ADSBHub (Connection Guide)
                    if not line.endswith(b"\r\n"):
                        if line.endswith(b"\n"):
                            line = line[:-1] + b"\r\n"
                        else:
                            line = line + b"\r\n"
                    writer_remote.write(line)
                    await writer_remote.drain()
            finally:
                _write_status(False)
                if writer_remote:
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
        except (OSError, asyncio.CancelledError, ConnectionResetError, BrokenPipeError) as e:
            _write_status(False)
            print(f"[adsbhub-feeder] Disconnected or connect failed: {e}", flush=True)
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
