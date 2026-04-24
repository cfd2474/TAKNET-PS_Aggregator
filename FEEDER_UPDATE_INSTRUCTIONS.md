# Feeder AI Instructions: Support FEEDER_UUID in Beast Claim Proxy

We are updating the `TAKNET-PS_Aggregator` to allow non-NetBird feeders connecting via public IPs to be identified by their UDID (`FEEDER_UUID`) or MAC address, rather than just their IP address. This will prevent duplicate feeder entries when their IP addresses change.

To complete this feature, the `TAKNET-PS_ADS-B_Feeder` repository must be updated to always transmit its `FEEDER_UUID` when communicating with the Aggregator.

Please implement the following changes in the `TAKNET-PS_ADS-B_Feeder` repository:

## 1. Modify `scripts/config_builder.py`

### Update `taknet_beast_uses_claim_proxy()`
Currently, the beast claim proxy only runs if `TAKNET_PS_FEEDER_CLAIM_KEY` or `TAKNET_PS_FEEDER_MAC` is set. Update this function to also return `True` if `FEEDER_UUID` is set (which is essentially always, since it's generated at install time).

```python
def taknet_beast_uses_claim_proxy(env_vars):
    """
    True when the TAKNET-PS Beast feed should use the local identity proxy
    (valid claim key, valid feeder MAC, OR FEEDER_UUID + TAKNET enabled + resolvable upstream host).
    """
    if env_vars.get("TAKNET_PS_ENABLED", "true").lower() != "true":
        return False
    claim_uuid = normalize_feeder_claim_uuid(env_vars.get("TAKNET_PS_FEEDER_CLAIM_KEY", ""))
    feeder_mac = normalize_feeder_mac(env_vars.get("TAKNET_PS_FEEDER_MAC", ""))
    feeder_uuid = env_vars.get("FEEDER_UUID", "").strip()
    
    if claim_uuid is None and feeder_mac is None and not feeder_uuid:
        return False
        
    host, _ctype = select_taknet_host(env_vars)
    return bool(host)
```

### Update `build_docker_compose()`
Inject the `FEEDER_UUID` variable into the `taknet-beast-claim` container's environment so that the proxy script can access it.

```python
        services['taknet-beast-claim'] = {
            # ... keep existing configurations ...
            'environment': [
                'LISTEN_HOST=0.0.0.0',
                f'LISTEN_PORT={BEAST_CLAIM_PROXY_PORT}',
                f'UPSTREAM_HOST={taknet_upstream}',
                f'UPSTREAM_PORT={beast_port}',
                f'FEEDER_CLAIM_UUID={claim_uuid or ""}',
                f'FEEDER_MAC={feeder_mac}',
                f'FEEDER_UUID={env_vars.get("FEEDER_UUID", "").strip()}', # Add this line
            ],
            # ... keep existing configurations ...
```

## 2. Modify `scripts/beast_claim_proxy.py`

Update the proxy script to read the `FEEDER_UUID` environment variable and send it upstream as a metadata line: `TAKNET_FEEDER_UUID <uuid>\n`.

Add the extraction for the UUID:
```python
_UUID = (os.environ.get("FEEDER_UUID") or "").strip().lower()
UUID_PREFIX = b"TAKNET_FEEDER_UUID "
UUID_LINE = (UUID_PREFIX + _UUID.encode("ascii") + b"\n") if _UUID else None
```

Update `_handle_client` to transmit the new line to the aggregator (upstream):
```python
        if CLAIM_LINE:
            upstream.sendall(CLAIM_LINE)
        if MAC_LINE:
            upstream.sendall(MAC_LINE)
        if UUID_LINE:
            upstream.sendall(UUID_LINE)
```

Update the print log in `main()` to note the UUID transmission:
```python
    claim_note = "yes" if CLAIM_LINE else "no"
    mac_note = _MAC if MAC_LINE else "no"
    uuid_note = "yes" if UUID_LINE else "no"
    print(
        f"[beast-claim-proxy] listen {LISTEN_HOST}:{LISTEN_PORT} "
        f"-> {UPSTREAM_HOST}:{UPSTREAM_PORT} claim={claim_note} mac={mac_note} uuid={uuid_note}"
    )
```

## Summary
With these changes, the feeder will natively transmit its UDID to the aggregator, ensuring stable connectivity and deduplication across dynamic IPs.
