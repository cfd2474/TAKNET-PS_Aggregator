# Azure Migration Runbook — Aggregator + Self-Hosted NetBird

**Goal:** Move the TAKNET-PS Aggregator and the self-hosted NetBird server from the
current Rocky 8 dev server to the new Rocky 9 Azure server, **without any feeder
needing to be re-enrolled or reconfigured**.

**Current NetBird versions (source server):** Management v0.72.2, Dashboard v2.39.0
(the modern combined `netbird-server` + separate `netbird-dashboard` layout — no
architecture migration needed, this is a straight host-to-host move).

---

## 1. Why this works (read before starting)

Feeders never point at a server IP. They are configured with:

| What feeders use | Where it lives | Must survive the move |
|---|---|---|
| Management URL `https://netbird.tak-solutions.com` | Hardcoded in feeder image | DNS flips to Azure IP — nothing changes on feeders |
| Their WireGuard peer identity | NetBird management database (`store.db` inside the NetBird server data volume) | **Copy the database. Never do a fresh NetBird install.** |
| Aggregator overlay IP (Beast 30004 / MLAT 30105 targets, e.g. `100.64.x.x`) | `netbird-client-data` Docker volume (the aggregator's own peer identity) | **Copy this volume so the aggregator keeps the same overlay IP.** |
| Aggregator dashboard/tunnel `https://adsb.tak-solutions.com` | DNS | DNS flips to Azure IP |

Three identities must move intact:

1. **NetBird management data** — all peer registrations, groups, policies, setup
   keys, and API tokens (`NETBIRD_API_TOKEN` in `.env` stays valid because PATs
   live in this database).
2. **Aggregator NetBird peer identity** — the `netbird-client-data` volume. If
   this is lost, the aggregator gets a **new overlay IP** and every feeder's
   Beast/MLAT target breaks.
3. **Aggregator database** — the `taknet-db-data` volume (`aggregator.db`:
   feeders, users, outputs, tunnel registrations).

If all three move, feeders reconnect on their own after DNS cutover. Nothing is
touched on any feeder.

**The one fatal mistake:** running a fresh NetBird install (or `enroll` with a new
setup key) on Azure and pointing DNS at it. Empty database = every feeder key
unknown = full re-enrollment of the fleet.

---

## 2. Phase 0 — Prep (days before, zero downtime)

### 2.1 Lower DNS TTLs

At your DNS provider, drop the TTL on both records to **300 seconds (5 min)**:

- `netbird.tak-solutions.com`
- `adsb.tak-solutions.com`

Do this at least 24 h before cutover so old TTLs expire everywhere.

### 2.2 Inventory the source server

SSH to the current dev server and record what's actually running:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker volume ls
```

Find the NetBird server's compose directory (it is a separate project from the
aggregator):

```bash
docker inspect netbird-server \
  --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
# Typical answer: /opt/netbird or similar — call it $NB_DIR below
```

Check how the NetBird containers join the aggregator's nginx network (the
aggregator nginx proxies `netbird.tak-solutions.com` to `netbird-server:80` and
`netbird-dashboard:80`, so they must share the `taknet-internal` network):

```bash
docker network inspect taknet-internal --format '{{range .Containers}}{{.Name}} {{end}}'
```

Note whether a `coturn` container exists (older installs). v0.72.x normally uses
the embedded relay over the domain on 443, but if coturn is present its
`turnserver.conf` contains the **old public IP** and must be updated on Azure.

Check for other co-located services you also need to move (Tailscale on the host,
Authentik at `/opt/authentik` if you installed it):

```bash
systemctl is-active tailscaled
ls -d /opt/authentik 2>/dev/null
```

### 2.3 Prepare the Azure VM (Rocky 9)

Via Azure Bastion, on the new VM:

```bash
# Docker (same repo the installer uses)
sudo dnf install -y dnf-utils rsync tar
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker

# Confirm WireGuard kernel module (built-in on Rocky 9)
sudo modprobe wireguard && lsmod | grep wireguard
```

### 2.4 Open Azure NSG + firewalld ports

**Azure NSG inbound rules** (Portal → VM → Networking), and the same on
`firewalld` on the VM:

| Port | Proto | Purpose |
|---|---|---|
| 22 | TCP | SSH (Bastion handles this; restrict source) |
| 80 | TCP | HTTP → Caddy (ACME challenges, redirect) |
| 443 | TCP | HTTPS — dashboard, NetBird mgmt/signal/relay gRPC+WS all ride 443 |
| 443 | UDP | HTTP/3 (Caddy) |
| 30004 | TCP | Beast input from feeders |
| 30003 | TCP | SBS output |
| 30005 | TCP | Beast output |
| 30105 | TCP | MLAT input |
| 39001 | TCP | MLAT results |
| 9000 | TCP | KLV ingest (only if using MediaMTX/KLV) |
| 51820 | UDP | WireGuard (NetBird client direct connections) |
| 3478 | UDP | STUN/TURN — **only if coturn exists on the source** |
| 49152–65535 | UDP | TURN relay range — **only if coturn exists** |

firewalld on the VM:

```bash
for p in 80/tcp 443/tcp 443/udp 30003/tcp 30004/tcp 30005/tcp 30105/tcp 39001/tcp 9000/tcp 51820/udp; do
  sudo firewall-cmd --permanent --add-port=$p
done
sudo firewall-cmd --reload
```

(Running `install.sh` later also opens the core ports, but do the NSG rules
manually — Azure blocks everything inbound by default.)

### 2.5 Set up a transfer path

Easiest: from the Azure VM, pull from the old server over SSH (old server is
publicly reachable). Generate a key on the Azure VM and authorize it on the old
server:

```bash
# On Azure VM
ssh-keygen -t ed25519 -f ~/.ssh/migration
cat ~/.ssh/migration.pub
# Append that line to /root/.ssh/authorized_keys (or your user) on the OLD server
ssh -i ~/.ssh/migration root@OLD_SERVER_IP hostname   # verify
```

If the old server is not directly reachable from Azure, reverse the direction
(push from old server to the Azure VM's public IP) or use
`az network bastion tunnel` from your workstation and `scp` through it.

---

## 3. Phase 1 — Trial sync (zero downtime)

Do a full copy **while everything is still running** on the old server. This is a
warm-up: it moves 95%+ of the bytes so the final cutover sync takes seconds, and
it lets you rehearse bringing the stack up on Azure.

On the **Azure VM** (adjust `OLD` and `$NB_DIR`):

```bash
OLD=root@OLD_SERVER_IP
KEY="-i ~/.ssh/migration"

# 1. NetBird server compose project (configs + any bind-mounted data)
sudo rsync -aHAX --numeric-ids -e "ssh $KEY" $OLD:/opt/netbird/ /opt/netbird/

# 2. Aggregator install dir (mainly for .env — the repo itself comes from git)
sudo mkdir -p /opt/taknet-aggregator
sudo rsync -aHAX --numeric-ids -e "ssh $KEY" \
  $OLD:/opt/taknet-aggregator/.env /opt/taknet-aggregator/.env

# 3. Docker named volumes (copy the _data dirs directly)
for v in taknet-db-data netbird-client-data taknet-mlat-work \
         taknet-tar1090-data taknet-graphs1090-data taknet-adsbhub-status; do
  sudo docker volume create $v
  sudo rsync -aHAX --numeric-ids -e "ssh $KEY" \
    $OLD:/var/lib/docker/volumes/$v/_data/ /var/lib/docker/volumes/$v/_data/
done

# 4. NetBird server volumes — list them on the old server first:
#    ssh $OLD "docker volume ls | grep -i netbird"
#    then copy each the same way (names vary by install: netbird_management,
#    netbird-mgmt-data, etc.). The critical file is store.db / management.json.
```

> **SQLite caveat:** copying `store.db` and `aggregator.db` while services are
> running can produce a torn copy. That is fine for this trial pass — the final
> cutover sync (Phase 3) happens with containers stopped, which re-copies them
> consistently.

Do **not** copy `taknet-caddy-data` (TLS certs) — Caddy re-issues certificates
automatically at cutover, and stale cert state causes more problems than it
solves.

---

## 4. Phase 2 — Stage the stacks on Azure (zero downtime)

### 4.1 NetBird server

```bash
cd /opt/netbird
# Review docker-compose.yml / management.json / setup.env for the OLD server's
# public IP. v0.72.x is normally all-domain-based; if you find a literal IP
# (common in coturn/turnserver.conf or NETBIRD_* env), change it to the Azure
# public IP now.
grep -rn "OLD_SERVER_IP" . || echo "clean — domain-based config"
```

**Do not start it yet** if the compose file publishes 80/443 directly — it will
conflict with nothing right now, but starting NetBird before its database has the
final sync is pointless. Just verify the config parses:

```bash
docker compose config >/dev/null && echo OK
```

### 4.2 Aggregator

```bash
curl -sSL https://raw.githubusercontent.com/cfd2474/TAKNET-PS_Aggregator/main/install.sh | sudo bash
```

The installer preserves the `.env` you rsynced, deploys to
`/opt/taknet-aggregator`, and starts the stack. That's fine — the aggregator can
run on Azure ahead of cutover; nothing points at it yet. Then stop it again so
the final data sync is clean:

```bash
cd /opt/taknet-aggregator && sudo docker compose down
```

### 4.3 Recreate the network attachment for NetBird

The aggregator's nginx expects to reach `netbird-server` and `netbird-dashboard`
on the `taknet-internal` Docker network. Replicate whatever the old server did
(from your Phase 0 inventory) — typically the NetBird compose file declares
`taknet-internal` as an external network, or the old setup used
`docker network connect taknet-internal netbird-server`. Make sure the same
mechanism exists on Azure before cutover.

### 4.4 Aggregator NetBird client container

The `taknet-netbird-client` container is created by the dashboard (Server
Enrollment), not by compose. On Azure, recreate it manually **reusing the copied
identity volume** — do NOT enroll with a setup key:

```bash
sudo docker run -d --name taknet-netbird-client \
  --cap-add NET_ADMIN --cap-add SYS_MODULE \
  --device /dev/net/tun:/dev/net/tun \
  -v netbird-client-data:/etc/netbird \
  --network host --restart unless-stopped \
  netbirdio/netbird:latest
```

Because `/etc/netbird/config.json` (private key + peer config) came from the old
server, this container is the **same peer** with the **same overlay IP**. It will
show disconnected until the NetBird server is live on Azure — that's expected.

> Check the exact image used on the old server with
> `ssh $OLD "docker inspect taknet-netbird-client --format '{{.Config.Image}}'"`
> and use that.

---

## 5. Phase 3 — Cutover (maintenance window, ~15–30 min)

Existing feeder-to-aggregator WireGuard tunnels keep passing data while the
management plane is down, but the aggregator endpoint itself is moving, so expect
a gap in feed data from "old server stopped" until "feeders reconnect to Azure."

### 5.1 Stop everything on the OLD server

```bash
cd /opt/taknet-aggregator && docker compose down
docker stop taknet-netbird-client
cd $NB_DIR && docker compose down        # NetBird server
```

### 5.2 Final data sync (consistent, containers stopped)

On the Azure VM, re-run the Phase 1 rsync commands. With everything stopped this
takes seconds and gives you byte-perfect databases:

```bash
# Same rsync commands as Phase 1 — re-run all of them.
```

### 5.3 Start NetBird server on Azure

```bash
cd /opt/netbird && sudo docker compose up -d
docker logs --tail 50 netbird-server   # look for clean startup, no cert/IdP errors
```

### 5.4 Start the aggregator on Azure

```bash
cd /opt/taknet-aggregator && sudo docker compose up -d
sudo docker start taknet-netbird-client
```

### 5.5 Flip DNS

Point both A records at the Azure VM's **public IP**:

- `netbird.tak-solutions.com` → Azure IP
- `adsb.tak-solutions.com` → Azure IP

With the 5-minute TTL from Phase 0, propagation is fast. Caddy obtains fresh
Let's Encrypt certificates automatically on the first HTTPS request for each
domain (watch: `docker logs -f taknet-caddy`).

### 5.6 Watch the fleet come back

```bash
# Management API responding through the proxy?
curl -s https://netbird.tak-solutions.com/api/peers \
  -H "Authorization: Bearer $NETBIRD_API_TOKEN" | head -c 300

# Aggregator's own peer identity intact? (should show the SAME overlay IP as before)
sudo docker exec taknet-netbird-client netbird status
```

Feeders' NetBird clients retry the management connection continuously; as DNS
propagates they reconnect on their own. Expect the peer list in the NetBird
dashboard (`https://netbird.tak-solutions.com`) to repopulate over 5–20 minutes.

---

## 6. Phase 4 — Verification checklist

- [ ] `https://netbird.tak-solutions.com` dashboard loads, login works, all peers listed
- [ ] Aggregator peer shows its **original** overlay IP (`netbird status` in `taknet-netbird-client`)
- [ ] `https://adsb.tak-solutions.com` dashboard loads; VPN page shows NetBird connected and API token valid
- [ ] Feeders reappear on the dashboard Feeders page with VPN classification intact
- [ ] Beast data flowing: dashboard map shows aircraft from direct feeders
- [ ] MLAT clients reconnected (`docker logs taknet-mlat`)
- [ ] Feeder web tunnels work (open a feeder detail page → tunnel)
- [ ] A brand-new setup key enrolls a test peer successfully (proves mgmt DB is writable and healthy)
- [ ] `taknet-agg status` clean; `docker ps` shows no restart loops

Straggler feeder that doesn't reconnect after ~30 min: on that feeder run
`netbird down && netbird up` (same key, no reconfiguration — it just forces a
fresh management connection / DNS lookup).

---

## 7. Rollback plan

The old server is untouched — its data was only ever *read*. If Azure
misbehaves:

1. `docker compose up -d` the NetBird server and aggregator on the old server,
   `docker start taknet-netbird-client`.
2. Flip both DNS A records back to the old IP.
3. Feeders reconnect to the old server the same way they would have to Azure.

Keep the old server intact (stopped is fine) for at least a week after cutover
before decommissioning.

---

## 8. Post-migration cleanup

- Restore DNS TTLs to their normal values (e.g. 3600).
- Remove the migration SSH key from the old server.
- Confirm `health-snapshot-host.timer` is active on Azure (`systemctl status health-snapshot-host.timer`) — `install.sh` sets it up.
- If Tailscale was in use on the old host (`TAILSCALE_ENABLED=true` in `.env`), install and `tailscale up` on Azure, or set `TAILSCALE_ENABLED=false`.
- If Authentik was running on the old server, migrate `/opt/authentik` the same way (compose dir + its postgres volume) — it's independent of NetBird and can move later.
- Snapshot the Azure VM once everything is verified (Portal → VM → Capture / disk snapshot) so you have a known-good restore point.
