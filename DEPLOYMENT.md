# Deployment

This guide covers persistent Docker operation, reverse proxies, OAuth, and optional maintenance. For the project overview, native setup, normal workflow, and troubleshooting, start with [README.md](README.md).

The default Compose deployment is self-contained and reverse-proxy agnostic. It binds the dashboard to `127.0.0.1` only, so another machine cannot connect unless the administrator deliberately adds a reverse proxy or changes the port binding.

## First start

1. Copy `.env.example` to `.env` and replace all three authentication placeholders. Keep the password hash single-quoted in `.env` so its `$` characters remain literal.
2. Generate a Werkzeug password hash without putting the plaintext password in a file:

   ```console
   python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('Password: ')))"
   ```

3. Generate the session secret independently:

   ```console
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. Start the service:

   ```console
   docker compose up -d --build
   ```

Open `http://127.0.0.1:8787`. The container will refuse to start if the required username, Werkzeug password hash, or session secret is missing or invalid. Do not set `VOD_DASHBOARD_AUTH_DISABLED` in a deployment.

Fresh installations do not enable browser-cookie extraction. Docker does not install Firefox. If Twitch requires authentication, mount `./data/twitch-cookies.txt` and set `VOD_DASHBOARD_TWITCH_COOKIE_FILE=/data/twitch-cookies.txt`; do not add a browser dependency to the container.

## Persistent layout

| Host path | Container path | Purpose |
| --- | --- | --- |
| `./data` | `/data` | Dashboard settings, streamer/archive lists, durable `jobs.json` history, OAuth files, and bounded log files |
| `./downloads` | `/downloads` | The administrator-controlled media root and downloaded VODs |

The directory mounts bootstrap cleanly; no pre-existing personal settings file is required. The application creates settings and list files as needed. The container starts as root only long enough to prepare mount ownership and private-file modes, then runs Gunicorn as `PUID`/`PGID` (both default to `1000`). Set those IDs to the owner of existing host data when necessary. The entrypoint does not recursively change ownership of the media library.

Back up `./data` before upgrades. It contains the small persistent configuration, archive, credential, and token state required to reproduce the dashboard configuration. Back up `./downloads` separately according to whether the downloaded media can be recreated.

New runtime files use a restrictive umask. Known settings, list, cookie, OAuth, and log files under `/data` are set to mode `0600` where the host filesystem supports Unix permissions. `/data` is mode `0700`. The Twitch cookie file remains writable because yt-dlp may update it.

Optional credentials and state use these neutral locations:

- Twitch cookies: place the file at `./data/twitch-cookies.txt`, then set `VOD_DASHBOARD_TWITCH_COOKIE_FILE=/data/twitch-cookies.txt`.
- YouTube OAuth client: `./data/client_secret.json` on the host, mounted as `/data/client_secret.json`.
- YouTube token: `./data/youtube-token.json` on the host, mounted as `/data/youtube-token.json` and refreshed there across restarts.

## YouTube OAuth for Docker

Docker uses `VOD_DASHBOARD_YOUTUBE_OAUTH_MODE=external` by default. The dashboard therefore never starts an unpredictable localhost callback listener inside the container. Its **Connect YouTube** action instead explains that the administrator must bootstrap the token outside the container. No callback port needs to be published.

1. In a Google Cloud project, enable the **YouTube Data API v3**.
2. Configure the OAuth consent screen. Add the intended Google account as a test user if the application remains in testing mode.
3. Create an OAuth client of type **Desktop app**. Download its JSON file to `./data/client_secret.json`.
4. Install this repository's Python dependencies in a native Windows or Linux environment where a browser can open:

   ```console
   python -m pip install -r requirements.txt
   ```

5. From the repository root, authorize the application and write the portable authorized-user token directly into the Compose data directory:

   ```console
   python -m vod_dashboard.youtube_oauth --client-secret ./data/client_secret.json --token ./data/youtube-token.json
   ```

   The helper uses Google's supported Desktop-app localhost callback flow on the machine running the command. It does not use the deprecated out-of-band copy/paste flow. The browser may select the Google account and request the configured YouTube scopes; token contents are never printed.

6. Start or restart Compose and open the YouTube page:

   ```console
   docker compose up -d --build
   ```

The token format produced by the helper is the same authorized-user JSON consumed by the dashboard. The container can refresh it in `/data`, and the entrypoint applies mode `0600` where the host filesystem supports Unix permissions. Match `PUID`/`PGID` to the host owner of `./data` if the container cannot update the token.

Never commit either credential file. Both `client_secret.json` and `youtube-token.json` are excluded by `.gitignore` and `.dockerignore`. To reauthorize, stop uploads, delete `./data/youtube-token.json`, optionally revoke the application's access in the Google Account security settings, and rerun the bootstrap command. Replacing the Desktop client also requires replacing `client_secret.json` and bootstrapping a new token.

