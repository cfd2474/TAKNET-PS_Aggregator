"""Docker service — container management via Docker socket."""

from __future__ import annotations

import json
import os

import docker
import docker.errors

NETBIRD_CLIENT_NAME = "netbird-client"
NETBIRD_IMAGE = "netbirdio/netbird:latest"


def get_client():
    try:
        return docker.from_env()
    except Exception:
        return None


def get_containers():
    """Get status of all taknet containers."""
    client = get_client()
    if not client:
        return []

    results = []
    try:
        containers = client.containers.list(all=True, filters={"name": "taknet-"})
        for c in containers:
            started = c.attrs.get("State", {}).get("StartedAt", "")
            results.append({
                "name": c.name,
                "short_name": c.name.replace("taknet-", ""),
                "status": c.status,
                "image": c.image.tags[0] if c.image.tags else str(c.image.short_id),
                "started_at": started,
            })
    except Exception as e:
        print(f"[docker] Error listing containers: {e}")
    return sorted(results, key=lambda x: x["name"])


def restart_container(name):
    """Restart a container by name."""
    client = get_client()
    if not client:
        return False, "Docker not available"
    try:
        container = client.containers.get(name)
        container.restart(timeout=30)
        return True, f"Restarted {name}"
    except Exception as e:
        return False, str(e)


def get_logs(name, tail=100):
    """Get last N lines of container logs."""
    client = get_client()
    if not client:
        return ""
    try:
        container = client.containers.get(name)
        return container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
    except Exception as e:
        return f"Error: {e}"


def exec_in_container(name: str, cmd: list[str]) -> tuple[int, str, str]:
    """Run a command inside a container via Docker SDK (no docker CLI required)."""
    client = get_client()
    if not client:
        return 1, "", "Docker not available"
    try:
        container = client.containers.get(name)
        if container.status != "running":
            return 1, "", f"Container {name} is not running"
        result = container.exec_run(cmd, demux=True)
        if isinstance(result.output, tuple):
            out_b, err_b = result.output
        else:
            out_b, err_b = result.output, b""
        stdout = (out_b or b"").decode("utf-8", errors="replace")
        stderr = (err_b or b"").decode("utf-8", errors="replace")
        return int(result.exit_code), stdout, stderr
    except docker.errors.NotFound:
        return 1, "", f"Container {name} not found"
    except Exception as e:
        return 1, "", str(e)


def normalize_netbird_ip(raw: str) -> str:
    """Strip CIDR suffix from NetBird status IP (e.g. 100.x.x.x/16 -> 100.x.x.x)."""
    val = (raw or "").strip()
    if "/" in val:
        val = val.split("/", 1)[0].strip()
    return val


def get_netbird_status_json() -> tuple[dict | None, str]:
    """Parse netbird status --json from the client container."""
    code, stdout, stderr = exec_in_container(
        NETBIRD_CLIENT_NAME, ["netbird", "status", "--json"]
    )
    if code != 0:
        return None, (stderr or stdout or "netbird status failed").strip()[:300]
    if not stdout.strip():
        return None, "Empty status response"
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return None, f"Invalid status JSON: {e}"
    if not isinstance(data, dict):
        return None, "Unexpected status format"
    return data, ""


def get_netbird_client_status():
    """Get netbird-client container status."""
    client = get_client()
    if not client:
        return {"enrolled": False, "error": "Docker not available"}
    try:
        container = client.containers.get(NETBIRD_CLIENT_NAME)
        return {
            "enrolled": True,
            "running": container.status == "running",
            "status": container.status,
            "started_at": container.attrs.get("State", {}).get("StartedAt", ""),
        }
    except docker.errors.NotFound:
        return {"enrolled": False, "status": "not_enrolled"}
    except Exception as e:
        return {"enrolled": False, "error": str(e)}


def restart_netbird_mesh():
    """Restart NetBird inside the existing client container (preserves peer identity)."""
    client = get_client()
    if not client:
        return False, "Docker not available"
    try:
        container = client.containers.get(NETBIRD_CLIENT_NAME)
    except docker.errors.NotFound:
        return False, "NetBird client is not enrolled"
    if container.status != "running":
        try:
            container.start()
        except Exception as e:
            return False, f"Failed to start container: {e}"
    code, stdout, stderr = exec_in_container(
        NETBIRD_CLIENT_NAME, ["netbird", "down"]
    )
    if code != 0 and stderr and "not logged in" not in stderr.lower():
        pass  # down may fail if already down
    code, stdout, stderr = exec_in_container(
        NETBIRD_CLIENT_NAME, ["netbird", "up"]
    )
    if code != 0:
        try:
            container.restart(timeout=30)
        except Exception as e:
            return False, (stderr or stdout or str(e))[:300]
    return True, "NetBird mesh restarted (peer identity preserved)"


