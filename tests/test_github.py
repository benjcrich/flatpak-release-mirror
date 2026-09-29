import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flatpak_release_mirror.github import (
    Asset,
    GitHubError,
    choose_flatpak_asset,
    parse_github_repo,
    verify_digest,
)


class ParseTests(unittest.TestCase):
    def test_repository_url(self):
        self.assertEqual(
            parse_github_repo("https://github.com/Wyze3306/BedrockOnLinux"),
            ("Wyze3306", "BedrockOnLinux"),
        )

    def test_releases_url_and_git_suffix(self):
        self.assertEqual(
            parse_github_repo("https://github.com/Wyze3306/BedrockOnLinux.git/releases"),
            ("Wyze3306", "BedrockOnLinux"),
        )

    def test_ssh_url(self):
        self.assertEqual(
            parse_github_repo("git@github.com:Wyze3306/BedrockOnLinux.git"),
            ("Wyze3306", "BedrockOnLinux"),
        )

    def test_rejects_other_hosts(self):
        with self.assertRaises(GitHubError):
            parse_github_repo("https://gitlab.com/owner/repo")


class AssetTests(unittest.TestCase):
    def test_picks_host_arch(self):
        assets = [
            Asset("App-aarch64.flatpak", "https://example/a", 1, None),
            Asset("App-x86_64.flatpak", "https://example/b", 1, None),
            Asset("App-x86_64.AppImage", "https://example/c", 1, None),
        ]
        chosen = choose_flatpak_asset(assets, "x86_64")
        self.assertEqual(chosen.name, "App-x86_64.flatpak")

    def test_single_bundle_without_arch(self):
        assets = [Asset("App.flatpak", "https://example/a", 1, None)]
        self.assertEqual(choose_flatpak_asset(assets, "x86_64").name, "App.flatpak")

    def test_missing_bundle(self):
        with self.assertRaises(GitHubError):
            choose_flatpak_asset(
                [Asset("App.AppImage", "https://example/a", 1, None)],
                "x86_64",
            )


class DigestTests(unittest.TestCase):
    def test_sha256(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bundle"
            path.write_bytes(b"flatpak")
            digest = "sha256:" + hashlib.sha256(b"flatpak").hexdigest()
            verify_digest(path, digest)

    def test_mismatch(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bundle"
            path.write_bytes(b"flatpak")
            with self.assertRaises(GitHubError):
                verify_digest(path, "sha256:" + "ab" * 32)


if __name__ == "__main__":
    unittest.main()
