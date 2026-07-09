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


| What feeders use                                                            | Where it lives                                                                 | Must survive the move                                             |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------- |
| Management URL `https://netbird.tak-solutions.com`                          | Hardcoded in feeder image                                                      | DNS flips to Azure IP — nothing changes on feeders                |
| Their WireGuard peer identity                                               | NetBird management database (`store.db` inside the NetBird server data volume) | **Copy the database. Never do a fresh NetBird install.**          |
| Aggregator overlay IP (Beast 30004 / MLAT 30105 targets, e.g. `100.64.x.x`) | `netbird-client-data` Docker volume (the aggregator's own peer identity)       | **Copy this volume so the aggregator keeps the same overlay IP.** |
| Aggregator dashboard/tunnel `https://adsb.tak-solutions.com`                | DNS                                                                            | DNS flips to Azure IP                                             |


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

### 2.2 Inventory the source server — CONFIRMED 2026-07-08

Inventory was run on the old server (`104.225.219.254`, hostname
`leckliter-takserver`). Confirmed facts, used throughout the rest of this
runbook:


| Item                                   | Confirmed value                                                                                                                      |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Old server public IP                   | `104.225.219.254`                                                                                                                    |
| NetBird server compose dir (`$NB_DIR`) | `**/root**` — compose project name is `root`                                                                                         |
| NetBird server container               | `netbird-server` (`netbirdio/netbird-server:latest`), config `/etc/netbird/config.yaml`, publishes `3478/udp` + `127.0.0.1:8081->80` |
| NetBird dashboard container            | `netbird-dashboard` (`netbirdio/dashboard:latest`), publishes `127.0.0.1:8080->80`                                                   |
| NetBird server data volume             | `root_netbird_data` (also see stale-volume check below)                                                                              |
| Aggregator NetBird client container    | `**netbird-client**` (`netbirdio/netbird:latest`, host network) — identity volume `netbird-client-data`                              |
| Network attachment                     | `netbird-server` and `netbird-dashboard` are already joined to `taknet-internal` (nginx reaches them by container name)              |
| coturn                                 | **None** — no separate coturn container; only `3478/udp` published by `netbird-server`                                               |
| Tailscale                              | **Active on the host** (`tailscaled` running) — must be installed on Azure too                                                       |
| Authentik                              | Not installed                                                                                                                        |


**NetBird server file layout — CONFIRMED from `/root/docker-compose.yml` and
`docker inspect netbird-server`:**

- Live data volume: `**root_netbird_data**` (declared as plain `netbird_data:`
in the compose file; the `root_` prefix comes from the project name). The
other volume, `taknet-aggregator_netbird_data`, is a **stale leftover — do
not copy it**.
- Server config: **bind mount `/root/config.yaml`** → `/etc/netbird/config.yaml`.
- Dashboard env: `**/root/dashboard.env**` (via `env_file`).
- Networks: the compose file declares `taknet-internal` as **external** and
joins both containers to it — no manual `docker network connect` needed on
Azure, but `taknet-internal` must already exist when NetBird starts (see 5.3).
- Because `netbird_data` is a plain (non-external) volume declaration,
`**docker compose -p root` is mandatory on Azure** — without it, compose
creates an empty volume under a different name and serves an empty peer
database.

Files that must reach Azure: `docker-compose.yml`, `config.yaml`,
`dashboard.env` (all in `/root`).

**Security note for the Azure VM:** the old server logged **940 failed root SSH
login attempts** between sessions — it has password auth on a public port 22. On
Azure, do not expose 22 publicly: use Bastion only, and disable password
authentication (`PasswordAuthentication no` in `sshd_config`).

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


Traffic sources: most ports must allow **Any** source — the clients are a fleet
of feeder devices on residential/cellular connections with dynamic IPs, so there
is no fixed range to scope to. Security is enforced at the service layer
(WireGuard keys, TLS, dashboard auth), not by source filtering. This mirrors the
exposure profile of the old server, which serves the same fleet today.
**Do not add a public inbound rule for port 22** — SSH stays Bastion-only.

| Port  | Proto | Source | Purpose                                                           |
| ----- | ----- | ------ | ----------------------------------------------------------------- |
| 80    | TCP   | Any (Let's Encrypt validators have no published ranges) | HTTP → Caddy (ACME challenges, redirect) |
| 443   | TCP   | Any (all NetBird feeder clients + dashboard browsers)   | HTTPS — dashboard, NetBird mgmt/signal/relay gRPC+WS all ride 443 |
| 443   | UDP   | Any (browsers; optional — omit if undesired)            | HTTP/3 (Caddy)                          |
| 30004 | TCP   | Any (feeders, dynamic IPs)                              | Beast input from feeders                |
| 30003 | TCP   | Scope to known consumer IPs, or omit if none            | SBS output                              |
| 30005 | TCP   | Scope to known consumer IPs, or omit if none            | Beast output                            |
| 30105 | TCP   | Any (feeders, dynamic IPs)                              | MLAT input                              |
| 39001 | TCP   | Any (feeders, dynamic IPs)                              | MLAT results                            |
| 9000  | TCP   | Scope to known MediaMTX IPs, or omit (KLV normally rides the VPN overlay) | KLV ingest |
| 51820 | UDP   | Any (all NetBird peers, dynamic IPs)                    | WireGuard direct connections            |
| 3478  | UDP   | Any (all NetBird peers, dynamic IPs)                    | STUN — published by `netbird-server`    |

Minimal set for the migration to succeed: 80/tcp, 443/tcp, 30004/tcp,
30105/tcp, 39001/tcp, 51820/udp, 3478/udp — all source Any. The rest can be
added later if a consumer needs them.

(No 49152–65535 TURN range needed — the source has no coturn container; relay
traffic rides 443 through Caddy/nginx.)

firewalld on the VM — **the Azure Rocky 9 image does not ship firewalld**, so
install and enable it first (safe mid-session: the default zone allows `ssh`,
so the Bastion connection survives).

> **Troubleshooting (hit on 2026-07-08):** if firewalld hangs in
> `activating (start)` and times out after 90 s, check
> `journalctl -u firewalld` for
> `libnftables.so.1: undefined symbol ... LIBNFTNL_11` — a
> nftables/libnftnl package version mismatch from a partially-updated image.
> Fix on a not-yet-in-service VM: `sudo dnf update -y && sudo reboot`, then
> retry. (Targeted alternative: `sudo dnf update -y nftables libnftnl`.)

```bash
sudo dnf update -y && sudo reboot   # recommended first on a fresh VM: consistent packages + current kernel
# ...reconnect...
sudo dnf install -y firewalld
sudo systemctl enable --now firewalld
sudo firewall-cmd --state                # "running"
sudo firewall-cmd --list-services        # should include: ssh

for p in 80/tcp 443/tcp 443/udp 30003/tcp 30004/tcp 30005/tcp 30105/tcp 39001/tcp 9000/tcp 51820/udp 3478/udp; do
  sudo firewall-cmd --permanent --add-port=$p
done
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports           # confirm all 11 entries
```

Docker-published container ports bypass firewalld via Docker's own iptables
rules, but the host-network `netbird-client` container (WireGuard 51820/udp)
genuinely needs these openings — don't skip this step.

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
ssh -i ~/.ssh/migration root@104.225.219.254 hostname   # verify
```

If the old server is not directly reachable from Azure, reverse the direction
(push from old server to the Azure VM's public IP) or use
`az network bastion tunnel` from your workstation and `scp` through it.

---

## 3. Phase 1 — Trial sync (zero downtime)

Do a full copy **while everything is still running** on the old server. This is a
warm-up: it moves 95%+ of the bytes so the final cutover sync takes seconds, and
it lets you rehearse bringing the stack up on Azure.

On the **Azure VM**. Two gotchas baked into these commands (both hit on
2026-07-08):

- **Absolute key path:** the rsyncs run under `sudo`, where `~` resolves to
`/root`, not your login user's home. Use the absolute path to the key or SSH
silently falls back to password prompts.
- **No `-A -X` flags:** `-X` tries to copy SELinux xattrs from the old server;
the destination SELinux policy blocks that (rsync exit code 23,
`lremovexattr ... Permission denied`) and cross-host labels are wrong anyway.
Use `-aH` and run `restorecon` on volume data so files get correct local
labels.

```bash
OLD=root@104.225.219.254
KEY="-i /home/atakuser/.ssh/migration"   # ABSOLUTE path — see note above

# 1. NetBird server compose project — exactly three files, all in /root on the
#    old server. Copy them into /opt/netbird on Azure (cleaner home).
#    IMPORTANT: because the old project directory was /root, the compose project
#    name is "root" (that's why the volume is root_netbird_data). On Azure you
#    must start it with `docker compose -p root ...` (see Phase 2/3) or compose
#    will create a fresh EMPTY netbird_data volume under a new prefix.
sudo mkdir -p /opt/netbird
sudo rsync -aH --numeric-ids -e "ssh $KEY" \
  $OLD:/root/docker-compose.yml $OLD:/root/config.yaml $OLD:/root/dashboard.env \
  /opt/netbird/

# 2. Aggregator install dir (mainly for .env — the repo itself comes from git)
sudo mkdir -p /opt/taknet-aggregator
sudo rsync -aH --numeric-ids -e "ssh $KEY" \
  $OLD:/opt/taknet-aggregator/.env /opt/taknet-aggregator/.env

# 3. Docker named volumes (copy the _data dirs directly).
#    root_netbird_data = NetBird server DB (all peer registrations) — the
#    critical one. Do NOT copy taknet-aggregator_netbird_data (stale leftover).
for v in taknet-db-data netbird-client-data root_netbird_data \
         taknet-mlat-work taknet-mlat-logs \
         taknet-tar1090-data taknet-graphs1090-data taknet-adsbhub-status; do
  sudo docker volume create $v
  sudo rsync -aH --numeric-ids -e "ssh $KEY" \
    $OLD:/var/lib/docker/volumes/$v/_data/ /var/lib/docker/volumes/$v/_data/
  sudo restorecon -R /var/lib/docker/volumes/$v/_data
done
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
# Review config.yaml, dashboard.env, and docker-compose.yml for the OLD server's
# public IP. v0.72.x is normally all-domain-based; if you find a literal IP,
# change it to the Azure public IP now.
grep -n "104.225.219.254" docker-compose.yml config.yaml dashboard.env \
  || echo "clean — domain-based config"
```

**Do not start it yet** — starting NetBird before its database has the final
sync is pointless. Just verify the config parses, **always using `-p root`**:
the compose file declares the data volume as plain `netbird_data:`, which only
resolves to the copied `root_netbird_data` volume when the project name is
`root`. Without `-p root`, compose silently creates an empty
`netbird_netbird_data` and you'd be serving an empty peer database.

```bash
sudo docker compose -p root config >/dev/null && echo OK
```

### 4.2 Aggregator

Run the installer **from your home directory, not from `/opt/netbird`** — when
piped through curl, installer versions before v1.0.410 treated any
`docker-compose.yml` in the current directory as the aggregator repo and would
deploy the NetBird server files into `/opt/taknet-aggregator` by mistake (fixed
in v1.0.410, but `cd ~` first regardless):

```bash
cd ~
curl -sSL https://raw.githubusercontent.com/cfd2474/TAKNET-PS_Aggregator/main/install.sh | sudo bash
```

Expect `Cloning from GitHub...` and a real version number in the output. If it
says `Running from local repo` with `Version: vunknown`, stop — it picked up the
wrong directory. Recover with:

```bash
sudo docker network rm taknet-aggregator_netbird 2>/dev/null
sudo rm -rf /opt/taknet-aggregator
cd ~   # then re-run the installer
```

The installer preserves the `.env` you rsynced, deploys to
`/opt/taknet-aggregator`, and starts the stack. That's fine — the aggregator can
run on Azure ahead of cutover; nothing points at it yet. (If the installer
created a default `.env` instead of preserving the copied one, re-run the Phase 1
step 2 rsync now.) Then stop the stack again so the final data sync is clean:

```bash
cd /opt/taknet-aggregator && sudo docker compose down
```

### 4.3 Network attachment for NetBird — handled by compose

Confirmed: the NetBird compose file already declares `taknet-internal` as an
**external** network and joins both containers to it, so nginx reaches
`netbird-server:80` / `netbird-dashboard:80` by container name with no manual
steps.

The only catch is ordering: an external network must exist before the NetBird
project starts, and `docker compose down` on the aggregator **removes**
`taknet-internal` (the aggregator project owns it). Phase 3 handles this by
creating the network explicitly before starting NetBird — nothing to do in this
phase.

### 4.4 Aggregator NetBird client container

On the old server this is the `netbird-client` container (image
`netbirdio/netbird:latest`, host network) — created by the dashboard's Server
Enrollment, not by compose. On Azure, recreate it manually **reusing the copied
identity volume** — do NOT enroll with a setup key:

```bash
sudo docker run -d --name netbird-client \
  --cap-add NET_ADMIN --cap-add SYS_MODULE \
  --device /dev/net/tun:/dev/net/tun \
  -v netbird-client-data:/etc/netbird \
  --network host --restart unless-stopped \
  netbirdio/netbird:latest

# CRITICAL: stop it immediately — it stays stopped until Phase 3 step 5.4
sudo docker stop netbird-client
```

Because `/etc/netbird/config.json` (private key + peer config) came from the old
server, this container is the **same peer** with the **same overlay IP**.

**Why it must be stopped until cutover:** DNS still points
`netbird.tak-solutions.com` at the old server's live management plane, so if
this container runs now it connects there and duels with the old server's
still-running `netbird-client` for the same peer identity. The aggregator's
overlay IP — the address every feeder sends Beast/MLAT data to — flaps between
the two hosts, and traffic following the Azure endpoint dies against the stopped
aggregator stack. If you started it by accident: `sudo docker stop
netbird-client` on Azure, then on the old server run
`docker exec netbird-client netbird status` to confirm it reclaims Management:
Connected with its usual NetBird IP; disrupted feeders self-heal within a couple
of minutes.

---

## 5. Phase 3 — Cutover (maintenance window, ~15–30 min)

Existing feeder-to-aggregator WireGuard tunnels keep passing data while the
management plane is down, but the aggregator endpoint itself is moving, so expect
a gap in feed data from "old server stopped" until "feeders reconnect to Azure."

### 5.1 Stop everything on the OLD server

```bash
cd /opt/taknet-aggregator && docker compose down
docker stop netbird-client
cd /root && docker compose down          # NetBird server (project dir is /root)
```

### 5.2 Final data sync (consistent, containers stopped)

On the Azure VM, re-run the Phase 1 rsync commands. With everything stopped this
takes seconds and gives you byte-perfect databases:

```bash
# Same rsync commands as Phase 1 — re-run all of them.
```

### 5.3 Start the aggregator on Azure (first — it owns `taknet-internal`)

The NetBird compose file needs `taknet-internal` to exist (external network),
and the aggregator project is what creates it. So the aggregator starts first:

```bash
cd /opt/taknet-aggregator && sudo docker compose up -d
```

`taknet-nginx` may restart-loop for a moment with "host not found in upstream
netbird-server" — expected until the next step, and it recovers on its own.

### 5.4 Start NetBird server on Azure

```bash
cd /opt/netbird && sudo docker compose -p root up -d   # -p root reuses root_netbird_data
sudo docker logs --tail 50 netbird-server    # look for clean startup, no cert/IdP errors
sudo docker restart taknet-nginx             # clear any stale upstream failure
sudo docker start netbird-client             # aggregator's own peer
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
sudo docker exec netbird-client netbird status
```

Feeders' NetBird clients retry the management connection continuously; as DNS
propagates they reconnect on their own. Expect the peer list in the NetBird
dashboard (`https://netbird.tak-solutions.com`) to repopulate over 5–20 minutes.

---

## 6. Phase 4 — Verification checklist

- [ ] `https://netbird.tak-solutions.com` dashboard loads, login works, all peers listed
- [ ] Aggregator peer shows its **original** overlay IP (`netbird status` in `netbird-client`)
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

1. On the old server, same order as cutover (aggregator first — it owns the
  `taknet-internal` network): `cd /opt/taknet-aggregator && docker compose up -d`,
   then `cd /root && docker compose up -d` (NetBird server), then
   `docker start netbird-client`.
2. Flip both DNS A records back to the old IP.
3. Feeders reconnect to the old server the same way they would have to Azure.

Keep the old server intact (stopped is fine) for at least a week after cutover
before decommissioning.

---

## 8. Post-migration cleanup

- Restore DNS TTLs to their normal values (e.g. 3600).
- Remove the migration SSH key from the old server.
- Confirm `health-snapshot-host.timer` is active on Azure (`systemctl status health-snapshot-host.timer`) — `install.sh` sets it up.
- Tailscale is confirmed active on the old host: install it on Azure (`dnf install tailscale`, `tailscale up`) or set `TAILSCALE_ENABLED=false` in `.env` if it's no longer needed.
- Harden SSH on the Azure VM: Bastion-only access, `PasswordAuthentication no` (the old server accumulated 940 failed root login attempts on its public port 22).
- Optional day-to-day SSH: once `netbird-client` is running, the VM is reachable at its overlay IP (`ssh atakuser@100.64.x.x`) from any enrolled peer — no NSG rule needed since WireGuard encapsulates it. Requires your admin device enrolled as a peer and an access policy allowing it to reach the aggregator on TCP 22 (default all-to-all policy already covers this). Keep Bastion as break-glass: the NetBird server runs on this same VM, so a NetBird outage takes SSH-over-NetBird down with it.
- Snapshot the Azure VM once everything is verified (Portal → VM → Capture / disk snapshot) so you have a known-good restore point.

