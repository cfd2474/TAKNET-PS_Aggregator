"""Docker service — container management via Docker socket."""

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


def enroll_netbird(setup_key, management_url):
    """Start netbird-client container with the given setup key.
    Uses network_mode=host so the WireGuard interface appears on the host.
    """
    client = get_client()
    if not client:
        return False, "Docker not available"

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
