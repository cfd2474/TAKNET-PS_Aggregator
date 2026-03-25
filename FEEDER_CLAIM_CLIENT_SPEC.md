# Feeder claim protocol (TAKNET-PS Aggregator)

This document describes how a **feeder** (Raspberry Pi / Beast client) proves ownership to the aggregator so the dashboard can set **feeder owners** automatically.

## User-side: claim key

1. Each **active** user has a permanent **Feeder claim key** (UUID, lowercase) shown on **Account details** (`/config/users/<your_id>`) in the web UI.
2. The user copies that key into **feeder local configuration** (your product’s settings file, env var, or UI field — implementation is up to the feeder stack).

## Wire protocol (Beast TCP to aggregator)

The feeder opens a **plain TCP** connection to the aggregator **Beast reduce-plus input port** (default **30004**), same as today.

### Backward-compatible behavior

- **Legacy feeders** send **binary Beast data** immediately. The first byte of a Beast frame is **`0x1A`**. The proxy detects this and forwards the stream unchanged to readsb.
- **Claiming feeders** send **one ASCII line first**, then the same Beast binary stream as before.

### Optional metadata line format

Immediately after the TCP connection is established, before any `0x1A` Beast data, feeders may send one or more metadata lines:

```text
TAKNET_FEEDER_CLAIM <uuid>\n
TAKNET_FEEDER_MAC <mac>\n
```

- **Claim keyword:** `TAKNET_FEEDER_CLAIM` — ASCII, case-insensitive (`taknet_feeder_claim` is OK).
- **MAC keyword:** `TAKNET_FEEDER_MAC` — ASCII, case-insensitive (`taknet_feeder_mac` is OK).
- **Separator:** one or more spaces or tabs between keyword and value.
- **UUID value:** standard 8-4-4-4-12 hex form, e.g. `a1b2c3d4-e5f6-7890-abcd-ef1234567890`. Upper/lower hex is accepted and normalized to lowercase.
- **MAC value:** standard 48-bit MAC (examples: `aa:bb:cc:dd:ee:ff` or `aa-bb-cc-dd-ee-ff`). It is normalized to lowercase colon format in DB.
- **Terminator:** Unix newline `\n` (LF). A preceding `\r` in `\r\n` is stripped by the server when matching.

**Examples (valid):**

```text
TAKNET_FEEDER_CLAIM a1b2c3d4-e5f6-7890-abcd-ef1234567890\n
taknet_feeder_claim A1B2C3D4-E5F6-7890-ABCD-EF1234567890\n
TAKNET_FEEDER_MAC AA:BB:CC:DD:EE:FF\n
taknet_feeder_mac aa-bb-cc-dd-ee-ff\n
```

After metadata lines, send **normal Beast binary** exactly as you do today (no extra headers).

### Implementation notes for the feeder client

1. **Buffering:** Build the claim line in memory, send it with `send()` / `write()`, then pipe Beast traffic on the same socket.
2. **TLS:** Not used on the standard Beast port; this is cleartext TCP (same as existing Beast feed).
3. **Reconnections:** Send metadata lines on **every new TCP connection** (each connect/disconnect/retry cycle).
4. **Key storage:** Store the UUID string securely on the feeder (config file readable only by the feeder service user, etc.).
5. **Errors:** If the claim key is unknown or the user is not active, the aggregator **still accepts the feed**; it only skips owner assignment. No error is returned on the TCP stream.

## Aggregator behavior (reference)

- On connect, **beast-proxy** reads optional metadata lines before Beast:
  - If first byte is `0x1A` → treat whole stream as Beast (no metadata).
  - If lines match `TAKNET_FEEDER_CLAIM <uuid>` and/or `TAKNET_FEEDER_MAC <mac>`, values are recorded.
  - Unknown lines are forwarded unchanged.
- Feeder identity upsert prefers `TAKNET_FEEDER_MAC` (stable across IP changes) when available; otherwise it falls back to source IP/hostname behavior.
- If a valid claim UUID was parsed and the feeder’s **`owners_locked`** flag is **0**, the DB **merges** the claimed username into the existing **`owners`** JSON array (appends if missing) rather than erasing other owners.
- If **`owners_locked`** is **1** (set by an admin in the feeder **Edit** form), claim processing **does not change** `owners`.

## Admin override

- Admins edit **Owners** and **Lock owner list** on the feeder detail page in the dashboard.
- **Lock owner list** = `owners_locked`: when enabled, automatic claim updates are disabled until an admin clears the lock.

## Files / ports summary

| Item | Value |
|------|--------|
| Beast TCP host | Aggregator hostname or IP (VPN or public as deployed) |
| Beast TCP port | `30004` (default; match `BEAST_PORT` / compose) |
| Claim line prefix | `TAKNET_FEEDER_CLAIM ` + UUID + `\n` |
| User key source | Web → Account details → Feeder claim key |

## Testing checklist

1. User has active account and copies claim key from Account details.
2. Feeder sends claim line + Beast; aggregator logs show owner assignment (or inspect DB / dashboard **Owners**).
3. Toggle **Lock owner list** on the feeder; change claim key user; reconnect — owners should **not** change while locked.
4. Legacy feeder (no claim line) still connects and counts packets as before.
