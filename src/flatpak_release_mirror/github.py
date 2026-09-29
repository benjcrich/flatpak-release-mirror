"""GitHub release lookup for .flatpak assets."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

USER_AGENT = "flatpak-release-mirror"
ARCH_TOKENS = {
    "x86_64": ("x86_64", "amd64"),
    "aarch64": ("aarch64", "arm64"),
}


class GitHubError(Exception):
    """A GitHub link or release could not be used."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    digest: str | None


@dataclass(frozen=True)
class Release:
    tag: str
    asset: Asset


def host_arch() -> str:
    machine = os.uname().machine
    if machine in ("amd64", "x86_64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    return machine


def parse_github_repo(url: str) -> tuple[str, str]:
    """Return (owner, repo) from a GitHub repository or releases URL."""
    raw = url.strip()
    if not raw:
        raise GitHubError("missing GitHub link")

    if raw.startswith("git@github.com:"):
        path = raw.split(":", 1)[1]
    else:
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise GitHubError(
                "give a GitHub link such as https://github.com/owner/repo"
            )
        host = parsed.netloc.lower().removeprefix("www.")
        if host != "github.com":
            raise GitHubError("only github.com links are supported")
        path = parsed.path

    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[2] == "releases":
        parts = parts[:2]
    elif parts and parts[-1] == "releases" and len(parts) >= 3:
        parts = parts[:-1]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise GitHubError(
            "give a GitHub link such as https://github.com/owner/repo"
        )
    repo = parts[1][: -len(".git")] if parts[1].endswith(".git") else parts[1]
    return parts[0], repo


def canonical_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}"


def choose_flatpak_asset(assets: list[Asset], arch: str) -> Asset:
    bundles = [asset for asset in assets if asset.name.endswith(".flatpak")]
    if not bundles:
        raise GitHubError("the latest release has no .flatpak file")

    tokens = ARCH_TOKENS.get(arch, (arch,))
    matched = [
        asset for asset in bundles if any(token in asset.name for token in tokens)
    ]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        names = ", ".join(asset.name for asset in matched)
        raise GitHubError(
            f"the latest release has several {arch} .flatpak files: {names}"
        )
    if len(bundles) == 1:
        return bundles[0]
    names = ", ".join(asset.name for asset in bundles)
    raise GitHubError(
        f"the latest release has no {arch} .flatpak file (found {names})"
    )


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise GitHubError(f"no GitHub release found at {url}") from exc
        if exc.code in (403, 429):
            raise GitHubError(
                "GitHub rate limit reached; set GITHUB_TOKEN and try again"
            ) from exc
        raise GitHubError(f"GitHub API returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"could not reach GitHub: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise GitHubError("GitHub returned an unexpected response")
    return payload


def fetch_latest_release(owner: str, repo: str, arch: str | None = None) -> Release:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    payload = _request_json(url)
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise GitHubError(f"{owner}/{repo} has no latest release")
    assets: list[Asset] = []
    for item in payload.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        download = item.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(download, str):
            continue
        size = item.get("size")
        digest = item.get("digest")
        assets.append(
            Asset(
                name=name,
                url=download,
                size=size if isinstance(size, int) else 0,
                digest=digest if isinstance(digest, str) and digest else None,
            )
        )
    asset = choose_flatpak_asset(assets, arch or host_arch())
    return Release(tag=tag, asset=asset)


def download(url: str, dest) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise GitHubError(f"download failed with HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"download failed: {exc.reason}") from exc


def verify_digest(path, digest: str | None) -> None:
    if not digest:
        return
    algo, _, expected = digest.partition(":")
    if algo != "sha256" or not expected:
        raise GitHubError(f"unsupported checksum {digest}")
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual.lower() != expected.lower():
        raise GitHubError(
            f"checksum mismatch: expected {expected}, got {actual}"
        )
