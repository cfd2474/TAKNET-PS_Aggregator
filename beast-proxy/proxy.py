"""TAKNET-PS Beast TCP Proxy

Intercepts Beast protocol connections from feeders, logs metadata to SQLite,
classifies connections as Tailscale/NetBird/public, and forwards data to readsb.
"""

import asyncio
import json
import os
import re
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
STATS_INTERVAL = int(os.environ.get("STATS_INTERVAL", "30"))
MLAT_LOG_PATH = os.environ.get("MLAT_LOG_PATH", "/mlat-logs/mlat.log")

# Active connections: key = writer id, value = connection info
active_connections = {}
total_connections = 0
start_time = time.time()

# Beast protocol: 0x1a followed by type byte
# Type 0x31 = Mode-AC (2 byte payload)
# Type 0x32 = Mode-S short (7 byte payload)
# Type 0x33 = Mode-S long (14 byte payload) — includes DF17 ADS-B with positions
BEAST_ESCAPE = 0x1A
BEAST_TYPES = {0x31, 0x32, 0x33, 0x34, 0x35}
BEAST_LONG = {0x33, 0x35}  # Mode-S long messages (contain ADS-B/DF17)


def count_beast_frames(data):
    """Count Beast message frames and position-capable messages in raw data.
    Returns (total_messages, position_messages)."""
    msgs = 0
    positions = 0
    i = 0
    length = len(data)
    while i < length - 1:
        if data[i] == BEAST_ESCAPE:
            next_byte = data[i + 1]
            if next_byte == BEAST_ESCAPE:
                # Escaped literal 0x1a, skip both
                i += 2
                continue
            if next_byte in BEAST_TYPES:
                msgs += 1
                if next_byte in BEAST_LONG:
                    positions += 1
                i += 2
                continue
        i += 1
    return msgs, positions


async def forward_stream(reader, writer, conn_info, direction):
    """Forward data between two streams, tracking bytes and Beast messages."""
    try:
        while True:
            data = await reader.read(8192)
            if not data:
                break
            writer.write(data)
            await writer.drain()
            if direction == "inbound":
                conn_info["bytes"] += len(data)
                conn_info["last_data"] = time.time()
                # Count Beast frames
                msgs, positions = count_beast_frames(data)
                conn_info["messages"] += msgs
                conn_info["positions"] += positions
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
        "bytes": 0,
        "bytes_flushed": 0,
        "messages": 0,
        "messages_flushed": 0,
        "positions": 0,
        "positions_flushed": 0,
        "last_data": time.time(),
    }
    active_connections[id(writer)] = conn_info

    # Open forwarding connection to readsb
    readsb_reader = None
    readsb_writer = None

    try:
        readsb_reader, readsb_writer = await asyncio.open_connection(READSB_HOST, READSB_PORT)
        print(f"[proxy] Forwarding {ip_address} → readsb:{READSB_PORT}")

        # Bidirectional proxy
        await asyncio.gather(
            forward_stream(reader, readsb_writer, conn_info, "inbound"),
            forward_stream(readsb_reader, writer, conn_info, "outbound"),
        )
    except (ConnectionRefusedError, OSError) as e:
        print(f"[proxy] Cannot connect to readsb: {e}")
    except asyncio.CancelledError:
        pass
    finally:
        # Flush remaining stats
        unflushed_bytes = conn_info["bytes"] - conn_info["bytes_flushed"]
        unflushed_msgs = conn_info["messages"] - conn_info["messages_flushed"]
        unflushed_pos = conn_info["positions"] - conn_info["positions_flushed"]
        if unflushed_bytes > 0 or unflushed_msgs > 0:
            db.update_feeder_stats(feeder_id, unflushed_bytes, unflushed_msgs, unflushed_pos)

        db.log_disconnection(feeder_id, connection_id, conn_info["bytes"])

        # Clean up
        active_connections.pop(id(writer), None)
        display = hostname or location or ip_address
        print(f"[proxy] Disconnected: {display} ({conn_type})")

        if readsb_writer and not readsb_writer.is_closing():
            readsb_writer.close()
        if not writer.is_closing():
            writer.close()


