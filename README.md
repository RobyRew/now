# now

Always-on Spotify Connect observers for the record player on the [RobyRew GitHub profile](https://github.com/RobyRew),
served as `GET /api/now`. Meant to run at `now.spotify.robyrew.com` (Dokploy on vps01).

One read-only SpotAPI observer per Spotify account (up to 9) stays connected and receives each account's player
state as it changes. `/api/now` answers from memory: the first account playing, else the latest play. Accounts are
added on `/admin`, after signing in with RobyRew ID (Logto), by pasting each account's `sp_dc` cookie.

Why it is built this way (stack, RAM, where the sessions live, what the API exposes): [docs/design.md](docs/design.md).

## Endpoints

| Path | Who | What |
|---|---|---|
| `GET /api/now` | anyone | `{"now": {...} \| null, "live": n, "at": "..."}`: the record player's fields (below), `live` = observers connected now |
| `GET /healthz` | the host's site probe | `ok` |
| `/admin/` | RobyRew ID users in `ADMIN_SUBS` | the accounts page (static Astro build) |
| `/admin/login`, `/admin/callback`, `POST /admin/logout` | | Logto sign-in (authorization code + PKCE) |
| `GET /admin/api/accounts`, `PUT/PATCH/DELETE /admin/api/accounts/{1-9}` | admin session | list, add/replace a session (`{"sp_dc": "..."}`), rename (`{"label": "..."}`), remove |

`now` holds: `playing`, `paused`, `title`, `artist`, `cover`, `context`, `context_kind`, `device`, `device_type`,
`user`, `platform`, `when`, `account`, `song_url`, `artist_url`, `context_url`, `user_url`, `play_url`: the same
fields and rules as `.github/profile/data.py` in RobyRew/RobyRew. Nothing else leaves the service.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `NOW_KEY` | yes | 32 random bytes, base64 (`openssl rand -base64 32`). Seals the sessions at rest. Keep a copy in the Ansible vault: without it the stored sessions cannot be opened. |
| `NOW_KEY_PREVIOUS` | no | The key before a rotation; sessions sealed with it open and are sealed again with `NOW_KEY`. |
| `SESSION_SECRET` | yes | 32+ characters (`openssl rand -base64 48`). Signs the admin session cookie. |
| `PUBLIC_URL` | no | Default `https://now.spotify.robyrew.com`. Redirect URIs and the CSRF origin check derive from it. |
| `LOGTO_ENDPOINT` | no | Default `https://auth.robyrew.com`. |
| `LOGTO_APP_ID`, `LOGTO_APP_SECRET` | for /admin | The Logto app (below). Without them /admin answers 503 and `/api/now` keeps working. |
| `ADMIN_SUBS` | for /admin | Comma-separated Logto user ids allowed into /admin. |
| `DATA_DIR` | no | Default `/data` (the named volume). |
| `PORT` | no | Default `8080`. |

## Deploying

1. **Logto** (console on the tailnet): new application, type *Traditional Web*, **first-party**. Redirect URI
   `https://now.spotify.robyrew.com/admin/callback`. Its App ID and secret go into the Dokploy env.
2. **DNS**: A `now.spotify` → 94.143.138.233 and AAAA → 2001:ba0:20d:3c00::1 in the robyrew.com zone, before the
   Dokploy domain is created (Traefik asks Let's Encrypt at once).
3. **Dokploy** (Tools environment): application `now-spotify` from the image `ghcr.io/robyrew/now:latest`, port
   8080, `memoryLimit` `"167772160"` (160 MiB), named volume `now-spotify-data` mounted at `/data`, the variables
   above, domain `now.spotify.robyrew.com` with Let's Encrypt. The repository secret `DOKPLOY_DEPLOY_WEBHOOK` holds
   the app's deploy webhook; CI calls it after each image push to main (`request_deploy: true`).
4. **Infrastructure repo**: `now-spotify-data` into `backup_docker_volumes` and `report_expected_app_mounts`, and the host
   into the site probe (`/healthz`).

The image has no Docker healthcheck on purpose: exec probes leaked pids and kernel memory on vps01 (2026-09).

## Local run and tests

```bash
python3 -m venv .venv.nosync
.venv.nosync/bin/pip install --require-hashes -r requirements.txt
.venv.nosync/bin/python -m unittest discover -s tests -t .
(cd admin && npm ci --ignore-scripts && npm run build)
.venv.nosync/bin/python scripts/dev.py
```

`scripts/dev.py` runs the service unchanged on http://localhost:4350 with a fake Spotify (scripted accounts) and a
fake Logto on :4351. The tests use the same fakes and never reach Spotify or Logto. `.venv.nosync` keeps the
virtualenv out of iCloud.

## Dependencies

`requirements.in` lists the pinned runtime packages; `requirements.txt` is generated from it with hashes:

```bash
uv pip compile requirements.in --universal --generate-hashes --python-version 3.13 --no-header --annotation-style line -o requirements.txt
```

## Sessions and logs

- An `sp_dc` is a whole Spotify web session. It travels only in the body of an admin request and is checked with
  Spotify (the account's profile names the user). It is then sealed with AES-256-GCM (the slot number is bound in as
  associated data) and stored in SQLite on the volume. The admin API never returns it.
- Log lines carry a slot, an event, an error type and an HTTP status. SpotAPI's own loggers are off, and no log
  line prints a traceback: exception messages and URLs can carry a cookie or a token.

## Credits

- **[SpotAPI](https://github.com/Aran404/SpotAPI)** by [Aran404](https://github.com/Aran404) does the hard part: the
  Spotify web-player sign-in from an `sp_dc` cookie, the Spotify Connect dealer connection and the player state this
  service reads. `now/observer.py` builds on its `PlayerStatus` class. SpotAPI is licensed under the GNU GPL v3.0;
  the published image ships it unchanged, with its license in `spotapi-2.1.3.dist-info/licenses/LICENSE`.
- **[wreq](https://github.com/0x676e67/wreq-python)** by 0x676e67 is the HTTP client underneath SpotAPI, with a
  Chrome TLS fingerprint (Apache-2.0).

## License

Apache-2.0 for this repository's own code. SpotAPI, which it imports, is GPL-3.0; Apache-2.0 code can be combined
with GPL-3.0 code, and the combined image is distributed under the terms of both.
