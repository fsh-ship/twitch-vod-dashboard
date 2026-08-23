# Twitch VOD Dashboard

Twitch VOD Dashboard is a self-hosted, single-user web application for finding completed Twitch VODs, downloading them with yt-dlp, preparing local media for YouTube, and optionally uploading it through the YouTube Data API.

The project is designed for a personal archive workflow. It includes secure-by-default administrator authentication and supports native Windows/Linux use as well as a persistent Docker Compose deployment. It is not a multi-user or enterprise media-management system.

## Features

- Manage an ordered list of Twitch streamers.
- Search completed Twitch VODs with date filtering, source fallback, deduplication, and optional date enrichment.
- Exclude live/upcoming entries and non-canonical VOD URLs.
- Validate individual Twitch VOD URLs before download.
- Queue yt-dlp downloads and inspect bounded, browser-visible job logs.
- Track downloaded VOD IDs through the yt-dlp archive file.
- List local VODs below an administrator-controlled media root.
- Generate configurable YouTube titles, descriptions, filenames, and sidecar metadata.
- Upload to YouTube with configurable privacy, chunk size, and optional playlist insertion.
- Track uploaded files and optionally move completed VOD bundles into `_hochgeladen`.
- Protect the dashboard and API with single-user session authentication, CSRF tokens, Origin checks, trusted-host checks, and login throttling.
- Run with native Python or the included Gunicorn-based Docker image.

## Screenshots

No public-safe screenshots are currently included. Screenshots showing an empty or synthetic-data dashboard should be added before the public release; do not use private streamer, path, token, or account data.

## Architecture

- `app.py` contains the Flask application, route adapters, and native entry point.
- `vod_dashboard/security.py` implements authentication configuration, request policy helpers, and process-local login throttling.
- `vod_dashboard/settings.py` manages settings normalization and runtime-data persistence.
- `vod_dashboard/media.py` enforces the media-root boundary and implements contained file operations.
- `vod_dashboard/twitch.py` contains Twitch parsing, yt-dlp integration, and VOD search orchestration.
- `vod_dashboard/vod_search.py` prepares search requests and download selections.
- `vod_dashboard/youtube.py` handles local metadata, OAuth credentials, uploads, playlists, and post-upload actions.
- `vod_dashboard/youtube_oauth.py` provides the external OAuth bootstrap used by Docker deployments.
- `vod_dashboard/jobs.py` provides the in-memory job registry and download/upload workers.
- `vod_dashboard/local_vods.py` constructs the local VOD listing and status payloads.
- `vod_dashboard/dashboard_state.py` constructs read-only dashboard diagnostics.

Flask routes remain centralized in `app.py`; the extracted modules do not use Flask request or session globals.

## Requirements

Python 3.12 is the recommended native version and is used for local release verification. Linux CI is configured to run the full suite on Python 3.12 and 3.14; the Dockerfile targets Python 3.14. Versions below 3.12 are not part of the project's tested support targets.

Native operation requires:

- Python and `pip`
- the packages in `requirements.txt`, including Flask, yt-dlp, and the Google API clients
- `ffmpeg` available on `PATH` for media merging/remuxing performed by yt-dlp
- a browser for the initial native Google OAuth authorization

Docker operation requires Docker Engine with the Compose plugin. The image installs ffmpeg and serves the application with a Docker-only Gunicorn dependency. Node.js is not required to run the application; it is used only for a JavaScript syntax check during development and CI.

## Docker quick start

Docker is the recommended server deployment.

1. Clone the repository and enter it.
2. Copy `.env.example` to `.env`.
3. Replace the username, Werkzeug password hash, and secret-key placeholders.
4. Start the dashboard:

   ```console
   docker compose up -d --build
   ```

5. Open <http://127.0.0.1:8787> and sign in with the configured username and password.

The default port is published only on `127.0.0.1`. Compose mounts:

| Host | Container | Purpose |
| --- | --- | --- |
| `./data` | `/data` | Settings, streamer/archive files, OAuth files, and logs |
| `./downloads` | `/downloads` | Downloaded media and media sidecars |

Back up `./data` regularly. Back up `./downloads` separately if the media itself cannot be recreated. See [DEPLOYMENT.md](DEPLOYMENT.md) for permissions, reverse proxies, OAuth bootstrap, migration, and optional operational tools.

## Authentication configuration

Authentication is enabled by default. The application refuses to start without:

- `VOD_DASHBOARD_USERNAME`
- `VOD_DASHBOARD_PASSWORD_HASH`, containing a Werkzeug-compatible hash rather than plaintext
- `VOD_DASHBOARD_SECRET_KEY`, containing at least 32 characters

