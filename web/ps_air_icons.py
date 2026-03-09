"""
PS Air Icons (Public Safety Air) — reference set for CoT icon paths.

Icons are stored in static/ps_air_icons/ (from PS_AIR_ICONSv8). Used by the COTProxy
config page for picker and thumbnails. TAK clients use the full path including
iconset UID, e.g. 66f14976-4b62-4023-8edb-d8d2ebeaa336/Public Safety Air/CIV_FIXED_CAP.png.

For TAK/CoT protocol handling see PyTAK: https://github.com/snstac/pytak
"""

import os
import xml.etree.ElementTree as ET

# Iconset UID from iconset.xml (Public Safety Air v8)
ICONSET_UID = "66f14976-4b62-4023-8edb-d8d2ebeaa336"
ICONSET_FOLDER = "Public Safety Air"
STATIC_PREFIX = "ps_air_icons"


def _icons_dir():
    return os.path.join(os.path.dirname(__file__), "static", STATIC_PREFIX)


def _parse_iconset():
    """Return dict of icon filename -> type2525b (CoT type string)."""
    path = os.path.join(_icons_dir(), "iconset.xml")
    if not os.path.isfile(path):
        return {}
    out = {}
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        for icon in root.findall(".//icon"):
            name = icon.get("name")
            cot = icon.get("type2525b")
            if name:
                out[name] = cot or ""
    except Exception:
        pass
    return out


def get_ps_air_icons_list():
    """
    Return list of { name, path, url_path, cot } for each PNG in Public Safety Air.
    path = TAK-style path (UID/folder/name). url_path = URL path for static serving.
    """
    base = os.path.join(_icons_dir(), ICONSET_FOLDER)
    if not os.path.isdir(base):
        return []
    cot_by_name = _parse_iconset()
    icons = []
    for f in sorted(os.listdir(base)):
        if not f.lower().endswith(".png"):
            continue
        name = f
        rel_path = f"{ICONSET_FOLDER}/{name}"
        path = f"{ICONSET_UID}/{rel_path}"
        url_path = f"/static/{STATIC_PREFIX}/{rel_path.replace(' ', '%20')}"
        icons.append({
            "name": name,
            "path": path,
            "url_path": url_path,
            "cot": cot_by_name.get(name, ""),
        })
    return icons


def icon_path_to_url_path(icon_path):
    """
    If icon_path is the TAK-style path (UID/Public Safety Air/name.png or similar),
    return the static URL path for thumbnail. Otherwise return None.
    """
    if not icon_path or not isinstance(icon_path, str):
        return None
    s = icon_path.strip()
    if ICONSET_UID in s and (ICONSET_FOLDER in s or "Public Safety Air" in s):
        # Extract "Public Safety Air/name.png"
        for prefix in (f"{ICONSET_UID}/", ICONSET_UID + "/"):
            if s.startswith(prefix):
                rest = s[len(prefix):].lstrip("/")
                if rest:
                    return f"/static/{STATIC_PREFIX}/{rest.replace(' ', '%20')}"
    if s.startswith("Public Safety Air/") or s.startswith(ICONSET_FOLDER + "/"):
        rest = s.split("/", 1)[-1] if "/" in s else s
        return f"/static/{STATIC_PREFIX}/{ICONSET_FOLDER}/{rest.replace(' ', '%20')}"
    return None
