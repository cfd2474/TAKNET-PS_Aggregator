# Authentik Setup Guide — TAKNET-PS Aggregator (Practical Path)

This guide walks through installing Authentik via Docker and configuring it for **aggregator dashboard login first**. NetBird/LDAP can be added later.

**Principle:** Run Authentik as its **own Docker Compose project** (like NetBird). Do not merge it into the taknet-aggregator compose file.

---

## 1. Where to Run Authentik

- **Same VPS as aggregator:** Use a dedicated directory (e.g. `/opt/authentik`) and different ports so Authentik does not conflict with nginx (80/443) or the aggregator.
- **Dedicated host:** Simpler long-term; run Authentik on 80/443 there and point a subdomain (e.g. `auth.tak-solutions.com`) at it.

**Suggested (same host):** Authentik on ports **9000 (HTTP)** and **9443 (HTTPS)**. Put it behind nginx later or access via `https://auth.tak-solutions.com:9443` until you reverse-proxy.

---

## 2. Install Authentik with Docker Compose

On the server (or the host you chose):

```bash
# Dedicated directory — keep it separate from /opt/taknet-aggregator
sudo mkdir -p /opt/authentik
cd /opt/authentik

# Download official compose file
sudo curl -sSL -o docker-compose.yml https://goauthentik.io/docker-compose.yml

# Or if that URL fails, use the documented one:
# sudo curl -sSL -o docker-compose.yml https://raw.githubusercontent.com/goauthentik/authentik/version-2024.6/docker-compose.yml
# (Check https://goauthentik.io/docs/installation/docker-compose for current URL)
```

**Generate secrets and create `.env`:**

```bash
cd /opt/authentik

# Required: PostgreSQL password and Authentik secret key
echo "PG_PASS=$(openssl rand -base64 36)" | sudo tee -a .env
echo "AUTHENTIK_SECRET_KEY=$(openssl rand -base64 60)" | sudo tee -a .env

# Use non-default ports so they don't clash with aggregator nginx (80/443)
echo "COMPOSE_PORT_HTTP=9000" | sudo tee -a .env
echo "COMPOSE_PORT_HTTPS=9443" | sudo tee -a .env
```

**Optional but recommended:**

```bash
# Timezone (match your aggregator)
echo "TZ=America/Los_Angeles" | sudo tee -a .env

# Error reporting (optional)
# echo "AUTHENTIK_ERROR_REPORTING__ENABLED=true" | sudo tee -a .env
```

**Start Authentik:**

```bash
cd /opt/authentik
sudo docker compose pull
sudo docker compose up -d
```

Wait 1–2 minutes for migrations. Check:

```bash
sudo docker compose ps
sudo docker compose logs server --tail 30
```

---

## 3. First-Time Setup in the Web UI

1. **Open the UI**  
   - If same host: `https://SERVER_IP:9443` or `http://SERVER_IP:9000`  
   - Accept the self-signed cert warning if no reverse proxy yet.

2. **Create the initial admin user**  
   - Set username, email, and password. This is the only account that can manage Authentik.

3. **Configure the outpost (if prompted)**  
   - Default “authentik Embedded Outpost” is enough for OIDC. Ensure it’s enabled for the application you’ll create.

---

## 4. Create Groups for Aggregator Roles

We map Authentik **groups** to aggregator roles: **admin**, **network_admin**, **viewer**.

1. In Authentik: **Directory → Groups → Create**  
2. Create three groups (names are examples; you can change them):

   | Group name (example)     | Aggregator role   |
   |--------------------------|-------------------|
   | `aggregator-admins`      | admin             |
   | `aggregator-network-admins` | network_admin  |
   | `aggregator-viewers`    | viewer            |

3. For each group, add the users who should have that role.  
4. **Optional:** Create an “aggregator-users” group and add everyone who may log in; the aggregator can require membership in that group and then derive role from the three above.

---

## 5. Create the OAuth2/OIDC Provider and Application

1. **Applications → Applications → Create with provider.**

2. **Provider type:** Choose **OAuth2/OpenID Connect**.

3. **Configure OAuth2/OpenID Provider:**
   - **Name:** `TAKNET-PS Aggregator`
   - **Client type:** Confidential
   - **Redirect URIs:** Add the aggregator callback URL. Must match exactly what the Flask app will use:
     - Production: `https://adsb.tak-solutions.com/auth/callback`
     - If you test with HTTP or another host/port, add those too (e.g. `http://localhost:5000/auth/callback`).
   - **Scopes:** Ensure **openid**, **profile**, **email** are included.  
   - **Scope mapping (optional but useful):** Add a mapping so the aggregator can read groups:
     - Scope name: `groups`  
     - Expression: `groups` (or the attribute that returns the user’s group names).  
     - This lets the Flask app map Authentik groups → admin / network_admin / viewer.