Generate a password hash without saving the plaintext password:

```console
python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('Password: ')))"
```

Generate an independent session secret:

```console
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

For HTTPS deployments, set `VOD_DASHBOARD_SESSION_COOKIE_SECURE=1`. Configure `VOD_DASHBOARD_ALLOWED_ORIGINS` and `VOD_DASHBOARD_TRUSTED_HOSTS` for the externally visible origin and host. Comma-separated values are supported.

`VOD_DASHBOARD_AUTH_DISABLED=1` is an explicit development-only escape hatch. Never expose a dashboard running in that mode.

## Native installation

Native startup binds to `127.0.0.1:8787` and uses Flask's development server. It is suitable for local Windows/Linux use; use Docker/Gunicorn for a persistent server.

### Windows PowerShell

```powershell
git clone <repository-url>
Set-Location twitch-vod-dashboard
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
New-Item -ItemType Directory -Force data, downloads | Out-Null

$env:VOD_DASHBOARD_USERNAME = "admin"
$env:VOD_DASHBOARD_PASSWORD_HASH = (python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('Password: ')))")
$env:VOD_DASHBOARD_SECRET_KEY = (python -c "import secrets; print(secrets.token_urlsafe(48))")
$env:VOD_DASHBOARD_DIR = (Resolve-Path data).Path
$env:VOD_DASHBOARD_MEDIA_ROOT = (Resolve-Path downloads).Path

python app.py
```

### Linux shell

```bash
git clone <repository-url>
cd twitch-vod-dashboard
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p data downloads

export VOD_DASHBOARD_USERNAME=admin
export VOD_DASHBOARD_PASSWORD_HASH="$(python -c 'from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass("Password: ")))')"
export VOD_DASHBOARD_SECRET_KEY="$(python -c "import secrets; print(secrets.token_urlsafe(48))")"
export VOD_DASHBOARD_DIR="$PWD/data"
export VOD_DASHBOARD_MEDIA_ROOT="$PWD/downloads"