Native application launches keep the existing **Connect YouTube** behavior: unless `VOD_DASHBOARD_YOUTUBE_OAUTH_MODE=external` is explicitly set, the application opens Google's Desktop-app localhost authorization flow and persists the token in its configured dashboard-data directory.

Client-secret discovery is deterministic. The dashboard considers the explicit persisted setting and its designated dashboard-data `client_secret.json`; it no longer searches the application directory or its parent. Existing explicitly saved paths continue to work. Users who relied only on implicit source-tree discovery must move the file to the dashboard-data directory or save its exact path in Settings.

Fresh data directories do not search the host filesystem for older settings. To import one known legacy file, place it inside a mounted directory and explicitly set `VOD_DASHBOARD_LEGACY_SETTINGS_PATH` to its container path, such as `/data/legacy-settings.json`. The legacy file is only read when `/data/dashboard-settings.json` does not yet exist; save Settings once to write the migrated configuration to the current file.

Do not add these files to the image or Compose configuration. They are ignored by Git.

Dashboard logging defaults to `/data/dashboard.log`. It rotates to one `/data/dashboard.log.1` backup at approximately 5 MiB, keeping application-file logging bounded. Container stdout/stderr remains available through `docker compose logs`.

Automatic recording and durable Queue history run only in the production Gunicorn deployment. The configuration intentionally uses exactly one worker; startup aborts if a different worker count is configured so multiple managers cannot write the same history. The worker restores `/data/jobs.json`, reconciles stale process-owned states, prepares Auto Recorder restart state, and only then starts one monitor. No download, upload, or recording is automatically resumed.

Completed and interrupted Queue history survives container recreation through the existing `./data:/data` bind mount. A missing `jobs.json` is a healthy first start and does not create an empty file. Malformed history is preserved for operator recovery, the dashboard remains available with degraded persistence status, and new external job side effects fail closed until durable storage works again. Running uploads interrupted by restart are marked as having an unknown remote outcome and require review before Retry.

On graceful worker exit the monitor is stopped first, registered application-owned yt-dlp/ffmpeg process groups and live recordings use their bounded shutdown paths, and dirty job history receives a final persistence checkpoint. In-process YouTube uploads are not killed asynchronously; if one cannot finish before process exit, startup reconciliation remains authoritative. Gunicorn allows 60 seconds for graceful worker shutdown, while Compose allows the container 75 seconds. Native `python app.py` remains development-oriented: it starts neither the monitor nor production JobStore persistence.

## HTTPS reverse proxy

The base deployment has no proxy labels, external network, certificate resolver, or forwarded-header trust. `compose.traefik.example.yml` is an optional override:

```console
docker compose -f compose.yml -f compose.traefik.example.yml up -d
```

Before using it, set `VOD_DASHBOARD_HOSTNAME`, `VOD_DASHBOARD_CERT_RESOLVER`, and `VOD_DASHBOARD_TRAEFIK_NETWORK`. Also set:

```dotenv
VOD_DASHBOARD_SESSION_COOKIE_SECURE=1
VOD_DASHBOARD_ALLOWED_ORIGINS=https://dashboard.example.invalid
VOD_DASHBOARD_TRUSTED_HOSTS=dashboard.example.invalid
```

The application does not apply `ProxyFix` and does not trust `X-Forwarded-*` headers. The proxy must preserve the original `Host` header. If using a proxy other than Traefik, connect it to container port `8787` on a private Docker network; the host-published port can remain restricted to loopback.

## Optional operational helpers

No cleanup job or timer is installed automatically.

`cleanup-vods.py` is a dry run unless `--delete` is supplied. Select its media library with `VOD_DASHBOARD_MEDIA_ROOT` or `--media-root`. Inside the application container, `/downloads` is already the configured default:

```console
docker compose exec dashboard python /app/cleanup-vods.py
docker compose exec dashboard python /app/cleanup-vods.py --delete
```

`disk-guard.sh` is an optional Linux-host helper. Configure `VOD_DASHBOARD_MEDIA_ROOT`, `VOD_DASHBOARD_MIN_FREE_GB`, and optionally `VOD_DASHBOARD_CONTAINER` if it should stop a named container when space is low. With no container name it only logs the low-space condition and exits unsuccessfully. Administrators may schedule these tools themselves after reviewing their destructive behavior.

The helper is tracked as a portable shell source file rather than relying on a Windows checkout to preserve executable mode. Invoke it explicitly with Bash, for example:

```console
VOD_DASHBOARD_MEDIA_ROOT=/path/to/downloads VOD_DASHBOARD_MIN_FREE_GB=40 bash disk-guard.sh
```

Native Windows and Linux execution is unchanged: Docker-only `/data` and `/downloads` paths are supplied by Compose and are not application defaults.
