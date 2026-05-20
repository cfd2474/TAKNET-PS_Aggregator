"""
Mictronics/tar1090 aircraft database lookup for ADSBHub enrichment.

Uses the same aircraft.csv.gz as readsb/tar1090 (tar1090-db) so dbFlags, registration,
and type codes match local feeder aircraft for /v2/mil and related API filters.
"""

from __future__ import annotations

import gzip
import os
import threading
import time
import urllib.request

# dbFlags bits (airplanes.live / readsb)
FLAG_MIL = 1
FLAG_INTERESTING = 2
FLAG_PIA = 4
FLAG_LADD = 8

DEFAULT_DB_URL = (
    "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"
)

# ICAO ranges treated as military when not in DB (from readsb isMilRange).
_MIL_RANGES = (
    (0xADF7C8, 0xAFFFFF),
    (0x010070, 0x01008F),
    (0x0A4000, 0x0A4FFF),
    (0x33FF00, 0x33FFFF),
    (0x350000, 0x37FFFF),
    (0x3AA000, 0x3AFFFF),
    (0x3B7000, 0x3BFFFF),
    (0x3EA000, 0x3EBFFF),
    (0x3F4000, 0x3FBFFF),
    (0x400000, 0x40003F),
    (0x43C000, 0x43CFFF),
    (0x444000, 0x446FFF),
    (0x44F000, 0x44FFFF),
    (0x457000, 0x457FFF),
    (0x45F400, 0x45F4FF),
    (0x468000, 0x4683FF),
    (0x473C00, 0x473C0F),
    (0x478100, 0x4781FF),
    (0x480000, 0x480FFF),
    (0x48D800, 0x48D87F),
    (0x497C00, 0x497CFF),
    (0x498420, 0x49842F),
    (0x4B7000, 0x4B7FFF),
    (0x4B8200, 0x4B82FF),
    (0x70C070, 0x70C07F),
    (0x710258, 0x71028F),
    (0x710380, 0x71039F),
    (0x738A00, 0x738AFF),
    (0x7CF800, 0x7CFAFF),
    (0x800200, 0x8002FF),
    (0xC20000, 0xC3FFFF),
    (0xE40000, 0xE41FFF),
)

_lock = threading.Lock()
_lookup: dict[str, tuple[int, str | None, str | None]] = {}
_loaded_path: str | None = None
_loaded_mtime: float = 0.0
_loaded_at: float = 0.0
_entry_count = 0


def _parse_db_flags(flags_str: str) -> int:
    flags = 0
    for j, ch in enumerate((flags_str or "")[:32]):
        if ch == "1":
            flags |= 1 << j
    return flags


def is_mil_range(addr: int) -> bool:
    return any(lo <= addr <= hi for lo, hi in _MIL_RANGES)


def _normalize_hex(hex_raw: str) -> str | None:
    hex_ = str(hex_raw or "").strip().upper().lstrip("~")
    if len(hex_) != 6:
        return None
    try:
        int(hex_, 16)
    except ValueError:
        return None
    return hex_


def _load_gz(path: str) -> dict[str, tuple[int, str | None, str | None]]:
    out: dict[str, tuple[int, str | None, str | None]] = {}
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line[0] == "#":
                continue
            parts = line.rstrip("\n").split(";")
            if len(parts) < 4:
                continue
            hex_ = _normalize_hex(parts[0])
            if not hex_:
                continue
            flags = _parse_db_flags(parts[3])
            reg = (parts[1] or "").strip() or None
            typ = (parts[2] or "").strip() or None
            out[hex_] = (flags, reg, typ)
    return out


def _download_db(dest: str, url: str) -> None:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dest)


def _resolve_db_path() -> str | None:
    path = (os.environ.get("AIRCRAFT_DB_PATH") or "").strip()
    if path and os.path.isfile(path):
        return path
    url = (os.environ.get("AIRCRAFT_DB_URL") or DEFAULT_DB_URL).strip()
    cache = (os.environ.get("AIRCRAFT_DB_CACHE_PATH") or "/tmp/aircraft.csv.gz").strip()
    if not path and url:
        if not os.path.isfile(cache):
            try:
                print(f"[aircraft_db] Downloading {url} -> {cache}", flush=True)
                _download_db(cache, url)
            except Exception as e:
                print(f"[aircraft_db] Download failed: {e}", flush=True)
                return None
        return cache if os.path.isfile(cache) else None
    return None


def _maybe_reload() -> None:
    global _lookup, _loaded_path, _loaded_mtime, _loaded_at, _entry_count
    path = _resolve_db_path()
    if not path:
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    reload_sec = float(os.environ.get("AIRCRAFT_DB_RELOAD_HOURS", "24")) * 3600.0
    with _lock:
        need = (
            not _lookup
            or _loaded_path != path
            or _loaded_mtime != mtime
            or (time.time() - _loaded_at) >= reload_sec
        )
        if not need:
            return
    try:
        new_lookup = _load_gz(path)
    except Exception as e:
        print(f"[aircraft_db] Load failed ({path}): {e}", flush=True)
        return
    with _lock:
        _lookup = new_lookup
        _loaded_path = path
        _loaded_mtime = mtime
        _loaded_at = time.time()
        _entry_count = len(new_lookup)
    print(f"[aircraft_db] Loaded {len(new_lookup)} entries from {path}", flush=True)


def init() -> None:
    """Load aircraft DB at startup (non-fatal if missing)."""
    _maybe_reload()


def refresh_if_due() -> None:
    """Periodic reload when file mtime or cache age exceeds AIRCRAFT_DB_RELOAD_HOURS."""
    _maybe_reload()


def lookup(hex_raw: str) -> tuple[int, str | None, str | None] | None:
    hex_ = _normalize_hex(hex_raw)
    if not hex_:
        return None
    with _lock:
        return _lookup.get(hex_)


def enrich_adsbhub_aircraft(ac: dict) -> dict:
    """Add dbFlags (and optional r/t) to an ADSBHub-sourced aircraft record."""
    if (ac.get("source") or "").lower() != "adsbhub":
        return ac
    hex_ = _normalize_hex(ac.get("hex", ""))
    if not hex_:
        return ac

    db_flags = 0
    entry = lookup(hex_)
    if entry:
        db_flags = entry[0]
        if not ac.get("r") and entry[1]:
            ac["r"] = entry[1]
        if not ac.get("t") and entry[2]:
            ac["t"] = entry[2]

    try:
        if is_mil_range(int(hex_, 16)):
            db_flags |= FLAG_MIL
    except ValueError:
        pass

    if db_flags:
        ac["dbFlags"] = db_flags
    return ac


def stats() -> dict:
    with _lock:
        return {
            "loaded": _entry_count > 0,
            "entries": _entry_count,
            "path": _loaded_path,
        }
