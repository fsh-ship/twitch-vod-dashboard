import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from vod_dashboard import youtube
from vod_dashboard import youtube_oauth


class YouTubeOAuthBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.secret = self.base / "credentials" / "client_secret.json"
        self.token = self.base / "data" / "youtube-token.json"
        self.secret.parent.mkdir()
        self.secret.write_text(
            '{"installed":{"client_secret":"private-client-value"}}',
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bootstrap_uses_desktop_local_server_and_persists_token(self):
        credentials = mock.Mock()
        credentials.to_json.return_value = '{"refresh_token":"private-token"}'
        flow = mock.Mock()
        flow.run_local_server.return_value = credentials
        flow_class = mock.Mock()
        flow_class.from_client_secrets_file.return_value = flow

        result = youtube.bootstrap_youtube_oauth(
            self.secret,
            self.token,
            libraries_available=True,
            flow_class=flow_class,
        )

        self.assertIs(result, credentials)
        flow_class.from_client_secrets_file.assert_called_once_with(
            str(self.secret), youtube.YOUTUBE_SCOPES
        )
        flow.run_local_server.assert_called_once_with(port=0, prompt="consent")
        self.assertEqual(
            self.token.read_text(encoding="utf-8"),
            '{"refresh_token":"private-token"}',
        )
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)

    def test_bootstrap_requires_existing_client_secret(self):
        missing = self.base / "missing.json"
        flow_class = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "client_secret.json not found"):
            youtube.bootstrap_youtube_oauth(
                missing,
                self.token,
                libraries_available=True,
                flow_class=flow_class,
            )
        flow_class.from_client_secrets_file.assert_not_called()
        self.assertFalse(self.token.exists())

    def test_cli_resolves_explicit_paths_and_reports_token_location(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        bootstrap = mock.Mock()

        result = youtube_oauth.main(
            [
                "--client-secret",
                str(self.secret),
                "--token",
                str(self.token),
            ],
            bootstrap=bootstrap,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(result, 0)
        bootstrap.assert_called_once_with(
            self.secret.resolve(), self.token.resolve()
        )
        self.assertIn(str(self.token.resolve()), stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_missing_secret_never_starts_oauth(self):
        stderr = io.StringIO()
        bootstrap = mock.Mock()
        result = youtube_oauth.main(
            [
                "--client-secret",
                str(self.base / "missing.json"),
                "--token",
                str(self.token),
            ],
            bootstrap=bootstrap,
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(result, 2)
        self.assertIn("Client secret file not found", stderr.getvalue())
        bootstrap.assert_not_called()

    def test_cli_failure_does_not_print_secret_or_token_values(self):
        private_value = "private-refresh-token-value"
        stderr = io.StringIO()
        result = youtube_oauth.main(
            [
                "--client-secret",
                str(self.secret),
                "--token",
                str(self.token),
            ],
            bootstrap=mock.Mock(
                side_effect=RuntimeError(private_value)
            ),
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(result, 1)
        self.assertNotIn(private_value, stderr.getvalue())
        self.assertIn("RuntimeError", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
