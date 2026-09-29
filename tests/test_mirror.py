import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flatpak_release_mirror.mirror import Mirror, Source, _same_repo_url


class StateTests(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as directory:
            mirror = Mirror(Path(directory))
            source = Source(
                url="https://github.com/Wyze3306/BedrockOnLinux",
                owner="Wyze3306",
                repo="BedrockOnLinux",
                tag="v2.2.7",
                asset="BedrockOnLinux-2.2.7-x86_64.flatpak",
                digest="sha256:abc",
                ref="app/io.github.wyze3306.BedrockOnLinux/x86_64/master",
            )
            mirror._save({source.url: source})
            loaded = mirror._load()
            self.assertEqual(loaded[source.url].ref, source.ref)
            self.assertEqual(loaded[source.url].tag, "v2.2.7")


class RepoUrlTests(unittest.TestCase):
    def test_file_uri_matches_path(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve()
            self.assertTrue(_same_repo_url(path.as_uri(), str(path)))
            self.assertFalse(_same_repo_url(path.as_uri() + "-other", str(path)))


if __name__ == "__main__":
    unittest.main()
