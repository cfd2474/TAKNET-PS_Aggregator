"""Docker service — container management via Docker socket."""

import docker


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
