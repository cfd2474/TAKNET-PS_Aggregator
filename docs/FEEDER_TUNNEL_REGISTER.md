# Feeder tunnel registration (for map/stats via proxy)

When a feeder connects to the aggregator’s tunnel WebSocket (e.g. `wss://aggregator/tunnel`), the first message **must** be a `register` message. The aggregator uses this to route proxy requests to that feeder.

## Register message

Send a JSON object:

```json
{
  "type": "register",
  "feeder_id": "92882-test_test_test",
  "host": "100.85.149.249:8080"
}
```

- **`feeder_id`** (required): Same ID used in the proxy URL (`/feeder/<feeder_id>/`). Usually derived from MLAT site name or hostname (lowercase, spaces to dashes, allowed chars `a-z0-9_-`).
- **`host`** (optional but recommended): The `host:port` the feeder’s web UI listens on (e.g. `100.85.149.249:8080` for the NetBird IP, or `192.168.1.10:8080` for a LAN IP). The aggregator sends this as the **Host** header when proxying. If you omit `host`, the aggregator will try the feeder’s IP from the inputs DB, then `localhost:8080`. For **map (tar1090)** and **stats (graphs1090)** to work through the tunnel, the feeder’s web server must see the same Host it would see when you open `http://<that_host>/` or `http://<that_host>/graphs1090/` directly. Sending the real host (e.g. NetBird IP and port) ensures the feeder serves the same content through the proxy.

## What to send as `host`

Use the same address you use when on VPN or LAN to open the feeder’s map/stats:

- If you use **NetBird** and open `http://100.85.149.249:8080/graphs1090/`, send `"host": "100.85.149.249:8080"`.
- If the feeder’s web UI is on port **8080** and the primary IP is `192.168.1.10`, send `"host": "192.168.1.10:8080"`.

The feeder can obtain this from its own network config (e.g. NetBird status, primary interface, or the same value it would use for a “map URL” in its UI). No trailing slash; just `host:port`.

## Example (pseudo-code on feeder)

```python
# When connecting the tunnel WebSocket:
my_host = "100.85.149.249:8080"   # e.g. from NetBird IP or primary interface
register_msg = {
    "type": "register",
    "feeder_id": "92882-test_test_test",
    "host": my_host
}
ws.send(json.dumps(register_msg))
```

After the aggregator is updated and the feeder sends `host` in `register`, **Map (tar1090)** and **Stats (graphs1090)** under `/feeder/<feeder_id>/` should load correctly through the tunnel.
