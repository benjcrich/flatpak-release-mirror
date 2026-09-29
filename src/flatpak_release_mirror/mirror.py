"""Local OSTree mirror of GitHub .flatpak release bundles."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from flatpak_release_mirror.github import (
    GitHubError,
    canonical_url,
    download,
    fetch_latest_release,
    parse_github_repo,
    verify_digest,
)

REMOTE_NAME = "release-mirror"
REMOTE_TITLE = "GitHub release mirror"


class MirrorError(Exception):
    """The local mirror could not be updated."""


class SystemPermissionError(MirrorError):
    """A system Flatpak change needs an administrator password."""

    def __init__(self, command: list[str]):
        self.command = command
        super().__init__(shlex.join(["sudo", *command]))


@dataclass
class Source:
    url: str
    owner: str
    repo: str
    tag: str | None = None
    asset: str | None = None
    digest: str | None = None
    ref: str | None = None

    def to_json(self) -> dict:
        return {
            "url": self.url,
            "owner": self.owner,
            "repo": self.repo,
            "tag": self.tag,
            "asset": self.asset,
            "digest": self.digest,
            "ref": self.ref,
        }

    @classmethod
    def from_json(cls, payload: dict) -> Source:
        return cls(
            url=payload["url"],
            owner=payload["owner"],
            repo=payload["repo"],
            tag=payload.get("tag"),
            asset=payload.get("asset"),
            digest=payload.get("digest"),
            ref=payload.get("ref"),
        )


def data_dir() -> Path:
    override = os.environ.get("FLATPAK_RELEASE_MIRROR_HOME")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "flatpak-release-mirror"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user"


class Mirror:
    def __init__(self, root: Path | None = None):
        self.root = root or data_dir()
        self.repo = self.root / "repo"
        self.state_path = self.root / "state.json"
        self.cache = self.root / "cache"

    def add(self, url: str, installation: str | None, install_app: bool) -> Source:
        with self._lock():
            source = self._source_for_url(url)
            release_ref = self._import_latest(source)
            sources = self._load()
            sources[source.url] = source
            self._save(sources)
        if install_app:
            scope = installation or self._installation_for(release_ref)
            self._install_or_explain(scope, release_ref)
        return source

    def sync(self) -> list[str]:
        messages: list[str] = []
        with self._lock():
            sources = self._load()
            if not sources:
                return ["No repositories are mirrored yet."]
            for source in sources.values():
                previous = source.tag
                self._import_latest(source)
                if source.tag == previous:
                    messages.append(f"{source.repo} {source.tag} is current")
                else:
                    messages.append(
                        f"Imported {source.ref} from {source.repo} {source.tag}"
                    )
            self._save(sources)
        return messages

    def list_sources(self) -> list[Source]:
        return list(self._load().values())

    def remove(self, url: str, uninstall: bool) -> Source:
        key = self._canonical_from_url(url)
        with self._lock():
            sources = self._load()
            source = sources.get(key)
            if source is None:
                raise MirrorError(f"{key} is not mirrored")
            if source.ref:
                self._delete_ref(source.ref)
            del sources[key]
            self._save(sources)
            if not sources:
                self._delete_remote_if_present("user")
                self._delete_remote_if_present("system")
        if uninstall and source.ref:
            scope = self._installed_scope(self._app_id(source.ref))
            if scope:
                self._flatpak(
                    scope,
                    [
                        "uninstall",
                        "--assumeyes",
                        "--noninteractive",
                        self._app_id(source.ref),
                    ],
                )
        return source

    def _install_or_explain(self, scope: str, ref: str) -> None:
        blocked: list[list[str]] = []
        try:
            self.ensure_remote(scope)
        except SystemPermissionError as exc:
            blocked.append(exc.command)
        if not blocked:
            try:
                self.install(scope, ref)
            except SystemPermissionError as exc:
                blocked.append(exc.command)
        else:
            app_id = self._app_id(ref)
            origin = self._origin(scope, app_id)
            install_cmd = [
                "flatpak",
                f"--{scope}",
                "install",
                "--assumeyes",
                "--noninteractive",
            ]
            if origin:
                install_cmd.append("--reinstall")
            install_cmd.extend([REMOTE_NAME, app_id])
            blocked.append(install_cmd)
        if not blocked:
            return
        lines = "\n".join(
            "  " + shlex.join(["sudo", *command]) for command in blocked
        )
        raise MirrorError(
            "The mirror is ready. Run this in a terminal to switch the system install:\n"
            + lines
        )

    def ensure_remote(self, installation: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        url = self.repo.resolve().as_uri()
        remotes = self._remotes(installation)
        if REMOTE_NAME not in remotes:
            self._flatpak(
                installation,
                [
                    "remote-add",
                    "--if-not-exists",
                    "--no-gpg-verify",
                    f"--title={REMOTE_TITLE}",
                    REMOTE_NAME,
                    url,
                ],
            )
            return
        if _same_repo_url(remotes[REMOTE_NAME], url):
            return
        self._flatpak(
            installation,
            ["remote-modify", "--no-gpg-verify", f"--url={url}", REMOTE_NAME],
        )

    def install(self, installation: str, ref: str) -> None:
        app_id = self._app_id(ref)
        origin = self._origin(installation, app_id)
        if origin == REMOTE_NAME:
            self._flatpak(
                installation,
                ["update", "--assumeyes", "--noninteractive", app_id],
            )
            return
        command = ["install", "--assumeyes", "--noninteractive", REMOTE_NAME, app_id]
        if origin:
            print(
                f"Reinstalling {app_id} from {REMOTE_NAME}. Application data is kept.",
                flush=True,
            )
            command.insert(1, "--reinstall")
        self._flatpak(installation, command)

    def _import_latest(self, source: Source) -> str:
        try:
            release = fetch_latest_release(source.owner, source.repo)
        except GitHubError as exc:
            raise MirrorError(str(exc)) from exc
        self._ensure_repo()
        if (
            source.tag == release.tag
            and source.asset == release.asset.name
            and source.ref
            and self._ref_exists(source.ref)
        ):
            return source.ref

        self.cache.mkdir(parents=True, exist_ok=True)
        bundle = self.cache / release.asset.name
        print(f"Downloading {release.asset.name}", flush=True)
        try:
            try:
                download(release.asset.url, bundle)
                verify_digest(bundle, release.asset.digest)
            except GitHubError as exc:
                raise MirrorError(str(exc)) from exc
            before = self._revs()
            self._run(
                [
                    "flatpak",
                    "build-import-bundle",
                    "--update-appstream",
                    str(self.repo),
                    str(bundle),
                ]
            )
            self._run(
                [
                    "flatpak",
                    "build-update-repo",
                    "--prune",
                    f"--title={REMOTE_TITLE}",
                    str(self.repo),
                ]
            )
            ref = self._imported_ref(before)
        finally:
            bundle.unlink(missing_ok=True)

        source.tag = release.tag
        source.asset = release.asset.name
        source.digest = release.asset.digest
        source.ref = ref
        return ref

    def _imported_ref(self, before: dict[str, str]) -> str:
        after = self._revs()
        changed = [ref for ref, rev in after.items() if before.get(ref) != rev]
        apps = [ref for ref in changed if ref.startswith("app/")]
        chosen = apps or changed
        if len(chosen) != 1:
            names = ", ".join(sorted(chosen)) or "(none)"
            raise MirrorError(f"import did not identify one Flatpak ref: {names}")
        return chosen[0]

    def _source_for_url(self, url: str) -> Source:
        key = self._canonical_from_url(url)
        existing = self._load().get(key)
        if existing:
            return existing
        owner, repo = parse_github_repo(url)
        return Source(url=canonical_url(owner, repo), owner=owner, repo=repo)

    def _canonical_from_url(self, url: str) -> str:
        try:
            owner, repo = parse_github_repo(url)
        except GitHubError as exc:
            raise MirrorError(str(exc)) from exc
        return canonical_url(owner, repo)

    def _installation_for(self, ref: str) -> str:
        app_id = self._app_id(ref)
        found = [
            scope
            for scope in ("user", "system")
            if self._origin(scope, app_id) is not None
        ]
        if not found:
            return "user"
        if len(found) == 1:
            return found[0]
        raise MirrorError(
            f"{app_id} is installed for both user and system; "
            "pass --user or --system"
        )

    def _installed_scope(self, app_id: str) -> str | None:
        found = [
            scope
            for scope in ("user", "system")
            if self._origin(scope, app_id) is not None
        ]
        if len(found) == 1:
            return found[0]
        return None

    def _origin(self, installation: str, app_id: str) -> str | None:
        result = self._flatpak(
            installation,
            [
                "list",
                "--app",
                "--columns=application,origin",
            ],
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise MirrorError(detail or f"could not list {installation} Flatpaks")
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] == app_id:
                return parts[1]
        return None

    def _remotes(self, installation: str) -> dict[str, str]:
        result = self._flatpak(
            installation,
            ["remotes", "--columns=name,url"],
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise MirrorError(detail or f"could not list {installation} remotes")
        remotes: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                remotes[parts[0]] = parts[1]
            elif len(parts) == 1 and parts[0]:
                remotes[parts[0]] = ""
        return remotes

    def _delete_remote_if_present(self, installation: str) -> None:
        if REMOTE_NAME not in self._remotes(installation):
            return
        self._flatpak(installation, ["remote-delete", REMOTE_NAME], check=False)

    def _ensure_repo(self) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        if (self.repo / "config").exists():
            return
        self._run(["ostree", f"--repo={self.repo}", "init", "--mode=archive-z2"])

    def _ref_exists(self, ref: str) -> bool:
        return ref in self._revs()

    def _revs(self) -> dict[str, str]:
        if not (self.repo / "config").exists():
            return {}
        result = self._run(["ostree", f"--repo={self.repo}", "refs"], check=False)
        if result.returncode != 0:
            raise MirrorError(result.stderr.strip() or "ostree refs failed")
        revs: dict[str, str] = {}
        for ref in result.stdout.splitlines():
            ref = ref.strip()
            if not ref:
                continue
            parsed = self._run(
                ["ostree", f"--repo={self.repo}", "rev-parse", ref],
                check=False,
            )
            if parsed.returncode == 0:
                revs[ref] = parsed.stdout.strip()
        return revs

    def _delete_ref(self, ref: str) -> None:
        if not self._ref_exists(ref):
            return
        self._run(["ostree", f"--repo={self.repo}", "refs", "--delete", ref])
        self._run(
            [
                "flatpak",
                "build-update-repo",
                "--prune",
                f"--title={REMOTE_TITLE}",
                str(self.repo),
            ]
        )

    def _load(self) -> dict[str, Source]:
        if not self.state_path.exists():
            return {}
        payload = json.loads(self.state_path.read_text())
        sources = {}
        for item in payload.get("sources", []):
            source = Source.from_json(item)
            sources[source.url] = source
        return sources

    def _save(self, sources: dict[str, Source]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"sources": [source.to_json() for source in sources.values()]}
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(self.state_path)

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".lock"
        with lock_path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _flatpak(self, installation: str, args: list[str], check: bool = True):
        if installation not in ("user", "system"):
            raise MirrorError(f"unknown installation {installation}")
        command = ["flatpak", f"--{installation}", *args]
        read_only = bool(args) and args[0] in {"list", "remotes", "info", "ps"}
        if installation != "system" or read_only or os.geteuid() == 0:
            return self._run(command, check=check)

        sudoed = self._run(["sudo", "-n", *command], check=False)
        if sudoed.returncode == 0:
            return sudoed
        if not check:
            return sudoed
        raise SystemPermissionError(command)

    def _run(self, command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, text=True, capture_output=True)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise MirrorError(detail or f"command failed: {' '.join(command)}")
        return result

    @staticmethod
    def _app_id(ref: str) -> str:
        parts = ref.split("/")
        if len(parts) < 2:
            raise MirrorError(f"unexpected Flatpak ref {ref}")
        return parts[1]


def _same_repo_url(current: str, wanted: str) -> bool:
    def normalize(value: str) -> str:
        text = value.strip().rstrip("/")
        if text.startswith("file://"):
            text = text[len("file://") :]
        return os.path.realpath(text)

    if not current:
        return False
    try:
        return normalize(current) == normalize(wanted)
    except OSError:
        return current.rstrip("/") == wanted.rstrip("/")
