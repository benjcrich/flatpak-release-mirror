# flatpak-release-mirror

Mirror `.flatpak` files from GitHub releases into one local Flatpak remote. Shelly and `flatpak update` can then update those apps. A GitHub repository is not itself a Flatpak remote; this imports each release bundle into an OSTree repository on your machine.

```bash
bin/flatpak-release-mirror add https://github.com/Wyze3306/BedrockOnLinux
```

That command:

1. Reads the latest GitHub release and downloads its `.flatpak` for this machine's architecture.
2. Checks the SHA-256 published on the release asset.
3. Imports it into `~/.local/share/flatpak-release-mirror/repo`.
4. Adds a Flatpak remote named `release-mirror` (unsigned, with `--no-gpg-verify`, matching the bundles GitHub ships).
5. Installs the app from that remote. If it was already installed from a one-shot bundle, it is reinstalled from `release-mirror` so updates work. Flatpak keeps application data unless you pass `--delete-data`, and this tool never does.
6. Enables a user systemd timer that imports newer releases once a day.

The timer only refreshes the mirror. Apply the update with Shelly (`shelly upgrade flatpak`) or `flatpak update`.

## Commands

```bash
bin/flatpak-release-mirror add URL [--user|--system] [--no-install]
bin/flatpak-release-mirror sync
bin/flatpak-release-mirror list
bin/flatpak-release-mirror remove URL [--uninstall]
bin/flatpak-release-mirror enable-timer
bin/flatpak-release-mirror disable-timer
```

`add` follows an app that is already installed: a system install stays system-wide, a user install stays per-user. A new app is installed for the current user. Pass `--system` or `--user` to override. Changing a system install needs administrator permission. If that permission is missing, the command prints the exact `sudo` line to run; it does not install a second copy.

`sync` is what the daily timer runs. `remove` stops mirroring that repository and leaves the installed app in place. `--uninstall` removes the app and still keeps its data.

Put the launcher on your `PATH` if you want:

```bash
ln -s "$(pwd)/bin/flatpak-release-mirror" ~/.local/bin/flatpak-release-mirror
```

## Requirements

`flatpak`, `flatpak-builder` (for `flatpak build-import-bundle`), `ostree`, and Python 3.11+. No extra Python packages.

Set `GITHUB_TOKEN` if you add enough repositories to hit GitHub's anonymous rate limit. The token only needs public read access.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
