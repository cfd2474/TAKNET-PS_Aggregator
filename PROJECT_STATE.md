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

## Production load baseline (measured 2026-11-03, for AWS sizing)
- Host: 8 vCPU Xeon Silver 4216, 32 GB RAM, 480 GB disk (40 GB used). CPU 74–83% idle (about 2 cores busy); about 2 GB RAM used by the stack.
- **VPS CPU topology is poor (operator, 2026-11-03):** the provider presents each vCPU as its own socket with no L3 cache.
  - `lscpu` confirms 8 sockets × 1 core × 1 thread on KVM. The guest does report a 16 MB L3, but KVM passes the host's cache info through, so this doesn't prove the cache is usable.
  - Per-core throughput here is worse than the spec suggests, so CPU % from this box overstates what AWS will need, which keeps the AWS sizing conservative.
  - Benchmark numbers from this box should only be compared before/after on the same box, never extrapolated to AWS.
- Workload: 14 active feeders (13 NetBird, 1 public), 20 offline. About 7,300 merged aircraft, mostly from inbound JSON push feeds. 39 outputs (23 CoT, 9 JSON, 6 adsb_direct, 1 beast_raw).
- Busiest processes: dashboard/CoT pipeline 43–66% of one core, aircraft-merger 30–40%, netbird client about 28%, Caddy about 25%, beast-proxy about 15%.
- Network: host NIC about 16 Mbit/s in and 15 Mbit/s out (live sample). 18-day average about 130 GB/day each way, roughly 4 TB/month egress.
- **Scaling walls (architecture, not instance size):** the dashboard (CoT pipeline) and the merger are each a single Python process, so the GIL caps each at about 1 core however big the VM is.
- **Data findings:** `connections` has 7.3 M rows and is never pruned (DB is 1 GB). `cot_transforms` has 536 k rows for 39 outputs, which is a likely duplication bug. Not yet investigated.
- **Investigated 2026-11-03 (read-only on prod):**
  - `cot_transforms`: no duplicates within any output. **451,725 rows (84%) are orphans** of deleted outputs, because `OutputModel.delete` (`web/models.py:1238`) never removes an output's transforms. Output ids are AUTOINCREMENT (seq 128), so orphans cannot leak into a new output. They only waste space.
  - `connections`: about 36 k new rows a day. All 13 NetBird feeders plus the public ones open a new Beast connection **every ~30 s**, and **no row ever gets `disconnected_at`**.
  - beast-proxy (up 3.3 h): 526 fds, about 450 established sockets, 47 "New connection" log lines in 2 min and 0 "Disconnected".
  - Cause (from code): `handle_client` gathers both forward directions. The readsb→feeder side never sees EOF, so `gather` never returns, `finally` never runs, and the sockets leak.
  - **Unknown:** why feeders reconnect every 30 s. To be asked of the feeder agent.

## Constraints
- Release rule (`.cursor/rules/release-checklist.mdc`): every push bumps `VERSION`, `web/VERSION`, `RELEASES.json` (new entry at the top, using the `notes` key) and the `env.example` header. Tarballs are never committed.
- Python changes need `docker compose up -d --build`.
- CLAUDE.md §6 and §8 were copied from another project. §8's host 209.182.235.108 is the sim_sensor server. The production host for this project (104.225.219.254) is now documented in `docs/REMOTE_SERVER.md` (2026-10-31, compiled from docs, no access exercised).

## Chunk H: ADSB Direct output speaks the ADSBExchange RapidAPI convention (2026-11-08)
**Status:** implemented as v1.0.424 and tested locally; committed but not pushed.
- Plugin facts (operator's screenshot of the plugin's API Config): Server Mode "ADSBEx RapidApi" has an API Key field and an API URL field (default `https://adsbexchange-com1.p.rapidapi.com/v2`). It requests `<base>/lat/44.010000/lon/-88.580000/dist/120/`, so the `lat44.01` form in the request was a typo and is not supported. The header name is still inferred from RapidAPI convention (`X-RapidAPI-Key`), not observed.
- The edit modal's Base URL panel (`adsb-direct-url-wrap`) had lost its markup in v1.0.342 (c15a4fb) while its JS stayed. Restored it, showing the keyless base.
- `openCreateModal` now clears `window._outputModalOutput`, so a previously edited output's key cannot appear in a new output's instructions.
- Tests (Flask test client, mocked aircraft.json):
  - The key is accepted from the header, from `api-auth` and from `?key=`.
  - A missing or invalid key → 401; a key for another output type → 400; a bad lat → 400.
  - Works with and without the trailing slash.
  - Range, altitude and network filters behave as before; dist is capped at 250.
  - The legacy path-key route gives the same result; an inactive output → 401.
  - In Node, the panel shows the base URL only once a key exists, and the instructions never contain the key.