4. **Submit** the provider, then on the **New application** page:
   - **Name:** e.g. `TAKNET-PS Aggregator`
   - **Slug:** e.g. `taknet-aggregator` (used in URLs)
   - **Provider:** Select the provider you just created.
   - **Launch URL (optional):** `https://adsb.tak-solutions.com`
   - Save.

5. **Get credentials:**
   - Open **Applications → Providers** → your provider → **Edit**.
   - Note:
     - **Client ID**
     - **Client Secret** (generate/copy once; you’ll put it in the aggregator’s env).

6. **Issuer / discovery URL** (for the aggregator’s OIDC config):
   - If Authentik is at `https://auth.tak-solutions.com` (or `https://SERVER:9443`):
     - Issuer: `https://auth.tak-solutions.com/application/o/taknet-aggregator/`  
       (Replace `taknet-aggregator` with your application **slug**.)
   - OIDC discovery: `https://auth.tak-solutions.com/application/o/taknet-aggregator/.well-known/openid-configuration`  
   - The aggregator will use this to get authorize/token/userinfo endpoints.

---

## 6. Optional: Put Authentik Behind nginx (Same Host)

If you want `https://auth.tak-solutions.com` on 443 instead of 9443:

1. In Authentik `.env` you can leave `COMPOSE_PORT_HTTP=9000` and `COMPOSE_PORT_HTTPS=9443`, and bind nginx to 443 for `auth.tak-solutions.com` proxying to `http://localhost:9000` (or 9443 with SSL to backend).
2. Or set `COMPOSE_PORT_HTTP=80` and `COMPOSE_PORT_HTTPS=443` only if the aggregator’s nginx is not using 80/443 on this host (e.g. aggregator on another machine).  
3. Add a server block in nginx (or in the aggregator’s nginx config) that proxies `auth.tak-solutions.com` to the Authentik server container (e.g. `proxy_pass http://127.0.0.1:9000`).  
4. After that, use `https://auth.tak-solutions.com` as the base URL in Issuer and redirect URIs.

---

## 7. Values to Give the Aggregator (Next Step)

When we add OAuth to the Flask app, you’ll need:

| Variable            | Example value |
|---------------------|----------------|
| `AUTHENTIK_ISSUER`  | `https://auth.tak-solutions.com/application/o/taknet-aggregator/` |
| `AUTHENTIK_CLIENT_ID` | (from provider) |
| `AUTHENTIK_CLIENT_SECRET` | (from provider) |
| `AUTHENTIK_REDIRECT_URI` | `https://adsb.tak-solutions.com/auth/callback` |
| `AUTHENTIK_SCOPE`   | `openid profile email groups` (if you added groups scope) |

The aggregator will:
- Redirect unauthenticated users to Authentik.
- Accept the callback at `/auth/callback`, exchange code for tokens, read userinfo (and groups if configured).
- Map Authentik groups → `admin` / `network_admin` / `viewer` and create or update a local session (we can keep a minimal SQLite record for role and display name, or derive everything from the token).

---

## 8. Checklist

- [ ] Authentik running in its own directory (`/opt/authentik` or similar).
- [ ] Initial admin user created.
- [ ] Groups created: `aggregator-admins`, `aggregator-network-admins`, `aggregator-viewers` (or your names).
- [ ] OAuth2/OIDC provider + application created for the aggregator.
- [ ] Redirect URI = `https://adsb.tak-solutions.com/auth/callback` (and any dev URLs).
- [ ] Client ID and Client Secret noted; Issuer URL known.
- [ ] Optional: Authentik behind nginx at `https://auth.tak-solutions.com`.
- [ ] Ready to hand off: AUTHENTIK_ISSUER, AUTHENTIK_CLIENT_ID, AUTHENTIK_CLIENT_SECRET, AUTHENTIK_REDIRECT_URI (and optional AUTHENTIK_SCOPE) for the next step (Flask OAuth integration).

---

## 9. Troubleshooting

- **Compose file 500 / not found:** Check the official docs for the current compose URL:  
  https://goauthentik.io/docs/installation/docker-compose  
  They sometimes move the file; the project’s GitHub also has `docker-compose.yml` in the repo root.
- **Redirect URI mismatch:** The callback URL in Flask must match **exactly** (scheme, host, port, path). No trailing slash unless both have it.
- **Groups not in token:** Ensure scope mapping for `groups` is set and that the provider includes that scope; then request `groups` in the aggregator’s scope list.
- **NetBird later:** Same Authentik instance can expose LDAP (for NetBird) and OIDC (for the aggregator); add the LDAP provider and NetBird config when you’re ready.

Once this is done, the next step is implementing the Flask OAuth flow and role mapping in the aggregator codebase.
