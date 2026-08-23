import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def read_text(name: str) -> str:
    return (REPOSITORY_ROOT / name).read_text(encoding="utf-8")


class DeploymentConfigTests(unittest.TestCase):
    def test_docker_copy_layout_contains_package_and_imports_application(self):
        dockerfile = read_text("Dockerfile")
        with tempfile.TemporaryDirectory() as temp_dir:
            layout = Path(temp_dir)
            for raw_line in dockerfile.splitlines():
                line = raw_line.strip()
                if not line.startswith("COPY "):
                    continue
                parts = line.split()
                self.assertEqual(len(parts), 3, f"Unsupported COPY instruction: {line}")
                source = REPOSITORY_ROOT / parts[1]
                destination_text = parts[2]
                if destination_text.startswith("/"):
                    continue
                destination = (
                    layout
                    if destination_text == "."
                    else layout / destination_text.removeprefix("./")
                )
                if source.is_dir():
                    shutil.copytree(source, destination, dirs_exist_ok=True)
                else:
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination / source.name)

            package_init = layout / "vod_dashboard" / "__init__.py"
            self.assertTrue(package_init.is_file())

            env = os.environ.copy()
            env.update(
                VOD_DASHBOARD_AUTH_DISABLED="1",
                VOD_DASHBOARD_MEDIA_ROOT=str(layout / "media"),
                VOD_DASHBOARD_DIR=str(layout / "data"),
                VOD_DASHBOARD_SETTINGS=str(layout / "data" / "settings.json"),
                VOD_DASHBOARD_LEGACY_SETTINGS_PATH="",
            )
            import_paths = []
            for entry in sys.path:
                if not entry:
                    continue
                try:
                    if Path(entry).resolve() == REPOSITORY_ROOT.resolve():
                        continue
                except OSError:
                    pass
                import_paths.append(entry)
            env["PYTHONPATH"] = os.pathsep.join(import_paths)
            process = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import app, vod_dashboard; "
                        "root = Path.cwd().resolve(); "
                        "assert Path(app.__file__).resolve().parent == root; "
                        "assert Path(vod_dashboard.__file__).resolve().parent.parent == root"
                    ),
                ],
                cwd=layout,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                process.returncode,
                0,
                f"Docker layout import failed:\n{process.stdout}\n{process.stderr}",
            )

    def test_base_compose_is_generic_and_loopback_only(self):
        compose = read_text("compose.yml")

        self.assertIn("127.0.0.1:${VOD_DASHBOARD_PORT:-8787}:8787", compose)
        self.assertIn("./data:/data", compose)
        self.assertIn("./downloads:/downloads", compose)
        self.assertIn("VOD_DASHBOARD_MEDIA_ROOT: ${VOD_DASHBOARD_MEDIA_ROOT:-/downloads}", compose)
        self.assertIn(
            "VOD_DASHBOARD_LEGACY_SETTINGS_PATH: ${VOD_DASHBOARD_LEGACY_SETTINGS_PATH:-}",
            compose,
        )
        self.assertIn(
            "VOD_DASHBOARD_YOUTUBE_OAUTH_MODE: ${VOD_DASHBOARD_YOUTUBE_OAUTH_MODE:-external}",
            compose,
        )
        self.assertNotIn("traefik", compose.lower())
        self.assertNotIn("external: true", compose)
        self.assertNotIn("VOD_DASHBOARD_AUTH_DISABLED", compose)

    def test_fresh_ui_does_not_reintroduce_firefox_cookie_default(self):
        template = read_text("templates/index.html")
        javascript = read_text("static/app.js")

        self.assertIn('<input id="cookieBrowser">', template)
        self.assertNotIn('id="cookieBrowser" value="firefox"', template)
        self.assertIn("cookie_browser: val('cookieBrowser')", javascript)
        self.assertNotIn("cookie_browser: val('cookieBrowser', 'firefox')", javascript)

    def test_environment_example_documents_required_deployment_variables(self):
        example = read_text(".env.example")
        required = {
            "VOD_DASHBOARD_USERNAME",
            "VOD_DASHBOARD_PASSWORD_HASH",
            "VOD_DASHBOARD_SECRET_KEY",
            "VOD_DASHBOARD_MEDIA_ROOT",
            "VOD_DASHBOARD_LEGACY_SETTINGS_PATH",
            "VOD_DASHBOARD_YOUTUBE_OAUTH_MODE",
            "VOD_DASHBOARD_SESSION_COOKIE_SECURE",
            "VOD_DASHBOARD_ALLOWED_ORIGINS",
            "VOD_DASHBOARD_TRUSTED_HOSTS",
            "TZ",
        }

        for name in required:
            self.assertIn(f"{name}=", example)
        self.assertNotIn("VOD_DASHBOARD_AUTH_DISABLED", example)

    def test_deployment_documents_external_youtube_oauth_bootstrap(self):
        deployment = read_text("DEPLOYMENT.md")
        gitignore = read_text(".gitignore")

        self.assertIn("VOD_DASHBOARD_YOUTUBE_OAUTH_MODE=external", deployment)
        self.assertIn("python -m vod_dashboard.youtube_oauth", deployment)
        self.assertIn("YouTube Data API v3", deployment)
        self.assertIn("Desktop app", deployment)
        self.assertIn("./data/client_secret.json", deployment)
        self.assertIn("./data/youtube-token.json", deployment)
        self.assertIn("client_secret.json", gitignore)
        self.assertIn("youtube-token.json", gitignore)

    def test_release_local_and_private_artifacts_are_ignored(self):
        gitignore = read_text(".gitignore")
        required = {
            ".env.*",
            "!.env.example",
            "*.log.*",
            ".ruff_cache/",
            ".mypy_cache/",
            ".tox/",
            "coverage.xml",
            "venv/",
            "env/",
            "/cookies.txt",
            "/*-cookies.txt",
            "/*.cookies.txt",
        }

        for pattern in required:
            self.assertIn(pattern, gitignore)

    def test_docker_build_context_excludes_private_and_local_artifacts(self):
        dockerignore = read_text(".dockerignore")
        required = {
            ".env",
            ".env.*",
            ".venv/",
            "venv/",
            "env/",
            ".ruff_cache/",
            ".mypy_cache/",
            ".tox/",
            ".pytest_cache/",
            "data/",
            "downloads/",
            "twitch-cookies.txt",
            "/cookies.txt",
            "/*-cookies.txt",
            "/*.cookies.txt",
            "client_secret.json",
            "youtube-token.json",
            "*.log",
            "*.log.*",
            "*.uploaded.json",
            "*.info.json",
            "*.youtube.json",
            "*.youtube-beschreibung.txt",
            "*.bak",
            "*.backup*",
            "app.py.backup-*",
            ".idea/",
            ".vscode/",
            ".coverage",
            "coverage.xml",
            "htmlcov/",
            "dist/",
            "build/",
            "*.egg-info/",
        }

        for pattern in required:
            self.assertIn(pattern, dockerignore)

        dockerfile = read_text("Dockerfile")
        for source in (
            "requirements.txt",
            "app.py",
            "gunicorn.conf.py",
            "vod_dashboard",
            "templates",
            "static",
            "cleanup-vods.py",
            "docker-entrypoint.sh",
        ):
            self.assertIn(f"COPY {source} ", dockerfile)

    def test_auto_recorder_gunicorn_and_compose_shutdown_budgets(self):
        dockerfile = read_text("Dockerfile")
        gunicorn = read_text("gunicorn.conf.py")
        compose = read_text("compose.yml")
        deployment = read_text("DEPLOYMENT.md")

        self.assertIn(
            'CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]',
            dockerfile,
        )
        for setting in (
            'bind = "0.0.0.0:8787"',
            "workers = 1",
            "threads = 4",
            "timeout = 300",
            "graceful_timeout = 60",
            "def post_worker_init(worker)",
            "def worker_exit(server, worker)",
        ):
            self.assertIn(setting, gunicorn)
        self.assertIn("stop_grace_period: 75s", compose)
        self.assertIn("exactly one worker", deployment)
        self.assertIn("Native `python app.py`", deployment)

    def test_container_bootstrap_drops_root_and_protects_private_files(self):
        dockerfile = read_text("Dockerfile")
        entrypoint = read_text("docker-entrypoint.sh")
        attributes = read_text(".gitattributes")
        dockerignore = read_text(".dockerignore")

        self.assertIn('ENTRYPOINT ["docker-entrypoint.sh"]', dockerfile)
        self.assertIn('exec gosu "$puid:$pgid" "$@"', entrypoint)
        self.assertIn("chmod 0600", entrypoint)
        self.assertIn("chmod 0700 /data", entrypoint)
        self.assertIn("youtube-token.json", entrypoint)
        self.assertIn("twitch-cookies.txt", entrypoint)
        self.assertIn("*.sh text eol=lf", attributes)
        self.assertIn(".env", dockerignore)
        self.assertIn("data/", dockerignore)
        self.assertIn("downloads/", dockerignore)
        self.assertIn("client_secret.json", dockerignore)

    def test_application_file_logging_is_configurable_and_bounded(self):
        application = read_text("app.py")
        runtime = read_text("vod_dashboard/runtime.py")

        self.assertIn("RUNTIME_PATHS.log_file", application)
        self.assertIn('env.get("VOD_DASHBOARD_LOG_FILE")', runtime)
        self.assertIn("LOG_MAX_BYTES = 5 * 1024 * 1024", runtime)
        self.assertIn('backup_file = Path(f"{log_file}.1")', runtime)

    def test_operational_helpers_use_configurable_media_roots(self):
        cleanup = read_text("cleanup-vods.py")
        disk_guard = read_text("disk-guard.sh")
        deployment = read_text("DEPLOYMENT.md")

        self.assertIn("VOD_DASHBOARD_MEDIA_ROOT", cleanup)
        self.assertIn("--media-root", cleanup)
        self.assertIn("VOD_DASHBOARD_MEDIA_ROOT", disk_guard)
        self.assertIn("VOD_DASHBOARD_MIN_FREE_GB", disk_guard)
        self.assertIn("VOD_DASHBOARD_CONTAINER", disk_guard)
        self.assertIn("bash disk-guard.sh", deployment)

    def test_runtime_and_development_dependencies_are_explicitly_bounded(self):
        runtime = read_text("requirements.txt")
        development = read_text("requirements-dev.txt")
        dockerfile = read_text("Dockerfile")

        for requirement in (
            "Flask>=3.0,<4",
            "Werkzeug>=3.0,<4",
            "yt-dlp>=2026.1.1,<2027",
            "google-api-python-client>=2.0,<3",
            "google-auth-oauthlib>=1.0,<2",
            "google-auth>=2.0,<3",
        ):
            self.assertIn(requirement, runtime)
        self.assertNotIn("pytest", runtime.lower())
        self.assertIn("-r requirements.txt", development)
        self.assertIn("pytest>=9.0,<10", development)
        self.assertIn('"gunicorn>=23,<24"', dockerfile)

    def test_production_image_installs_ffmpeg_once(self):
        dockerfile = read_text("Dockerfile")

        self.assertIn(
            "apt-get install -y --no-install-recommends ffmpeg",
            dockerfile,
        )
        self.assertEqual(dockerfile.split().count("ffmpeg"), 1)

    def test_linux_ci_runs_supported_python_matrix_and_offline_checks(self):
        workflow = read_text(".github/workflows/ci.yml")

        for fragment in (
            "push:",
            "pull_request:",
            "- main",
            "runs-on: ubuntu-latest",
            '- "3.12"',
            '- "3.14"',
            "python -m pip install -r requirements-dev.txt",
            "python -m pytest -q",
            "python -m compileall -q app.py cleanup-vods.py vod_dashboard tests",
            "node --check static/app.js",
        ):
            self.assertIn(fragment, workflow)
        self.assertNotIn("continue-on-error", workflow)

    def test_public_text_files_have_no_private_deployment_identifiers(self):
        banned = (
            "Fra" + "nz",
            "/home/" + "franz",
            "/srv/" + "vods",
            "se" + "vai.de",
            "do" + "de",
        )
        text_suffixes = {".css", ".html", ".js", ".json", ".md", ".py", ".sh", ".txt", ".yml"}
        ignored_directories = {
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".tox",
            ".venv",
            "__pycache__",
            "build",
            "data",
            "dist",
            "downloads",
            "env",
            "htmlcov",
            "node_modules",
            "venv",
        }

        for path in REPOSITORY_ROOT.rglob("*"):
            if not path.is_file() or ignored_directories.intersection(path.parts):
                continue
            if path.name not in {
                "Dockerfile",
                "LICENSE",
                ".gitignore",
                ".env.example",
            } and path.suffix not in text_suffixes:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            for identifier in banned:
                if path.name == "LICENSE" and identifier == banned[0]:
                    continue
                self.assertNotIn(identifier, content, f"private identifier remains in {path}")