def enroll_netbird(setup_key, management_url, *, force=False):
    """Start netbird-client container with the given setup key.
    Uses network_mode=host so the WireGuard interface appears on the host.

    Re-enrolling destroys the container and registers a new peer (new overlay IP),
    which breaks all NetBird feeders until they reconnect. Pass force=True to allow
    that when already connected; otherwise use restart_netbird_mesh().
    """
    client = get_client()
    if not client:
        return False, "Docker not available"

    mesh_data, _ = get_netbird_status_json()
    if mesh_data and not force:
        overlay = normalize_netbird_ip(
            mesh_data.get("netbirdIp")
            or (mesh_data.get("Self") or {}).get("NetbirdIP")
            or ""
        )
        if overlay:
            return False, (
                "Aggregator is already enrolled on the mesh. Use Restart mesh instead — "
                "re-enrolling creates a new peer IP and disconnects all NetBird feeders."
            )

    # Remove existing container if present
    try:
        existing = client.containers.get(NETBIRD_CLIENT_NAME)
        existing.stop(timeout=10)
        existing.remove()
    except docker.errors.NotFound:
        pass
    except Exception as e:
        return False, f"Failed to remove existing container: {e}"

    # Pull image to ensure latest
    try:
        client.images.pull(NETBIRD_IMAGE)
    except Exception:
        pass  # Use cached image if pull fails

    # Start new container
    try:
        client.containers.run(
            NETBIRD_IMAGE,
            name=NETBIRD_CLIENT_NAME,
            cap_add=["NET_ADMIN", "SYS_MODULE"],
            devices=["/dev/net/tun:/dev/net/tun:rwm"],
            environment={
                "NB_SETUP_KEY": setup_key,
                "NB_MANAGEMENT_URL": management_url,
            },
            volumes={"netbird-client-data": {"bind": "/etc/netbird", "mode": "rw"}},
            network_mode="host",
            restart_policy={"Name": "unless-stopped"},
            detach=True,
        )
        return True, "NetBird client enrolled and started"
    except Exception as e:
        return False, str(e)


def disconnect_netbird():
    """Stop and remove netbird-client container."""
    client = get_client()
    if not client:
        return False, "Docker not available"
    try:
        container = client.containers.get(NETBIRD_CLIENT_NAME)
        container.stop(timeout=10)
        container.remove()
        return True, "NetBird client disconnected"
    except docker.errors.NotFound:
        return True, "NetBird client was not running"
    except Exception as e:
        return False, str(e)


def get_caddy_status():
    """Get status of the taknet-caddy container."""
    client = get_client()
    if not client:
        return {"running": False, "status": "Docker not available"}
    try:
        container = client.containers.get("taknet-caddy")
        started = container.attrs.get("State", {}).get("StartedAt", "")
        return {
            "running": container.status == "running",
            "status": container.status,
            "started_at": started,
            "image": container.image.tags[0] if container.image.tags else str(container.image.short_id),
        }
    except docker.errors.NotFound:
        return {"running": False, "status": "not_created"}
    except Exception as e:
        return {"running": False, "status": "error", "error": str(e)}


def get_caddy_certs():
    """Read and parse SSL certificates managed by Caddy."""
    import glob
    from datetime import datetime, timezone
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        return []

    caddy_data_path = "/caddy-data"
    search_path = os.path.join(caddy_data_path, "caddy", "certificates", "**", "*.crt")
    
    certs = []
    for crt_path in glob.glob(search_path, recursive=True):
        try:
            with open(crt_path, "rb") as f:
                cert_data = f.read()
            cert = x509.load_pem_x509_certificate(cert_data, default_backend())
            
            subject = cert.subject
            common_name = ""
            for attribute in subject:
                if attribute.oid._name == "commonName":
                    common_name = attribute.value
            
            sans = []
            try:
                ext = cert.extensions.get_extension_for_oid(x509.OID_SUBJECT_ALTERNATIVE_NAME)
                sans = ext.value.get_values_for_type(x509.DNSName)
            except Exception:
                pass

            issuer = ""
            for attribute in cert.issuer:
                if attribute.oid._name == "commonName":
                    issuer = attribute.value
                    
            expiry = cert.not_valid_after_utc
            now = datetime.now(timezone.utc)
            
            days_remaining = None
            if expiry:
                delta = expiry - now
                days_remaining = delta.days

            certs.append({
                "domain": common_name or (sans[0] if sans else "Unknown"),
                "sans": sans,
                "issuer": issuer or "Unknown",
                "expiry": expiry.isoformat() if expiry else None,
                "days_remaining": days_remaining,
                "expired": expiry < now if expiry else False,
            })
        except Exception as e:
            print(f"[caddy] Error reading cert {crt_path}: {e}")
            
    return sorted(certs, key=lambda x: x["domain"])


def force_renew_caddy_cert(domain):
    """Force renew Caddy certificate by deleting the cert directory and restarting Caddy."""
    import shutil
    import glob
    caddy_data_path = "/caddy-data"
    
    search_pattern = os.path.join(caddy_data_path, "caddy", "certificates", "*", domain)
    for domain_dir in glob.glob(search_pattern):
        try:
            if os.path.isdir(domain_dir):
                shutil.rmtree(domain_dir)
        except Exception as e:
            print(f"[caddy] Failed to delete cert dir {domain_dir}: {e}")
            return False, f"Failed to clean old certificate: {e}"
            
    ok, msg = restart_container("taknet-caddy")
    if ok:
        return True, "Old certificate removed. Caddy is restarting to issue a new certificate."
    else:
        return False, f"Failed to restart Caddy: {msg}"

