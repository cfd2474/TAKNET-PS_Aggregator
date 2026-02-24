"""TAKNET-PS Beast TCP Proxy

Intercepts Beast protocol connections from feeders, logs metadata to SQLite,
classifies connections as Tailscale/NetBird/public, and forwards data to readsb.
"""

import asyncio
import json
import os
import signal
import sys
import time

import db
import geoip_helper
import vpn_resolver

LISTEN_HOST        = "0.0.0.0"
LISTEN_PORT        = int(os.environ.get("LISTEN_PORT",        "30004"))
OUTPUT_LISTEN_PORT = int(os.environ.get("OUTPUT_LISTEN_PORT", "30005"))
READSB_HOST        = os.environ.get("READSB_HOST", "readsb")
READSB_PORT        = int(os.environ.get("READSB_PORT", "30006"))
STATS_INTERVAL     = int(os.environ.get("STATS_INTERVAL", "30"))
MLAT_CLIENTS_PATH  = os.environ.get("MLAT_CLIENTS_PATH", "/mlat-work/clients.json")

# Active connections: key = writer id, value = connection info
active_connections = {}
total_connections = 0
start_time = time.time()

# Beast protocol: 0x1a followed by type byte
BEAST_ESCAPE = 0x1A
BEAST_TYPES = {0x31, 0x32, 0x33, 0x34, 0x35}
BEAST_LONG = {0x33, 0x35}  # Mode-S long messages (contain ADS-B/DF17)


def _reclassify_existing_feeders():
    """On startup, re-check all stored feeders against live VPN peer lists.

    Fixes feeders that were previously mis-classified (e.g. NetBird peers
    marked as tailscale because Tailscale CIDR was checked first).
    Only reclassifies when the new classification differs from the stored one.
    """
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT id, ip_address, conn_type FROM feeders"
        ).fetchall()
    except Exception as e:
        print(f"[proxy] Reclassify: DB read failed: {e}")
        return

    # Force a fresh peer list load before checking anything
    vpn_resolver.refresh_caches()

    corrected = 0
    for row in rows:
        feeder_id   = row["id"]
        ip_address  = row["ip_address"]
        stored_type = row["conn_type"]
        new_type    = vpn_resolver.classify_connection(ip_address)
        if new_type != stored_type:
            hostname = vpn_resolver.resolve_hostname(ip_address, new_type)
            conn.execute(
                """UPDATE feeders SET conn_type = ?, hostname = COALESCE(?, hostname),
                   updated_at = datetime('now') WHERE id = ?""",
                (new_type, hostname, feeder_id),
            )
            print(f"[proxy] Reclassified feeder {ip_address}: {stored_type} → {new_type}")
            corrected += 1

    if corrected:
        conn.commit()
        print(f"[proxy] Reclassified {corrected} feeder(s) on startup")
    else:
        print("[proxy] Reclassification: all feeders correctly classified")


def count_beast_frames(data):
    """Count Beast message frames and position-capable messages in raw data."""
    msgs = 0
    positions = 0
    i = 0
    length = len(data)
    while i < length - 1:
        if data[i] == BEAST_ESCAPE:
            next_byte = data[i + 1]
            if next_byte == BEAST_ESCAPE:
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

    conn_type = vpn_resolver.classify_connection(ip_address)
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

    feeder_id = db.upsert_feeder(ip_address, hostname, conn_type, location, lat, lon)
    connection_id = db.log_connection(feeder_id, ip_address)

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

    readsb_reader = None
    readsb_writer = None

    try:
        readsb_reader, readsb_writer = await asyncio.open_connection(READSB_HOST, READSB_PORT)
        print(f"[proxy] Forwarding {ip_address} → readsb:{READSB_PORT}")

        await asyncio.gather(
            forward_stream(reader, readsb_writer, conn_info, "inbound"),
            forward_stream(readsb_reader, writer, conn_info, "outbound"),
        )
    except (ConnectionRefusedError, OSError) as e:
        print(f"[proxy] Cannot connect to readsb: {e}")
    except asyncio.CancelledError:
        pass
    finally:
        unflushed_bytes = conn_info["bytes"] - conn_info["bytes_flushed"]
        unflushed_msgs = conn_info["messages"] - conn_info["messages_flushed"]
        unflushed_pos = conn_info["positions"] - conn_info["positions_flushed"]
        if unflushed_bytes > 0 or unflushed_msgs > 0:
            db.update_feeder_stats(feeder_id, unflushed_bytes, unflushed_msgs, unflushed_pos)

        db.log_disconnection(feeder_id, connection_id, conn_info["bytes"])

        active_connections.pop(id(writer), None)
        display = hostname or location or ip_address
        print(f"[proxy] Disconnected: {display} ({conn_type})")

        if readsb_writer and not readsb_writer.is_closing():
            readsb_writer.close()
        if not writer.is_closing():
            writer.close()


