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

## Request routing hint (`X-Tunnel-Target`)

When the aggregator proxies requests, it now includes a header:

- `X-Tunnel-Target: dashboard` for feeder app pages/APIs (`/dashboard`, `/api/...`, `/settings`, etc.)
- `X-Tunnel-Target: tar1090` for map/stats paths (`/`, `/graphs1090/...`, `/data/...`, `/db2/...`, `/tracks/...`)

Feeder tunnel client should use this hint to route requests to the correct local backend:

- `dashboard` -> feeder app backend
- `tar1090` -> local `:8080` web stack that serves tar1090/graphs1090

This avoids relying on fragile URL rewrites and keeps feeder links unchanged.

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

After the aggregator is updated and the feeder sends `host` in `register` and routes by `X-Tunnel-Target`, **Map (tar1090)** and **Stats (graphs1090)** under `/feeder/<feeder_id>/` should load correctly through the tunnel.

## Troubleshooting

- **Map still shows dashboard / stats “no url”**  
  - Check **tunnel service logs** when the feeder connects. You should see: `Tunnel registered: feeder_id=... host=...`. If `host=(none)`, the feeder is not sending `host` in the register message—fix the feeder client.  
  - **See what the tunnel has stored:** from the aggregator host run  
    `docker exec <tunnel-container-name> curl -s http://localhost:5001/feeders`  
    (e.g. `docker exec taknet-tunnel curl -s http://localhost:5001/feeders`).  
    The response lists each connected feeder’s `feeder_id` and `host`. If `host` is `(none)`, the feeder didn’t send `host`. If the listed `feeder_id` doesn’t match the one in the aggregator URL (e.g. aggregator uses `92882-test_test_test` but feeder registered as `92882-test`), fix the feeder to send the same id the aggregator expects.  
  - The aggregator normalizes `host`: it strips any `http://` or `https://` and adds `:8080` if no port is present. So sending `"host": "http://100.85.149.249:8080"` or `"host": "100.85.149.249"` is fine.  
  - If the feeder’s `feeder_id` uses dashes and the aggregator URL uses underscores (or vice versa), the aggregator tries alternate forms when looking up the host; reconnect the feeder and try again.
