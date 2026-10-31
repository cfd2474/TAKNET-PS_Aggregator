# PROJECT_STATE — TAKNET-PS Aggregator

_Last updated: 2026-10-31 (handover from Gemini to Claude)_

## Repos
- Aggregator: `cfd2474/TAKNET-PS_Aggregator` (private), local `D:\Projects\cursor\TAKNET-PS_Aggregator`. `main` matches `origin/main` at v1.0.415 (2026-07-25).
- Feeder image: `cfd2474/TAKNET-PS_ADS-B_Feeder` (public), local `D:\Projects\cursor\TAKNET-PS_ADS-B_Feeder`. It is maintained by the peer Claude session `taknet-ps-ads-b-feeder-cb`, reachable with SendMessage. GitHub is at v3.1.9. Local has uncommitted v3.1.10 waiting for operator review.

## Live architecture (source of truth: docker-compose.yml)
Request path: Caddy (80/443, TLS) → nginx → services on the `taknet-internal` bridge.

| Service | Dir / image | Role |
|---|---|---|
| beast-proxy | `beast-proxy/` | Feeder Beast in on :30004. Reads the in-band `TAKNET_FEEDER_CLAIM/MAC/UUID` lines, classifies feeders, writes SQLite, forwards to readsb:30006 |
| readsb | sdr-enthusiasts image | Net-only aggregation |
| mlat-server | `mlat-server/` | Feeder MLAT in on :30105, results on :39001 |
| tar1090 | sdr-enthusiasts image | Map and graphs1090 |
| aircraft-merger | `aircraft-merger/` | Merges tar1090 + ADSBHub + `/ingest/` JSON push (third-party feeds; the feeder does not use it) |
| adsbhub-feeder | `adsbhub-feeder/` | Optional outbound feed to ADSBHub |
| api | `api-server/` (container name `taknet-api`) | `/v2/` REST on :8880. Release 1.0.297 calls it retired, but it is still wired |
| tunnel | `tunnel-service/` | WebSocket `/tunnel` for feeders, internal `/proxy` |
| dashboard | `web/` | Flask on :5000. Also runs the KLV listener on :9000, the APScheduler jobs and the CoT push pipeline |
| nginx | `nginx/` | Routing. `conf.d/netbird.conf` expects NetBird containers from a separate compose project |
| caddy | `Caddyfile` | TLS for adsb.tak-solutions.com and netbird.tak-solutions.com |

Host side: `scripts/health_snapshot_host.py` runs from a systemd timer that `install.sh` installs.

## Feeder ↔ aggregator contract (confirmed by the feeder agent from feeder code)
- Beast → :30004. When the feeder has a claim key, MAC or UUID, it prefixes the stream with `TAKNET_FEEDER_*` lines.
- MLAT → :30105/39001.
- `wss://<host>/tunnel`: the feeder sends `register`, then `response`. Requests are routed by `X-Tunnel-Target` (tar1090 → :8080, dashboard → nginx :80 as of feeder v3.1.10).
- NetBird enrollment goes to netbird.tak-solutions.com. The feeder picks its host: vpn.tak-solutions.com when NetBird is up, otherwise adsb.tak-solutions.com.
- Inbound over NetBird to the feeder: :5000, :80, :8080, :8081, :8082, :8754, through `/feeder-view/` and the NetBird fast path.
- Tailscale on the feeder is personal remote access only. It is no longer an aggregator link.

## Dead / unused (removed in v1.0.416, except the local ARCHIVE/ and .db files and the README)
- `taknet-api/`: superseded by `api-server/`. Nothing builds it.
- `v139_work/`: an old snapshot. It is still rsynced into /opt by `install.sh:94`.
- `ARCHIVE/*.tar.gz`: gitignored local tarballs.
- `.venv/`: 1,726 macOS venv files committed in v1.0.152 by mistake. Already deleted locally; the deletion is uncommitted.
- 21 committed `__pycache__/*.pyc` files. There is no `.dockerignore`, so they end up in images.
- `web/nato_icons.py`, `GET /api/nato-icons` and `web/static/nato_icons/` (104 PNGs): the feature was removed from the UI.
- `GET /api/ps-air-icons`: no client calls it (the icons themselves are live). `web/static/img/taknetps.png` is unreferenced.
- `.tmp_sanity502.db` and the empty `taknet.db` at the root.
- Stale docs: README says v1.0.373 and "seven containers"; the root `*.md` notes have not been reviewed for currency.

