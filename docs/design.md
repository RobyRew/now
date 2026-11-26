# now.spotify.robyrew.com: design

Approved 2026-11-26, with the host changed to now.spotify.robyrew.com.

**What it is.** One always-on process on vps01 keeps a read-only Spotify Connect observer open for each account
(up to 9) and answers `GET /api/now` from memory. The profile deck then follows the music within the Worker's 15 s
reuse window, instead of the workflow's 5 minutes. Accounts are added on `/admin`, after signing in with RobyRew ID
(Logto, auth.robyrew.com).

## 1. Stack: a Python runtime, with the admin page built in Astro

- SpotAPI, and wreq (its Chrome-TLS transport), only exist in Python, so the observers run in Python: one asyncio
  process with Starlette + uvicorn. It runs with **1 worker**, because the observers live inside the process.
  `spotapi==2.1.3` and `wreq==0.12.3` are pinned, as in the workflows.
- The admin page is Astro + TS + Tailwind with @robyrew/ui, and no React. A `node:26-alpine` stage of the
  Dockerfile builds it to static files, and the Python process serves them. Only one runtime ships.
- Options not chosen:
  - **Astro SSR (@robyrew/astro-logto) with a Python sidecar.** That ships a second runtime, Node, at about
    80–120 MB, just for one admin page.
  - **Porting SpotAPI to TypeScript.** SpotAPI carries Spotify's TOTP token flow, the Chrome TLS fingerprint and
    the dealer protocol. A port means following Spotify's changes alone.
  - **The official "Log in with Spotify".** It needs a Spotify developer app, and the app needs a Premium owner.

## 2. RAM

Measured on this Mac (Python 3.13, arm64), with no Spotify connection open:

| stage | RSS |
|---|---|
| bare Python | 22 MB |
| + SpotAPI + wreq | 33 MB |
| + Starlette, uvicorn, Authlib, httpx, cryptography | 48 MB |
| + 9 SpotAPI clients (`AsyncClient.new`, offline) | 52 MB |

- **Measured after deploy (2026-11-26):** 3 accounts use about 150 MiB, roughly 30 MB per account over the
  45–70 MiB baseline. The limit was raised to 320 MiB. The paragraph below is the estimate made before.
- **Live connections are not measured yet.** Each one adds a dealer WebSocket, a TLS session and a player
  snapshot with its queue. The estimate is 2–5 MB each, so **about 70–100 MB with 9 accounts**.
- **vps01 today** (`free -m`): 1,935 MB total, 563 MB available, 1,437 MB of swap in use.
- **Container settings:**
  - Dokploy `memoryLimit` of 160 MiB (`"167772160"`), so a leak can't take the box down.
  - `HEALTHCHECK NONE`. The host's site probe checks `/healthz` instead.
  - RSS is measured again after the first day.

## 3. Where the sessions live

