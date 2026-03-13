"""
NATO Icons (Friend Air) — reference set for CoT icon paths.

Icons from NATO symbol set (Friend Air). Stored in static/nato_icons/friend_air/.
Used by the COTProxy config page for icon picker and reference grid.
Path format for TAK: Friend Air/<filename>.png
"""

import os

FOLDER = "Friend Air"
STATIC_PREFIX = "nato_icons"
SUB_DIR = "friend_air"


def _icons_dir():
    return os.path.join(os.path.dirname(__file__), "static", STATIC_PREFIX, SUB_DIR)


def get_nato_icons_list():
    """
    Return list of { name, path, url_path } for each PNG in Friend Air.
    path = TAK-style path (Friend Air/name). url_path = URL for static serving.
    """
    base = _icons_dir()
    if not os.path.isdir(base):
        return []
    icons = []
    for f in sorted(os.listdir(base)):
        if not f.lower().endswith(".png"):
            continue
        name = f
        path = f"{FOLDER}/{name}"
        url_path = f"/static/{STATIC_PREFIX}/{SUB_DIR}/{name}"
        icons.append({"name": name, "path": path, "url_path": url_path})
    return icons


def icon_path_to_url_path(icon_path):
    """If icon_path is Friend Air/xxx.png, return static URL for thumbnail."""
    if not icon_path or not isinstance(icon_path, str):
        return None
    s = icon_path.strip()
    if s.startswith(f"{FOLDER}/") or s.startswith("Friend Air/"):
        rest = s.split("/", 1)[-1]
        return f"/static/{STATIC_PREFIX}/{SUB_DIR}/{rest}"
    return None
