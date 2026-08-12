import re
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class PublicDocumentationTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (REPOSITORY_ROOT / name).read_text(encoding="utf-8-sig")

    def test_readme_relative_repository_links_resolve(self):
        readme = self.read("README.md")
        links = re.findall(r"\[[^]]+\]\(([^)]+)\)", readme)
        repository_links = [
            link.split("#", 1)[0]
            for link in links
            if not re.match(r"^[a-z]+://", link)
        ]
        self.assertTrue(repository_links)
        for link in repository_links:
            with self.subTest(link=link):
                self.assertTrue((REPOSITORY_ROOT / link).exists())

    def test_documented_files_modules_and_commands_exist(self):
        readme = self.read("README.md")
        deployment = self.read("DEPLOYMENT.md")
        combined = readme + deployment

        for path in (
            "requirements.txt",
            "requirements-dev.txt",
            ".env.example",
            "compose.yml",
            ".github/workflows/ci.yml",
            "static/app.js",
            "vod_dashboard/youtube_oauth.py",
        ):
            self.assertTrue((REPOSITORY_ROOT / path).is_file(), path)
        self.assertIn("python -m pip install -r requirements-dev.txt", readme)
        self.assertIn("python -m pytest -q", readme)
        self.assertIn("node --check static/app.js", readme)
        self.assertIn("python -m vod_dashboard.youtube_oauth", combined)
        self.assertIn("docker compose up -d --build", combined)

    def test_readme_authentication_variables_match_environment_example(self):
        readme = self.read("README.md")
        example = self.read(".env.example")
        for variable in (
            "VOD_DASHBOARD_USERNAME",
            "VOD_DASHBOARD_PASSWORD_HASH",
            "VOD_DASHBOARD_SECRET_KEY",
            "VOD_DASHBOARD_MEDIA_ROOT",
            "VOD_DASHBOARD_SESSION_COOKIE_SECURE",
            "VOD_DASHBOARD_ALLOWED_ORIGINS",
            "VOD_DASHBOARD_TRUSTED_HOSTS",
        ):
            self.assertIn(variable, readme)
            self.assertIn(f"{variable}=", example)

    def test_license_is_mit_without_contact_details(self):
        license_text = self.read("LICENSE")

        self.assertTrue(license_text.startswith("MIT License\n"))
        holder = "Fra" + "nz Hernschier"
        self.assertIn(f"Copyright (c) 2026 {holder}", license_text)
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertNotIn("@", license_text)

    def test_public_docs_do_not_contain_private_infrastructure_or_secret_examples(self):
        public_docs = "\n".join(
            self.read(name)
            for name in ("README.md", "DEPLOYMENT.md", ".env.example")
        )
        for banned in (
            "C:\\Users\\",
            "/home/" + "franz",
            "se" + "vai.de",
            "vod.lab.",
            "do" + "de",
            "315550821+",
        ):
            self.assertNotIn(banned, public_docs)
        self.assertNotRegex(public_docs, r"(?i)(api[_-]?key|access[_-]?token)\s*[=:]\s*[^\s<]+")


if __name__ == "__main__":
    unittest.main()