def get_readsb_aircraft_count():
    """Read readsb aircraft.json for current aircraft count."""
    try:
        with open("/run/readsb/aircraft.json", "r") as f:
            data = json.load(f)
        aircraft = data.get("aircraft", [])
        with_pos = sum(1 for a in aircraft if "lat" in a and "lon" in a)
        return len(aircraft), with_pos
    except Exception:
        return 0, 0


def get_mlat_connected_ips():
    """Parse mlat-server log to find currently connected feeder IPs."""
    mlat_ips = set()
    try:
        with open(MLAT_LOG_PATH, "r") as f:
            # Read last 100 lines for recent connections/disconnections
            lines = f.readlines()[-200:]

        # Track connect/disconnect per IP
        connected = {}
        for line in lines:
            # Match: "Handshake successful" lines with IP
            m = re.search(r'\[(\d+\.\d+\.\d+\.\d+):\d+\] Handshake successful', line)
            if m:
                connected[m.group(1)] = True
                continue
            # Match: disconnect/lost lines
            m = re.search(r'\[(\d+\.\d+\.\d+\.\d+):\d+\].*(lost|disconnect|closed)', line, re.I)
            if m:
                connected[m.group(1)] = False

        mlat_ips = {ip for ip, status in connected.items() if status}
    except (FileNotFoundError, PermissionError):
        pass
    except Exception as e:
        print(f"[proxy] MLAT log parse error: {e}")
    return mlat_ips


async def stats_flusher():
    """Periodically flush stats for all active connections."""
    while True:
        await asyncio.sleep(STATS_INTERVAL)

        # Check which IPs are connected to MLAT
        mlat_ips = get_mlat_connected_ips()

        # Flush counters for all active feeders
        for conn_info in list(active_connections.values()):
            feeder_id = conn_info["feeder_id"]
            ip = conn_info["ip"]

            unflushed_bytes = conn_info["bytes"] - conn_info["bytes_flushed"]
            unflushed_msgs = conn_info["messages"] - conn_info["messages_flushed"]
            unflushed_pos = conn_info["positions"] - conn_info["positions_flushed"]

            if unflushed_bytes > 0 or unflushed_msgs > 0:
                db.update_feeder_stats(feeder_id, unflushed_bytes, unflushed_msgs, unflushed_pos)
                conn_info["bytes_flushed"] = conn_info["bytes"]
                conn_info["messages_flushed"] = conn_info["messages"]
                conn_info["positions_flushed"] = conn_info["positions"]
            else:
                db.touch_feeder(feeder_id)

            # Update MLAT status
            is_mlat = ip in mlat_ips
            db.update_feeder_mlat(feeder_id, is_mlat)

        # Get aircraft count from readsb
        total_ac, with_pos = get_readsb_aircraft_count()

        # Mark feeders with no active connection as stale/offline
        active_feeder_ids = {c["feeder_id"] for c in active_connections.values()}
        db.mark_inactive_feeders(active_feeder_ids)

        # Status line
        count = len(active_connections)
        mlat_count = sum(1 for c in active_connections.values() if c["ip"] in mlat_ips)
        uptime = int(time.time() - start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(
            f"[proxy] Status: {count} feeders ({mlat_count} mlat), "
            f"{total_ac} aircraft ({with_pos} with pos), "
            f"uptime {hours}h{minutes}m{seconds}s"
        )

        # Refresh VPN caches periodically
        vpn_resolver.refresh_caches()


async def main():
    """Start the Beast TCP proxy server."""
    print("=" * 60)
    print("TAKNET-PS Beast Proxy v1.0.24")
    print(f"  Listening on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  Forwarding to {READSB_HOST}:{READSB_PORT}")
    print(f"  Stats interval: {STATS_INTERVAL}s")
    print(f"  MLAT log: {MLAT_LOG_PATH}")
    print(f"  Tailscale: {'enabled' if vpn_resolver.TAILSCALE_ENABLED else 'disabled'}")
    print(f"  NetBird:   {'enabled' if vpn_resolver.NETBIRD_ENABLED else 'disabled'}")
    print(f"  GeoIP:     {'enabled' if geoip_helper.GEOIP_ENABLED else 'disabled'}")
    print("=" * 60)

    # Initialize database
    db.init_db()

    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)

    # Start periodic stats flusher
    asyncio.create_task(stats_flusher())

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