TAR1090_URL = os.environ.get("TAR1090_URL", "http://tar1090:80/data/aircraft.json")


def get_readsb_aircraft_count():
    """Fetch aircraft count from tar1090 HTTP endpoint."""
    try:
        import urllib.request
        with urllib.request.urlopen(TAR1090_URL, timeout=5) as resp:
            data = json.loads(resp.read())
        aircraft = data.get("aircraft", [])
        with_pos = sum(1 for a in aircraft if "lat" in a and "lon" in a)
        return len(aircraft), with_pos
    except Exception:
        return 0, 0


def get_mlat_clients():
    """Read mlat-server clients.json for connected feeders with coordinates.
    Returns dict keyed by source_ip."""
    try:
        with open(MLAT_CLIENTS_PATH, "r") as f:
            data = json.load(f)
        clients = {}
        for user, info in data.items():
            ip = info.get("source_ip")
            if ip:
                clients[ip] = {
                    "name": info.get("user", user),
                    "lat": info.get("lat"),
                    "lon": info.get("lon"),
                    "alt": info.get("alt"),
                    "message_rate": info.get("message_rate", 0),
                    "peer_count": info.get("peer_count", 0),
                }
        return clients
    except (FileNotFoundError, PermissionError):
        return {}
    except Exception as e:
        print(f"[proxy] MLAT clients.json error: {e}")
        return {}