- **Storage.** A named volume `now-spotify-data`, mounted at `/data`, holds a SQLite database (Python's stdlib). It has
  one row per slot, 1–9, with:
  - the label, the username and the encrypted sp_dc;
  - the status;
  - the last record, meaning the public deck fields, so "latest play" survives a restart.
- **Encryption.** Each sp_dc is encrypted with AES-256-GCM (`cryptography`): a random nonce on every write, and the
  slot number as associated data.
  - The key is `NOW_KEY` in the Dokploy env, with a copy in the Ansible vault for disaster recovery. It is never
    on the volume, so the Restic→B2 backup holds only ciphertext.
  - `NOW_KEY_PREVIOUS` allows the key to be rotated.
- **Only `sp_dc=<value>` is kept.** If a full Cookie header is pasted, everything else in it is dropped.
- **The admin API is write-only for sessions.** No response ever contains a cookie.
- **Never logged:**
  - The service's own log lines carry only the slot, the event, the error type and the HTTP status, as data.py
    does now.
  - SpotAPI's `spotapi.*` loggers are silenced. A failed token refresh logs the exception with a traceback, and
    the dealer URL carries the access token in its query string.
  - An asyncio exception handler drops exception messages.
  - uvicorn's access log is off.
  - The cookie only travels in a POST body, so it never reaches Traefik's URL logs.
- **Infra** (standing rule): add `now-spotify-data` to `backup_docker_volumes` and `report_expected_app_mounts`, and add
  `/healthz` to the site probe.

## 4. The observers

- **One client and one observer per account.** Each account gets one `AsyncClient.from_cookies` (token renewal
  happens inside it) and one `PlayerStatus`.
  - `PlayerStatus` is SpotAPI's read-only base class. `Player`, which adds the playback commands, is never
    imported.
  - It registers SpotAPI's control-only device, named "robyrew-now".
- **Reconnects.** `start()` reconnects with backoff (1 s, doubling to 30 s) on network errors and on
  408/429/5xx.
- **Expired sessions.** A 401 (`LoginExpired`) is not retried by `start()`; it raises. The slot then shows
  "expired, paste a new sp_dc" on /admin. The service retries it at most every 6 h.
- **To measure first, with a real account: what happens when no device is online.** SpotAPI's `connect()` waits
  15 s for a first snapshot. Its error text says a Spotify player has to be open on the account. Without one,
  `connect()` fails and `start()` reconnects every 30 s or sooner.
  - If an idle account really sends no state, the observer subclass skips that wait. The socket then stays open,
    and the next play arrives by push.
- **Records.** Each `state_update` rebuilds that slot's record, with the same fields and rules as data.py's
  `spotify_observe()`. When title or artist is missing, the service does one catalogue lookup per track URI and
  caches the result.

## 5. What `/api/now` exposes

The endpoint is public, with no auth: the deck already shows the same data publicly.

```json
{ "now": { "...": "the record player's fields" }, "live": 6, "at": "2026-11-26T15:04:05Z" }
```

- **`now`** is the first account playing, in slot order. When nothing plays, it is the latest `when`, and the
  lower slot wins a tie, the same rule as data.py. It is `null` when there is nothing to show.
- **Fields in `now`:** playing, paused, title, artist, cover, context, context_kind, device, device_type, user
  (label or username), platform (`"Spotify"`), when (ISO minute), account (slot), song_url, artist_url, context_url,
  user_url, play_url. These are the record player's fields and nothing else.
- **Never in the answer:** cookies, tokens, the queue, volume, device ids, other accounts.
- **`live`** is the number of observers connected right now. Callers fall back when it is 0.
- **Headers and extra routes.** The answer is sent with `cache-control: no-store`. `GET /healthz` answers `ok`.

## 6. Changes to the profile repo (RobyRew/RobyRew)

- **Worker** (`.github/worker/now.js`):
  - Add a wrangler var `NOW_API`.
  - `lookUp()` asks it, with a 1.5 s timeout.
  - If it answers with `live > 0`, its `now` becomes the Spotify candidate. Otherwise the Worker uses
    `kit.spotify`, which is today's path.
  - The Last.fm part doesn't change.
- **data.py** (optional):
  - `now_data()` asks `/api/now` first, with a 5 s timeout.
  - If it answers with `live > 0`, the SpotAPI sign-ins are skipped. Otherwise it takes today's path.
  - While the service is up, GitHub's runner stops signing in to the accounts every 5 minutes. That is about
    1,700 web-player sign-ins a day for 6 accounts, all from GitHub's datacenter IPs.

## 7. Admin page (`/admin`)

- **Login:** a Logto code flow with PKCE, through Authlib (1.9.10, released 2026-11-20). Logto's own Python SDK
  last shipped in 2024-04, and it would pull in aiohttp and pydantic.
- **Logto app:** a new one, "Now playing".
  - Type Traditional Web, and **first-party**: as third-party apps, the earlier ones looped on `invalid_scope`.
  - Redirect URI: `https://now.spotify.robyrew.com/admin/callback`.
- **Access:** only the Logto user ids listed in `ADMIN_SUBS` get in.
- **Session:** a signed cookie `__Host-now` (Secure, HttpOnly, SameSite=Lax), valid for 12 h.
- **Writes:** JSON POST and DELETE requests, with an Origin check.
- **The page:**
  - It lists slots 1–9 with label, username, status (live, idle, expired, or the error type) and last seen.
  - You paste an sp_dc there, following the same steps as `spotify_login.py`. The service checks it by reading
    the account's profile, and shows which account it belongs to.
  - You can also remove a slot or rename it.

## 8. Risks

- **SpotAPI uses Spotify's private web-player API**, so an account can be flagged.
  - With this service, each account has one long-lived connection, which looks like an open web player.
  - Today, GitHub's runners sign in to every account every 5 minutes.
- **A change on Spotify's side breaks SpotAPI** until a new release comes out. The versions are pinned, and
  Dependabot watches them. While SpotAPI is broken, the deck falls back to Last.fm and to the Pages copy.
- **An sp_dc is a full account session**: it can do anything the web player can. That is the reason for the
  encryption, the write-only API and the absence of logs.

## Decisions (2026-11-26)

1. Python runtime with an Astro-built static admin page.
2. Repo `RobyRew/now`, public; host `now.spotify.robyrew.com`.
3. `SPOTIFY_COOKIE_1..9` stay in the profile repo's secrets as the fallback path.
4. /admin: only the `robyrew` Logto user (w2nbkvwmzzyp).

## Found while building

- auth.robyrew.com signs ID tokens with ES384 (its JWKS holds one EC P-384 key). joserfc 1.8 refuses ES384 unless
  the caller lists it; Authlib passes the discovery document's `id_token_signing_alg_values_supported`, which lists
  it. The tests sign with ES384 the same way.
- Authlib 1.9 and Starlette's test client use `httpx2`; plain `httpx` is a deprecated fallback in both.
