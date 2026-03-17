# Feeder owners (access control)

Each feeder has an **Owners** field: a list of usernames (JSON in the database).

## Who can do what

| Role | Feeder list | Feeder detail / edit | Connect & tunnel |
|------|-------------|----------------------|------------------|
| **admin** | All feeders | All fields, including Owners | Always |
| **network_admin** (and others with feeder access) | Only feeders where they are an owner | Only those feeders (no Owners edit) | Only if listed as owner |
| Feeders with **empty** owners | Admin only | Admin only | Admin only |

## After upgrade

Existing feeders start with **no owners** → only **admin** sees them until an admin opens each feeder (or new ones) and checks the appropriate users under **Owners**.

## Tunnel URL

Non-admins may only use `/feeder/<tunnel_id>/…` when:

1. The tunnel id matches a feeder row in the dashboard, and  
2. Their username is in that feeder’s **Owners** list.

Admins may still tunnel even when there is no matching dashboard row (e.g. troubleshooting).

## API

- `PUT /api/feeders/<id>` — only **admin** may send `owners` (array of usernames). Others’ updates ignore `owners`.
- `GET /api/users/usernames` — **admin** only; lists active usernames (for tooling).
