"""
Feeder tunnel service: WebSocket server at /tunnel and internal HTTP proxy API at /proxy.

- Feeders connect via WebSocket, send register with feeder_id; we store feeder_id -> ws.
- Dashboard (Flask) calls POST /proxy with feeder_id, method, path, headers, body; we forward
  over the feeder's WebSocket as a "request" message and wait for "response", then return to Flask.
"""

import asyncio
import base64
import json
import logging
import os
import sqlite3
import uuid
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Feeder Tunnel Service")

# feeder_id -> WebSocket (one active connection per feeder; new replaces old)
feeder_connections: dict[str, WebSocket] = {}
# feeder_id -> host (e.g. "100.85.149.249:8080") from register; used by aggregator for Host header
feeder_hosts: dict[str, str] = {}
# request_id -> (event, holder_list); holder[0] set to result dict when response received
pending_requests: dict[str, tuple[asyncio.Event, list[Optional[dict]]]] = {}
_lock = asyncio.Lock()
DB_PATH = os.environ.get("DB_PATH", "/data/aggregator.db")


def update_feeder_db(feeder_id: str, version: Optional[str]):
    """Update software_version and tunnel_feeder_id in the shared aggregator database."""
    if not os.path.exists(DB_PATH):
        logger.warning("Database not found at %s, skipping update", DB_PATH)
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        # Use feeder_id AS tunnel_feeder_id (the feeder has already sanitized it)
        # We find the feeder by trying to match tunnel_feeder_id or name/hostname fallback
        # But most importantly, we set these columns so the dashboard can find it fast next time.
        
        # We update software_version if provided.
        if version:
            conn.execute(
                "UPDATE feeders SET software_version = ?, tunnel_feeder_id = ?, updated_at = CURRENT_TIMESTAMP WHERE tunnel_feeder_id = ? OR name LIKE ? OR hostname LIKE ?",
                (version, feeder_id, feeder_id, f"{feeder_id}%", f"{feeder_id}%")
            )
        else:
            conn.execute(
                "UPDATE feeders SET tunnel_feeder_id = ?, updated_at = CURRENT_TIMESTAMP WHERE tunnel_feeder_id = ? OR name LIKE ? OR hostname LIKE ?",
                (feeder_id, feeder_id, f"{feeder_id}%", f"{feeder_id}%")
            )
        conn.commit()
        conn.close()
        logger.info("Updated DB for feeder_id=%s version=%s", feeder_id, version or "(none)")
    except Exception as e:
        logger.error("Failed to update DB for feeder %s: %s", feeder_id, e)


def _headers_for_proxy(in_headers: dict) -> dict:
    """Filter hop-by-hop and other headers not to forward to feeder."""
    skip = {
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
    return {k: v for k, v in in_headers.items() if k.lower() not in skip and v is not None}


@app.websocket("/tunnel")
async def tunnel_websocket(websocket: WebSocket):
    await websocket.accept()
    feeder_id: Optional[str] = None
    try:
        # First message must be register
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
        msg = json.loads(raw)
        if msg.get("type") != "register":
            await websocket.send_json({"type": "error", "message": "First message must be register"})
            await websocket.close(code=4000)
            return
        fid = msg.get("feeder_id")
        if not fid or not isinstance(fid, str):
            await websocket.send_json({"type": "error", "message": "register requires feeder_id"})
            await websocket.close(code=4000)
            return
        feeder_id = fid.strip()
        h = msg.get("host")
        host = h.strip() if isinstance(h, str) and h else None
        version = msg.get("version")
        version = version.strip() if isinstance(version, str) and version else None
        
        async with _lock:
            old = feeder_connections.get(feeder_id)
            if old and old != websocket:
                try:
                    await old.close(code=4001)
                except Exception:
                    pass
            feeder_connections[feeder_id] = websocket
            feeder_hosts[feeder_id] = host or ""
        
        # Update database in the background (non-blocking for the websocket)
        asyncio.create_task(asyncio.to_thread(update_feeder_db, feeder_id, version))
        
        logger.info("Tunnel registered: feeder_id=%s host=%s version=%s", feeder_id, host or "(none)", version or "(none)")

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "response":
                req_id = data.get("id")
                if req_id and req_id in pending_requests:
                    event, holder = pending_requests.pop(req_id)
                    holder[0] = {
                        "status": data.get("status", 502),
                        "headers": data.get("headers") or {},
                        "body": data.get("body") or "",
                    }
                    event.set()
                continue
            if msg_type == "pong":
                continue
            logger.warning("Unknown message type from feeder %s: %s", feeder_id, msg_type)
    except asyncio.TimeoutError:
        logger.warning("Tunnel register timeout")
    except WebSocketDisconnect:
        logger.info("Tunnel disconnected: feeder_id=%s", feeder_id)
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON from feeder: %s", e)
    except Exception as e:
        logger.exception("Tunnel error: %s", e)
    finally:
        if feeder_id:
            async with _lock:
                if feeder_connections.get(feeder_id) == websocket:
                    feeder_connections.pop(feeder_id, None)
                    feeder_hosts.pop(feeder_id, None)
            try:
                await websocket.close()
            except Exception:
                pass


@app.get("/feeders")
async def list_feeders():
    """List registered feeder_id and host (for debugging: see what the tunnel has stored)."""
    async with _lock:
        return {
            "feeders": [
                {"feeder_id": fid, "host": feeder_hosts.get(fid, "") or "(none)"}
                for fid in feeder_connections
            ]
        }


@app.get("/feeder/{feeder_id}/host")
async def get_feeder_host(feeder_id: str):
    """Return the host (e.g. '100.85.149.249:8080') the feeder sent at register, for proxy Host header."""
    async with _lock:
        host = feeder_hosts.get(feeder_id)
    if not host:
        return JSONResponse({"error": "Unknown feeder or no host"}, status_code=404)
    return {"host": host}


@app.post("/proxy")
async def proxy_request(request: Request):
    """Internal API: forward HTTP request to feeder over its WebSocket and return response."""
    try:
        body = await request.body()
        data = body.decode("utf-8")
        if not data:
            return JSONResponse({"error": "Missing JSON body"}, status_code=400)
        payload = json.loads(data)
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)

    feeder_id = payload.get("feeder_id")
    method = payload.get("method", "GET")
    path = payload.get("path", "/")
    headers = payload.get("headers") or {}
    body_b64 = payload.get("body", "")

    if not feeder_id:
        return JSONResponse({"error": "Missing feeder_id"}, status_code=400)

    async with _lock:
        ws = feeder_connections.get(feeder_id)
    if not ws:
        return JSONResponse(
            {"error": "Feeder offline", "feeder_id": feeder_id},
            status_code=503,
        )

    req_id = str(uuid.uuid4())
    event = asyncio.Event()
    holder: list[Optional[dict]] = [None]  # holder[0] set when response received
    pending_requests[req_id] = (event, holder)

    try:
        await ws.send_json({
            "type": "request",
            "id": req_id,
            "method": method,
            "path": path,
            "headers": _headers_for_proxy(headers),
            "body": body_b64,
        })
        try:
            await asyncio.wait_for(event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pending_requests.pop(req_id, None)
            return JSONResponse(
                {"error": "Feeder response timeout", "feeder_id": feeder_id},
                status_code=504,
            )
        result = holder[0]
        if result is None:
            return JSONResponse({"error": "No response"}, status_code=502)
        return JSONResponse({
            "status": result["status"],
            "headers": result["headers"],
            "body": result["body"],
        })
    except Exception as e:
        pending_requests.pop(req_id, None)
        logger.exception("Proxy error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/health")
async def health():
    return {"status": "ok"}
