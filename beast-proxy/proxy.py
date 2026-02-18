"""TAKNET-PS Beast TCP Proxy

Intercepts Beast protocol connections from feeders, logs metadata to SQLite,
classifies connections as Tailscale/NetBird/public, and forwards data to readsb.
"""

import asyncio
import os
import signal
import sys
import time

import db
import geoip_helper
import vpn_resolver

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "30004"))
READSB_HOST = os.environ.get("READSB_HOST", "readsb")
READSB_PORT = int(os.environ.get("READSB_PORT", "30006"))

# Stats
active_connections = {}
total_connections = 0
start_time = time.time()


async def forward_stream(reader, writer, feeder_id, direction, stats):
    """Forward data between two streams, tracking bytes."""
    try:
        while True:
            data = await reader.read(8192)
            if not data:
                break
            writer.write(data)
            await writer.drain()
            if direction == "inbound":
                stats["bytes"] += len(data)
                stats["msgs"] += 1
                # Periodic stats flush (every 1000 messages)
                if stats["msgs"] % 1000 == 0:
                    db.update_feeder_stats(feeder_id, stats["bytes"])
                    stats["bytes"] = 0
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception as e:
        print(f"[proxy] Forward error ({direction}): {e}")


async def handle_client(reader, writer):
    """Handle a single feeder connection."""
    global total_connections
    total_connections += 1

    peername = writer.get_extra_info("peername")
    if not peername:
        writer.close()
        return

    ip_address = peername[0]
    print(f"[proxy] New connection from {ip_address}")

    # Classify connection type
    conn_type = vpn_resolver.classify_connection(ip_address)

    # Resolve hostname for VPN connections
    hostname = None
    location = None
    lat = None
    lon = None

    if conn_type in ("tailscale", "netbird"):
        hostname = vpn_resolver.resolve_hostname(ip_address, conn_type)
        print(f"[proxy] {ip_address} classified as {conn_type}, hostname: {hostname}")
    else:
        geo = geoip_helper.lookup(ip_address)
        if geo:
            location = geo.get("location")
            lat = geo.get("latitude")
            lon = geo.get("longitude")
        print(f"[proxy] {ip_address} classified as public, location: {location}")

    # Register feeder and log connection
    feeder_id = db.upsert_feeder(ip_address, hostname, conn_type, location, lat, lon)
    connection_id = db.log_connection(feeder_id, ip_address)

    # Track active connection
    conn_info = {
        "feeder_id": feeder_id,
        "connection_id": connection_id,
        "ip": ip_address,
        "conn_type": conn_type,
        "hostname": hostname,
        "connected_at": time.time(),
    }
    active_connections[id(writer)] = conn_info

    # Open forwarding connection to readsb
    readsb_reader = None
    readsb_writer = None
    stats = {"bytes": 0, "msgs": 0}

    try:
        readsb_reader, readsb_writer = await asyncio.open_connection(READSB_HOST, READSB_PORT)
        print(f"[proxy] Forwarding {ip_address} → readsb:{READSB_PORT}")

        # Bidirectional proxy
        await asyncio.gather(
            forward_stream(reader, readsb_writer, feeder_id, "inbound", stats),
            forward_stream(readsb_reader, writer, feeder_id, "outbound", stats),
        )
    except (ConnectionRefusedError, OSError) as e:
        print(f"[proxy] Cannot connect to readsb: {e}")
    except asyncio.CancelledError:
        pass
    finally:
        # Flush remaining stats
        if stats["bytes"] > 0:
            db.update_feeder_stats(feeder_id, stats["bytes"])

        total_bytes = stats.get("bytes", 0)
        db.log_disconnection(feeder_id, connection_id, total_bytes)

        # Clean up
        active_connections.pop(id(writer), None)
        display = hostname or location or ip_address
        print(f"[proxy] Disconnected: {display} ({conn_type})")

        if readsb_writer and not readsb_writer.is_closing():
            readsb_writer.close()
        if not writer.is_closing():
            writer.close()


async def status_reporter():
    """Periodic status output."""
    while True:
        await asyncio.sleep(60)
        count = len(active_connections)
        uptime = int(time.time() - start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(
            f"[proxy] Status: {count} active connections, "
            f"{total_connections} total, uptime {hours}h{minutes}m{seconds}s"
        )
        # Refresh VPN caches periodically
        vpn_resolver.refresh_caches()


async def main():
    """Start the Beast TCP proxy server."""
    print("=" * 60)
    print("TAKNET-PS Beast Proxy v1.0.17")
    print(f"  Listening on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  Forwarding to {READSB_HOST}:{READSB_PORT}")
    print(f"  Tailscale: {'enabled' if vpn_resolver.TAILSCALE_ENABLED else 'disabled'}")
    print(f"  NetBird:   {'enabled' if vpn_resolver.NETBIRD_ENABLED else 'disabled'}")
    print(f"  GeoIP:     {'enabled' if geoip_helper.GEOIP_ENABLED else 'disabled'}")
    print("=" * 60)

    # Initialize database
    db.init_db()

    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)

    # Start status reporter
    asyncio.create_task(status_reporter())

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    # Handle graceful shutdown
    loop = asyncio.new_event_loop()

    def shutdown_handler():
        print("\n[proxy] Shutting down...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_handler)

    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        print("[proxy] Shutdown complete.")