- Request (operator): the plugin that ingests the ADSB Direct output changed. It now takes a base URL plus a separate API key, like the ADSBExchange RapidAPI (`https://adsbexchange-com1.p.rapidapi.com/v2`), and appends `/lat/<lat>/lon/<lon>/dist/<nm>/`. The output details should show only the base URL and the key.
- Today: `output_type = adsb_direct`. The key is embedded in the path: `/api/outputs/direct/<key>/point/<lat>/<lon>/<nm>`. The response uses `aircraft`.
- Prod: 6 ADSB Direct outputs (ids 38, 43, 45, 111, 122, 128). No key has been used since 2026-10-06, which suggests the plugin already stopped calling the old URL.
- Assumptions, to be confirmed against the plugin, which we don't have:
  - Key header: `X-RapidAPI-Key` (RapidAPI convention). Also accept `api-auth` (ADSBx's own direct API) and `?key=`. `X-RapidAPI-Host` is ignored.
  - Response: ADSBx v2 uses `ac`. Return `ac` and also keep `aircraft`, plus `msg`, `now`, `total`, `ctime`, `ptime`.
  - `dist` is in nautical miles (ADSBx), capped at 250.
- Plan:
  1. Route `GET /api/outputs/direct/v2/lat/<lat>/lon/<lon>/dist/<dist>/`, trailing slash optional. Key from header or query → `OutputKeyModel.validate`; must be `adsb_direct`.
  2. Move the shared filtering (range, network ADS-B, altitude, category/registration stripping) out of `output_adsb_direct_point` into one function. Keep the old path-key route as a thin wrapper, so existing configs keep working.
  3. Response gains `ac` alongside `aircraft`.
  4. UI (`outputs.html`): the edit-modal URL panel and the key-generated modal show **Base URL** `https://<host>/api/outputs/direct/v2` and the **API key**, each with Copy.
  5. Tests: header/query/missing/wrong-type key, both path forms, filtering parity with the old route, response shape. Release v1.0.424.

## v1.0.423: "Test TLS connection" showed no result (2026-11-07)
- Symptom (operator): clicking Test TLS shows "Testing…", then nothing.
- Evidence:
  - On prod, `test_cot_tls_handshake` returns within 0.0–0.5 s for most outputs and within 8.1 s at worst (the connect timeout), so the request does not hang.
  - The JS in `outputs.html` writes the result, then calls `updateCotTlsPausedBanner()`, which clears the message once `dataset.sticky` is gone. This has been there since v1.0.347 and is not a v1.0.422 regression.
- Fix: refresh the banner before writing the result.
- Verified by running the handler in Node with a stubbed DOM. The old code leaves an empty message on both pass and fail; the new code shows the green or red result.
- Also seen: "SamTAK Emergency" (id 67) has no client cert uploaded, and both TN TriCities outputs (31, 119) cannot reach their servers.

## Chunk G: CoT output pause is too aggressive (approved 2026-11-04: 5 min / 6 h limits; emails go to output creators only)
**Status:** implemented as v1.0.422, tested locally and pushed. Deployed by the operator (prod reported 1.0.422 on 2026-11-07); 48 h of logs show no pauses, rejections or tracebacks, but a real outage has not exercised it yet.
- Design:
  - `web/services/cot_output_health.py` holds the pure policy with an injectable clock: failure window, backoff, rejection streak, recheck cadence, email rate limit, and the `paused_config` patch.
  - `cot_pipeline` reports each connect outcome to it.
  - `_connect_cot_socket` now returns `(sock, ConnectOutcome)`.
  - `drop_cot_persistent_socket` also clears the failure window.
  - The `cot_tls_fail_count` config key is no longer used.
  - New config keys: `cot_tls_paused_at`, `cot_pause_emailed`, `cot_pause_last_email_at`.
- Tests:
  - Policy, with a fake clock: blip; 3.5-min restart, no pause; sustained outage, pause at 300 s; 3 rejections, pause at 6 s; rejection streak reset; classification; recheck cadence; 6 h email limit.
  - End to end through `_run_cot_sender_cycle_impl` (scratch DB, scripted server):
    - The LECK restart pattern no longer pauses and makes 11 connect attempts in 210 s, against about 70 before.
    - A sustained outage pauses at 300 s with 1 email. Rechecks come every 120 s, with no sends while paused. Recovery sends the "resumed" email and the feed flows again.
    - A second pause within 6 h sends no email and no "resumed" email.
  - Test TLS route, via the Flask test client: a failure pauses; a repeat failure keeps `paused_at` and the emailed flag; success unpauses.
- Open (not in this chunk): the test6/test12 outputs' constant connection resets after a successful handshake. With TLS 1.3 a server's client-certificate rejection arrives *after* the handshake, on the first send. Those failures count as successful connects, so they never pause; they reconnect every few cycles.
**Evidence (prod logs, 05:34–16:28 UTC 2026-11-04, the only logs since the container was recreated):**
- 1 pause: "LECK CoT".
  - 16:25:37 broken pipe, :41 refused, :48 timeout, :56 timeout. **Paused and emailed 19 s after the first error.** This looks like a TAK server restart.
  - Port 8060 was reachable again at 16:29. The output stays paused until the 10-min recheck, and the email tells the user that manual "Test TLS" is required.
- Separately: about 13.9 k "send failed" (connection reset / broken pipe) in 11 h across 3 test outputs (`takserver.test6.takwerx.com`, `takserver.test12.taktical.net`). They never pause, but each one reconnects (a TLS handshake) every few cycles.

**Causes (`web/cot_pipeline.py`):**
- `:1714`: 3 consecutive connect failures trigger a pause. At a 2 s cycle with 3 s connect timeouts that is about 15–20 s of outage.
- `:1709`: a *single* TLS handshake error pauses immediately. `_connect_cot_socket` `:1175` classes every non-timeout handshake exception as a TLS error, including reset/EOF while a server is starting.
- `:1236`: the email is sent on every pause. There is no rate limit and no "resumed" mail, and it says "suspended until manual handshake" although the auto-recheck (`:1401`, every 10 min) silently resumes the feed.

**Plan:**
1. Pause on time, not count: pause only after failing continuously for `COT_PAUSE_AFTER_SECONDS` (default 300). Until then, retry with backoff (2 s doubling to 30 s) so a down server isn't hit every cycle.
2. Classify handshake failures: an `SSLError` carrying a TLS alert (bad/unknown/expired certificate, unknown CA) is a real rejection and pauses after 3 in a row. Reset/EOF/timeout during the handshake is transient and goes into the time window from item 1.
3. Paused outputs: recheck every 2 min for the first 30 min, then every 10 min.
4. Email:
   - Pause mail only after the threshold, at most once per output per 6 h, with corrected wording (auto-retrying; Test TLS is optional).
   - A short "resumed" mail if auto-recheck clears a pause that was emailed.
5. Unit-test the state machine (transient blip, server restart under 5 min, sustained outage, cert rejection, recovery) with a fake clock and socket. Bump the version, push, and the operator deploys via Config → Updates.

## Chunk F: optimize on the current box (2026-11-03: operator approved items 1, 3, 4 only)
**Status:** 1, 3 and 4 are implemented as v1.0.421, verified locally, pushed (b3ef5bf), and **deployed and verified 2026-11-04**.
- Deploy: the operator ran Config → Updates 1.0.420 → 1.0.421. It was recorded as `Web UI update (run 1793770425)`, success. This is the second clean web-UI update.
- beast-proxy at 11 min uptime: 39 fds and 28 established sockets (before: 526 and about 450 at 3.3 h).
  - Last 5 min: 128 new connections and 127 disconnects.
  - Of the connections opened in the last 10 min, 242 closed and 13 are open, one per live feeder.
  - Status reports 14 feeders, which matches the DB (14 active).
- First maintenance run: removed 451,725 `cot_transforms`, 40 `output_cot_certs`, 39 `output_api_keys` and 6,172,888 `connections` rows.
- VACUUM at 05:45 UTC: took 5.0 s, `quick_check` ok, DB file went from 1,051 MB to 206 MB. Afterwards: 0 lock or error lines in beast-proxy, dashboard or api, and no feeders dropped.
- Merged aircraft at 05:45 UTC was 2,798 (the baseline of about 7,300 was daytime). Assumed to be diurnal; not compared like for like.
- Items 2, 5 and 6 are not started. Item 2 (30 s reconnects) is now the main source of DB churn: about 250 disconnect activity rows per 10 min.
- Item 1:
  - Keepalive was dropped: the existing 120 s idle timeout already closes dead feeders, and closing the transport gives the reader an EOF.
  - Added `log_disconnection(mark_offline=False)` for a feeder that has another live session.
  - Test: a local asyncio harness with a readsb stub that never closes. HEAD records no disconnect at all (leak reproduced). The fix records both disconnects with byte counts and marks offline only on the last session.
- Item 3: `OutputModel.delete` now deletes `cot_transforms`, `output_cot_certs` and `output_api_keys`. `output_drop_signals` is kept for beast-proxy. The schema's `ON DELETE CASCADE` is inert because `PRAGMA foreign_keys` is never enabled; turning it on globally was rejected as too broad.
- Item 4:
  - `services/db_maintenance.py`, scheduled hourly (first run 2 min after start), with `MaintenanceModel` in models.py.
  - Batched 5,000-row deletes. Retention is `CONNECTION_RETENTION_DAYS`, default 30.
  - Local timing: 6.37 M rows in 12 s, 9 ms per batch.
  - VACUUM was **not** automated. It takes an exclusive lock longer than beast-proxy's 5 s busy_timeout, so it needs a quiet window. Without it the freed pages are reused and the file stops growing.
- Behavior change to watch after deploy: disconnects now get recorded, so `activity_log` gains about 36 k "feeder_disconnected" rows a day (7-day retention) until the 30 s reconnects (item 2) are understood.
1. beast-proxy: end the session when *either* direction ends (`asyncio.wait(FIRST_COMPLETED)`, cancel the other side, close both). This fixes the socket and fd leak and makes disconnects get recorded. Add TCP keepalive so dead peers are noticed.
2. Find out why feeders reconnect every 30 s: troubleshoot with the feeder agent (`taknet-ps-ads-b-feeder-cb`) using feeder logs. No fix until there is evidence.
3. Make output delete also remove its `cot_transforms` (and certs and keys), plus a one-time purge of the 451 k orphan rows.
4. Retention: prune `connections` older than 30 days in the existing cleanup job. Then run a one-time `VACUUM` in a quiet window (1 GB DB, a few seconds of write lock).
5. Measure before tuning: CoT phase timing (the built-in flag), plus Caddy (44% CPU), nginx (24%) and merger (50%) request and CPU profiles. Findings only; no changes in this chunk.
6. Bump the version, verify locally, push, deploy, then re-measure CPU, fds and DB size against the 2026-11-03 baseline.

## Chunk D: make Config → Updates reliable (approved 2026-10-31). v1.0.419 deployed 2026-10-31 via `taknet-agg update` (gthread worker confirmed, CoT flowing, leftover `vibrant_perlman` removed). End-to-end test PASSED 2026-11-03: the operator ran Config → Updates 1.0.419 → 1.0.420, and it was recorded as `Web UI update (run 1793737065)`, success
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
- 2026-11-03, operator: planning a move to **AWS** for national scale (CONUS public-safety agencies, many more feeders; future Remote ID and AIS).
- 2026-10-31, operator: the **Azure migration is cancelled**, and 104.225.219.254 stays production. The runbook is kept for its host inventory and marked cancelled.
