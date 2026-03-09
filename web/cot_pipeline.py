"""
CoT (Cursor on Target) output pipeline — integration point for adsbcot and COTProxy-style transforms.

Outputs with output_type='cot' can send aircraft as CoT to a TAK Server or multicast.
When use_cotproxy is True, transform rules (per ICAO hex) are applied from the cot_transforms
table — same concept as COTProxy known_craft / COTProxyWeb.

References:
  - PyTAK: https://github.com/snstac/pytak — Python TAK clients, servers & gateways; CoT parse/serialize.
  - adsbcot: https://github.com/snstac/adsbcot (ADS-B → CoT)
  - cotproxy: https://github.com/snstac/cotproxy (CoT transformation proxy)

This module provides helpers for a future or external CoT sender:
  - get_cot_push_outputs() → list of output configs (cot_url, output_id, use_cotproxy)
  - Transform overrides: use CotTransformModel.get_by_hex(output_id, hex) when use_cotproxy is True.
"""

import os
import json


def get_cot_push_outputs():
    """
    Return list of active outputs that are CoT push (output_type=cot, mode=push, status=active)
    with a cot_url in config. Each item: { output_id, cot_url, use_cotproxy, name }.
    """
    from models import get_db
    conn = get_db()
    rows = conn.execute(
        """SELECT id, name, config, use_cotproxy
           FROM outputs
           WHERE output_type = 'cot' AND mode = 'push' AND status = 'active'"""
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        cfg = json.loads(row["config"] or "{}")
        cot_url = (cfg.get("cot_url") or "").strip()
        if not cot_url:
            continue
        result.append({
            "output_id": row["id"],
            "name": row["name"],
            "cot_url": cot_url,
            "use_cotproxy": bool(row["use_cotproxy"]),
        })
    return result


def get_transform_for_aircraft(output_id, hex_code):
    """
    Return transform override for an aircraft (by ICAO hex) when use_cotproxy is enabled.
    Returns dict with keys: callsign, type, cot, icon, domain, agency, reg, model; or None.
    """
    from models import CotTransformModel
    t = CotTransformModel.get_by_hex(output_id, hex_code)
    if not t:
        return None
    return {
        "callsign": t.get("callsign"),
        "type": t.get("type"),
        "cot": t.get("cot"),
        "icon": t.get("icon"),
        "domain": t.get("domain"),
        "agency": t.get("agency"),
        "reg": t.get("reg"),
        "model": t.get("model"),
    }