## Known bugs / open items
1. ~~NetBird fast path sends dashboard traffic to :5000~~: fixed in v1.0.417.
2. Compose publishes dashboard `9000:9000`. That port is the KLV listener, not the UI. This is intentional, but the label is misleading.
3. ~~Update paths blocked because the repo was private~~: the repo was made **public** on 2026-10-31 (operator decision). Anonymous raw fetch and clone verified working. `docs/REMOTE_SERVER.md` is gitignored and stays local.
4. The dashboard updater only builds `dashboard beast-proxy mlat-server api` (`web/routes/api.py:98`), so changes to tunnel, aircraft-merger and adsbhub-feeder never deploy through Config → Updates.
5. **Config → Updates reports "Update Failed" even though the update succeeds** (diagnosed 2026-10-31 on the v1.0.418 update):
   - The dashboard runs a single gunicorn **sync** worker with `--timeout 120` (`web/Dockerfile:17`). The SSE stream `/api/updates/stream` holds that worker for the whole build, which took 3.5 min. Gunicorn kills the worker at 120 s, the stream drops before `pre_restart`, and the UI shows "Connection lost unexpectedly".
   - The `docker:cli` updater container finishes independently: `DONE:1.0.418`, and the fix is confirmed in the running dashboard.
   - Side effects: the `_run_update` thread dies, so no `update_history` row is written and the updater container is left behind (`vibrant_perlman`). Even without the kill, `up -d` recreates the dashboard and would also kill that thread. While a stream is open, the single sync worker also blocks every other dashboard request.
   - Only 1 web-UI update has ever been recorded (2026-02-23) out of 97; the operator has used `taknet-agg update`.
   - Fixed in v1.0.419 (Chunk D). Verified: the updater exit trap was tested in the real `docker:cli` image (success and failure both write valid JSON), and idempotent recording was tested against a scratch DB. The app boots locally.
   - **Inferred**: the worker-timeout log line was lost when the dashboard container was recreated. The timeline fits exactly.
6. Planned with the feeder agent, not started: "Chunk E", per-feeder hostnames `<feeder>.feeders.tak-solutions.com`. Most of the work is on the aggregator side (DB column, single-host auth token, Caddy on-demand TLS with an `ask` guard, nginx vhost). Details are in the feeder repo's PROJECT_STATE.md.

## Constraints
- Release rule (`.cursor/rules/release-checklist.mdc`): every push bumps `VERSION`, `web/VERSION`, `RELEASES.json` (new entry at the top, using the `notes` key) and the `env.example` header. Tarballs are never committed.
- Python changes need `docker compose up -d --build`.
- CLAUDE.md §6 and §8 were copied from another project. §8's host 209.182.235.108 is the sim_sensor server. The production host for this project (104.225.219.254) is now documented in `docs/REMOTE_SERVER.md` (2026-10-31, compiled from docs, no access exercised).

## Chunk D: make Config → Updates reliable (approved 2026-10-31). Code done in v1.0.419; deploy + end-to-end UI test pending
1. Gunicorn: switch to the `gthread` worker with threads, so the SSE stream isn't killed at 120 s and doesn't block other requests. Keep 1 worker (APScheduler/KLV must stay singletons). Check thread safety of `models.py`/SQLite and the module globals first.
2. Record the update outcome from inside the updater script (a marker file), and have the new dashboard log the `update_history` row on startup.
3. The updater builds all locally-built services (plain `docker compose build`), not just 4.
4. Name the updater container `taknet-updater`: keep the last one for its logs, replace it on the next run, refuse to start while one is running. Remove the leftover `vibrant_perlman` on the host.
5. UI: if the stream drops before `pre_restart`, poll the version instead of declaring failure.
6. Bump to v1.0.419, verify, push, deploy, and run a full web-UI update to confirm it completes and is recorded.

## Completed — Chunk B: NetBird fast-path port fix (v1.0.417, pushed with A on 2026-10-31)
- `_proxy_to_feeder` now sends dashboard traffic to feeder nginx :80, with a fallback to :5000 only on connection refused. Timeouts return 503 without a retry. tar1090 traffic still goes to :8080.
- Verified with py_compile and a stubbed-session test of all four branches. Not yet exercised against a real NetBird feeder.
- Risk: if a feeder's nginx emits absolute `Location`/URLs without a port, the existing :8080/:8082 rewrites won't catch them. The relay tunnel path (feeder v3.1.10) has the same exposure.

## Completed — Chunk A: repo cleanup (v1.0.416, pushed 2026-10-31)
Verified: py_compile on all web/ Python, `bash -n install.sh`, `docker compose config -q`, and no remaining references to the removed code. The images have not been rebuilt or deployed.
Note: `CLAUDE.md` has never been tracked in git. It is left untracked until §6/§8 are fixed (Chunk C).

1. Untrack the committed `.venv/` and every `__pycache__/` (`git rm -r --cached`). Both are already in `.gitignore`.
2. Remove the dead directories `taknet-api/` and `v139_work/`.
3. Remove the NATO icon feature: `web/nato_icons.py`, `GET /api/nato-icons` and `web/static/nato_icons/`. Also remove the unused `GET /api/ps-air-icons` endpoint and `web/static/img/taknetps.png`.
4. Add a `.dockerignore` (`__pycache__/`, `*.pyc`) to each custom build context.
5. `install.sh` rsync: exclude `ARCHIVE/`, `.venv/` and `__pycache__/` so local cruft never reaches /opt.
6. Bump to v1.0.416 (VERSION, web/VERSION, RELEASES.json, env.example), verify with py_compile and `docker compose config`, then commit. Push only after the operator OKs it.
Out of scope: the local untracked `ARCHIVE/*.tar.gz` and `*.db` files (gitignored operator data, left on disk), the README rewrite, and the port bug (Chunk B).

## Decisions
- 2026-10-31, operator: SSH as root to the production host is authorized for investigation (password kept in the gitignored `docs/REMOTE_SERVER.md`).
- 2026-10-31: no deletions until the operator approves a cleanup chunk.
- 2026-10-31, operator: the **Azure migration is cancelled**, and 104.225.219.254 stays production. The runbook is kept for its host inventory and marked cancelled.