python app.py
```

The application opens the local URL automatically unless `VOD_DASHBOARD_NO_BROWSER=1` is set. The example runtime directories are ignored by Git.

## Twitch access and cookies

Fresh installations do not select browser-cookie extraction. Public Twitch VOD discovery may work without cookies, depending on the content and Twitch access requirements.

When authentication is required, configure one of these deliberately:

- Set the **Browser Cookies** setting to a browser supported by yt-dlp on the same machine.
- Set a cookie-file path in Settings.
- For Docker, mount a cookie file under `./data` and set `VOD_DASHBOARD_TWITCH_COOKIE_FILE=/data/twitch-cookies.txt`.

A configured cookie file takes precedence over browser-cookie extraction. Cookie files are credentials: restrict access and never commit them. The Docker image does not install Firefox and does not implicitly depend on a browser profile.

## YouTube OAuth

YouTube integration requires a Google Cloud project with **YouTube Data API v3** enabled, a configured OAuth consent screen, and a **Desktop app** OAuth client. If the consent screen remains in testing mode, add the intended Google account as a test user.

Native launches use Google's localhost Desktop-app authorization flow when **Connect YouTube** is selected. The resulting token is stored in the configured dashboard-data directory and refreshed there.

Docker deliberately disables interactive OAuth inside the container. Bootstrap the same portable token on the host:

1. Save the Desktop client JSON as `./data/client_secret.json`.
2. In a native Python environment with the requirements installed, run:

   ```console
   python -m vod_dashboard.youtube_oauth --client-secret ./data/client_secret.json --token ./data/youtube-token.json
   ```

3. Start or restart Compose. The container reads and refreshes `/data/youtube-token.json` through the persistent mount.

Never commit `client_secret.json` or `youtube-token.json`. To reauthorize, stop active uploads, delete the token, optionally revoke the application's access in the Google Account security settings, and rerun the bootstrap. See [the deployment guide](DEPLOYMENT.md#youtube-oauth-for-docker) for details.

## Normal workflow

1. Open **Streamers**, enter one Twitch login per line, and save the list.
2. Open **VOD Search**, select a date range, and search for completed VODs.
3. Review the results, select VODs, and choose **Download Selected**. The Queue page shows download progress and bounded logs.
4. Use **Prepare for YouTube** to inspect downloaded media, generate metadata sidecars, and optionally rename files.
5. Upload from the dashboard after connecting YouTube, or open YouTube Studio and upload manually.
6. Mark manual uploads as uploaded. Successful dashboard uploads update upload history; configured workflows can move the VOD and its sidecars to `_hochgeladen`.
7. Search results mark VOD IDs already present in the archive, and yt-dlp uses the same archive to skip repeat downloads.

## Runtime data

The dashboard-data directory contains small, important state:

- `dashboard-settings.json`: persisted application settings and YouTube upload history
- `streamer.txt`: configured streamer names
- `archive.txt`: yt-dlp download archive entries
- `client_secret.json`: Google OAuth client credentials, when supplied
- `youtube-token.json`: Google authorized-user token, when connected
- `jobs.json`: bounded, allowlisted Queue history used by production Gunicorn restart recovery
- `dashboard.log` and `dashboard.log.1`: bounded application logs
- an optional Twitch cookie file

The media root contains VOD files, yt-dlp `.info.json` metadata, upload markers, and YouTube preparation sidecars. All dashboard local-file operations are restricted to the resolved administrator-controlled `VOD_DASHBOARD_MEDIA_ROOT`; resolved symlinks cannot escape it.

## Security notes

- Keep authentication enabled and use a unique password and secret key.
- The default Compose binding is loopback-only. Deliberately configure a reverse proxy before remote access.
- Use HTTPS and secure session cookies for any non-local deployment.
- Configure trusted hosts and allowed origins to match the public URL.
- The application does not trust `X-Forwarded-*` headers or enable `ProxyFix` automatically.
- OAuth tokens, client secrets, and Twitch cookies are sensitive credentials.
- Login throttling is process-local and resets when the process restarts. Compose currently runs one Gunicorn worker.
- A reverse proxy should provide transport-level policies such as HSTS and may add a suitable Content Security Policy after testing it with the UI.
- Review file permissions and back up runtime state before upgrades or cleanup operations.

## Platform limitations

- **Open Folder**, **Show in Folder**, and file-selection integration use Windows APIs. These actions are not currently implemented for native Linux or Docker browsers.
- Docker YouTube authorization requires a one-time host/native browser bootstrap.
- The native entry point is loopback-only and uses Flask's development server.
- Production Gunicorn persists Queue history, but active work is never automatically resumed after a restart. Native `python app.py` keeps development-only process-local job state.
- A running YouTube upload interrupted by restart has an uncertain remote outcome and must be reviewed before Retry.
- Docker host UID/GID mapping assumes a Linux-style host filesystem. Review `PUID` and `PGID` when using mounted existing data.

## Development and testing

Install the development requirements, which include `requirements.txt` plus pytest, then run the offline suite:

```console
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Runtime and test dependencies use bounded major-version ranges. This avoids silently accepting a future breaking major release while still allowing compatible fixes across Windows and Linux; the project does not maintain a platform-specific lockfile.

Compile the application and test sources:

```console
python -m compileall -q app.py cleanup-vods.py vod_dashboard tests
```

Optional JavaScript syntax check when Node.js is installed:

```console
node --check static/app.js
```

Routine tests mock Twitch, yt-dlp subprocesses, Google, YouTube, browser launching, and operating-system file opening. They do not require live services.

GitHub Actions runs the full suite and Python compilation on Ubuntu with Python 3.12 and 3.14, then checks JavaScript syntax with Node.js 22. Linux executes the symlink-containment tests normally. On Windows systems where the account cannot create symbolic links, the six symlink-specific cases are skipped; the rest of the filesystem-security suite still runs.

## Troubleshooting

- **Startup reports missing authentication variables:** configure all three required authentication variables; do not use auth-disabled mode as a deployment workaround.
- **Twitch search returns no VODs:** confirm the streamer has recent completed broadcasts, update yt-dlp, and add cookies only if Twitch requires them.
- **ffmpeg is missing:** install ffmpeg and make sure the executable is on `PATH`; Docker already includes it.
- **YouTube is not connected in Docker:** create `./data/youtube-token.json` with the external bootstrap command, then restart or refresh the dashboard.
- **YouTube OAuth is rejected:** verify that the API, consent screen, Desktop client type, and test-user access are configured in Google Cloud.
- **A media path is rejected:** place the file below `VOD_DASHBOARD_MEDIA_ROOT`; absolute paths, traversal, and symlink escapes outside that root are rejected.
- **Docker cannot update `/data`:** set `PUID` and `PGID` to the host owner and review mount permissions.
- **Folder-opening actions fail on Linux/Docker:** use the host file manager directly; these integrations are currently Windows-specific.

## License

Twitch VOD Dashboard is available under the [MIT License](LICENSE).
