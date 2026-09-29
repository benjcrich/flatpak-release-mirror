"""Command line for the GitHub Flatpak release mirror."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from flatpak_release_mirror import __version__
from flatpak_release_mirror.mirror import REMOTE_NAME, Mirror, MirrorError, config_dir

SERVICE_NAME = "flatpak-release-mirror.service"
TIMER_NAME = "flatpak-release-mirror.timer"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flatpak-release-mirror",
        description=(
            "Mirror .flatpak files from GitHub releases into a local Flatpak "
            "remote that Shelly and flatpak update can use."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="mirror a GitHub repository and install its Flatpak")
    add.add_argument("url", help="https://github.com/owner/repo")
    scope = add.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_true", help="install for the current user")
    scope.add_argument("--system", action="store_true", help="install system-wide")
    add.add_argument(
        "--no-install",
        action="store_true",
        help="import the bundle without installing or switching the app",
    )

    commands.add_parser("sync", help="import newer GitHub releases for every mirrored app")
    commands.add_parser("list", help="show mirrored repositories")

    remove = commands.add_parser("remove", help="stop mirroring a GitHub repository")
    remove.add_argument("url", help="https://github.com/owner/repo")
    remove.add_argument(
        "--uninstall",
        action="store_true",
        help="also uninstall the Flatpak; application data is kept",
    )

    commands.add_parser("enable-timer", help="refresh the mirror once a day")
    commands.add_parser("disable-timer", help="stop the daily refresh")

    args = parser.parse_args(argv)
    try:
        return _run(args)
    except MirrorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    mirror = Mirror()
    if args.command == "add":
        installation = "user" if args.user else "system" if args.system else None
        source = mirror.add(args.url, installation, install_app=not args.no_install)
        print(f"Mirrored {source.url} {source.tag}")
        print(f"Ref {source.ref}")
        if not args.no_install:
            print(
                f"Installed from the {REMOTE_NAME} remote. "
                "shelly upgrade flatpak will see later releases after sync."
            )
        warning = enable_timer(_executable())
        if warning:
            print(warning, file=sys.stderr)
        return 0
    if args.command == "sync":
        for message in mirror.sync():
            print(message)
        return 0
    if args.command == "list":
        sources = mirror.list_sources()
        if not sources:
            print("No repositories are mirrored yet.")
            return 0
        for source in sources:
            print(f"{source.url}  {source.tag or '-'}  {source.ref or '-'}")
        return 0
    if args.command == "remove":
        source = mirror.remove(args.url, uninstall=args.uninstall)
        print(f"Stopped mirroring {source.url}")
        return 0
    if args.command == "enable-timer":
        warning = enable_timer(_executable())
        if warning:
            print(warning, file=sys.stderr)
            return 1
        print("Daily refresh enabled")
        return 0
    if args.command == "disable-timer":
        disable_timer()
        print("Daily refresh disabled")
        return 0
    raise MirrorError(f"unknown command {args.command}")


def enable_timer(executable: Path) -> str | None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    service = directory / SERVICE_NAME
    timer = directory / TIMER_NAME
    service.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Import new GitHub Flatpak release bundles",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=oneshot",
                f"ExecStart={executable} sync",
                "",
            ]
        )
    )
    timer.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Daily GitHub Flatpak release mirror refresh",
                "",
                "[Timer]",
                "OnCalendar=daily",
                "Persistent=true",
                "RandomizedDelaySec=1h",
                "",
                "[Install]",
                "WantedBy=timers.target",
                "",
            ]
        )
    )
    reload = subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        text=True,
        capture_output=True,
    )
    if reload.returncode != 0:
        detail = (reload.stderr or reload.stdout).strip()
        return (
            "Wrote the user timer, but systemd did not reload it"
            + (f": {detail}" if detail else "")
            + f"\nUnits are in {directory}"
        )
    enabled = subprocess.run(
        ["systemctl", "--user", "enable", "--now", TIMER_NAME],
        text=True,
        capture_output=True,
    )
    if enabled.returncode != 0:
        detail = (enabled.stderr or enabled.stdout).strip()
        return "Could not enable the timer" + (f": {detail}" if detail else "")
    return None


def disable_timer() -> None:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", TIMER_NAME],
        text=True,
        capture_output=True,
    )
    directory = config_dir()
    for name in (TIMER_NAME, SERVICE_NAME):
        path = directory / name
        path.unlink(missing_ok=True)
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        text=True,
        capture_output=True,
    )


def _executable() -> Path:
    return Path(sys.argv[0]).resolve()