async def stats_flusher():
    """Periodically flush stats for all active connections."""
    cycles = 0
    while True:
        await asyncio.sleep(STATS_INTERVAL)
        cycles += 1

        # Read MLAT client data (includes coordinates)
        mlat_clients = get_mlat_clients()

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

            # Update MLAT status and coordinates from mlat-server
            mlat_info = mlat_clients.get(ip)
            if mlat_info:
                db.update_feeder_mlat(feeder_id, True,
                                      mlat_info.get("lat"),
                                      mlat_info.get("lon"),
                                      mlat_info.get("alt"),
                                      mlat_info.get("name"))
            else:
                db.update_feeder_mlat(feeder_id, False)

        # Get aircraft count from readsb
        total_ac, with_pos = get_readsb_aircraft_count()

        # Mark feeders with no active connection as stale
        # (only if not seen in last 5 minutes — see mark_inactive_feeders)
        active_feeder_ids = {c["feeder_id"] for c in active_connections.values()}
        db.mark_inactive_feeders(active_feeder_ids)

        # Status line
        count = len(active_connections)
        mlat_count = len(mlat_clients)
        uptime = int(time.time() - start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(
            f"[proxy] Status: {count} feeders ({mlat_count} mlat), "
            f"{total_ac} aircraft ({with_pos} with pos), "
            f"uptime {hours}h{minutes}m{seconds}s"
        )

        vpn_resolver.refresh_caches()

        # Auto-purge feeders not seen in 24 hours (skip any that are currently active)
        # Also skip during startup grace period
        if cycles > 3:
            try:
                active_feeder_ids = {c["feeder_id"] for c in active_connections.values()}
                purged = db.purge_old_feeders(hours=24, exclude_ids=active_feeder_ids)
                if purged:
                    print(f"[proxy] Auto-purged {purged} feeder(s) not seen in 24h")
            except Exception as e:
                print(f"[proxy] Auto-purge error: {e}")

        # Check for key-regen drop signals
        try:
            for oid in db.pop_drop_signals():
                _drop_output_ids.add(oid)
                print(f"[output] Drop signal received for output_id={oid}")
        except Exception as e:
            print(f"[output] Drop signal check error: {e}")


# ── Beast Output Listener ─────────────────────────────────────────────────────
# Clients connect, send their API key as the first line, then receive a
# continuous beast raw stream sourced from readsb. Multiple simultaneous
# connections are supported via asyncio tasks.

# Dict of output_id -> set of asyncio.Queue (one per active connection)
_output_clients: dict = {}
# Set of output_ids whose connections should be dropped (regen signal)
_drop_output_ids: set = set()


async def _broadcast_beast_to_output_clients():
    """Continuously read from readsb beast-out port and broadcast to all output clients."""
    while True:
        try:
            reader, _ = await asyncio.open_connection(READSB_HOST, READSB_PORT)
            print(f"[output] Connected to readsb beast-out at {READSB_HOST}:{READSB_PORT}")
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for output_id, queues in list(_output_clients.items()):
                    dead = set()
                    for q in list(queues):
                        try:
                            q.put_nowait(data)
                        except asyncio.QueueFull:
                            dead.add(q)
                    queues -= dead
        except Exception as e:
            print(f"[output] readsb connection lost: {e} — retrying in 5s")
            await asyncio.sleep(5)


async def _handle_output_client(reader, writer):
    """Handle one inbound beast output connection."""
    peer = writer.get_extra_info("peername", ("?", 0))
    try:
        # Read API key (first line, max 256 bytes, 10s timeout)
        try:
            raw_line = await asyncio.wait_for(reader.readline(), timeout=10)
        except asyncio.TimeoutError:
            print(f"[output] {peer[0]} key timeout — closing")
            writer.close()
            return

        raw_key = raw_line.decode("utf-8", errors="replace").strip()
        if not raw_key:
            writer.close()
            return

        # Validate and consume key
        output = db.validate_output_key(raw_key)
        if not output:
            print(f"[output] {peer[0]} invalid or already-used key — rejected")
            writer.write(b"REJECTED\n")
            await writer.drain()
            writer.close()
            return

        if output.get("output_type") != "beast_raw":
            print(f"[output] {peer[0]} key is for non-beast output — rejected")
            writer.write(b"REJECTED\n")
            await writer.drain()
            writer.close()
            return

        output_id = output["id"]
        print(f"[output] {peer[0]} authenticated as '{output.get('name')}' — streaming")
        writer.write(b"OK\n")
        await writer.drain()

        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        if output_id not in _output_clients:
            _output_clients[output_id] = set()
        _output_clients[output_id].add(q)

        try:
            while True:
                # Check if this output's key was regenerated (drop signal)
                if output_id in _drop_output_ids:
                    print(f"[output] {peer[0]} dropped — key regenerated for output {output_id}")
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=5)
                    writer.write(data)
                    await writer.drain()
                except asyncio.TimeoutError:
                    continue  # loop back to check drop signal
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if output_id in _output_clients:
                _output_clients[output_id].discard(q)
            _drop_output_ids.discard(output_id)
            print(f"[output] {peer[0]} disconnected")

    except Exception as e:
        print(f"[output] {peer[0]} error: {e}")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    """Start the Beast TCP proxy server."""
    print("=" * 60)
    print("TAKNET-PS Beast Proxy v1.0.83")
    print(f"  Feeder listener:  {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  Output listener:  {LISTEN_HOST}:{OUTPUT_LISTEN_PORT}")
    print(f"  Forwarding to:    {READSB_HOST}:{READSB_PORT}")
    print(f"  Stats interval:   {STATS_INTERVAL}s")
    print(f"  MLAT clients:     {MLAT_CLIENTS_PATH}")
    print(f"  Tailscale: {'enabled' if vpn_resolver.TAILSCALE_ENABLED else 'disabled'}")
    print(f"  NetBird:   {'enabled' if vpn_resolver.NETBIRD_ENABLED else 'disabled'}")
    print(f"  GeoIP:     {'enabled' if geoip_helper.GEOIP_ENABLED else 'disabled'}")
    print("=" * 60)

    db.init_db()
    _reclassify_existing_feeders()

    # Feeder input server
    feeder_server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    # Beast output server
    output_server = await asyncio.start_server(_handle_output_client, LISTEN_HOST, OUTPUT_LISTEN_PORT)

    asyncio.create_task(stats_flusher())
    asyncio.create_task(_broadcast_beast_to_output_clients())

    async with feeder_server, output_server:
        await asyncio.gather(
            feeder_server.serve_forever(),
            output_server.serve_forever(),
        )


if __name__ == "__main__":
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
