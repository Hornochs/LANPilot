# Installation

LANPilot runs on one Linux machine in your network that can reach the game servers. It
needs no agent on the game hosts.

## Packages

Packages are published on the
[releases page](https://github.com/lan-dot-party/LANPilot/releases) for:

| Distribution | Package |
|---|---|
| Debian 12, Debian 13 | `lanpilot_<version>-1~debian12_amd64.deb`, `…~debian13…` |
| Ubuntu 24.04, Ubuntu 26.04 | `lanpilot_<version>-1~ubuntu24.04_amd64.deb`, `…~ubuntu26.04…` |
| Fedora 44 | `lanpilot-<version>-1~fedora44.x86_64.rpm` |
| Rocky Linux 9 and compatible | `lanpilot-<version>-1~rocky9….x86_64.rpm` |
| Arch Linux | `lanpilot-<version>-1-x86_64.pkg.tar.zst` |

Pick the package for **your distribution release**. Each one contains a complete Python
environment built for that release's Python version and depends on exactly that version;
the package for another release will refuse to install. Installing needs no internet
access.

```bash
sudo apt install ./lanpilot_*.deb          # Debian, Ubuntu
sudo dnf install ./lanpilot-*.rpm          # Fedora, Rocky Linux
sudo pacman -U lanpilot-*.pkg.tar.zst      # Arch Linux
```

`SHA256SUMS` on the release page lists the checksums of all files. As it comes from the
same page, it only shows that a download is complete. That a package was built by this
project's release workflow from its source is shown by the build attestation, which the
[GitHub CLI](https://cli.github.com/) checks before you install:

```bash
gh attestation verify lanpilot_*.deb --repo lan-dot-party/LANPilot
```

The Python packages inside are pinned to exact versions and checked against their hashes
when the package is built (`packaging/requirements.txt`).

### First start

The package creates the system user `lanpilot`, the configuration and the key that
encrypts the game servers' passwords. It does not start the service, because one setting
is yours to make:

1. Set `public_url` in `/etc/lanpilot/lanpilot.toml` to the address users will open, for
   example `https://lanpilot.example.lan`. See [configuration.md](configuration.md) and,
   if you use one, [reverse-proxy.md](reverse-proxy.md).
2. Create the first administrator (run as root, the `lanpilot` command switches to the
   service user, so the database it creates belongs to the service):

   ```bash
   sudo lanpilot user create admin
   ```

3. Start the service:

   ```bash
   sudo systemctl enable --now lanpilot
   ```

Then open the address from step 1 and sign in. Logs: `journalctl -u lanpilot`.

Optional settings in the same file (see [configuration.md](configuration.md)):
`[scan] networks` switches on the LAN scan, `enable_console = true` the raw console for
administrators, `default_language = "de"` makes German the default language. For running an
event, see [lan-party-guide.md](lan-party-guide.md).

### What is where

| Path | Content |
|---|---|
| `/etc/lanpilot/lanpilot.toml` | configuration |
| `/etc/lanpilot/secret.key` | key that encrypts the stored game server passwords |
| `/var/lib/lanpilot/` | database and map image cache |
| `/opt/lanpilot/` | the application |

### Backup

Back up `/var/lib/lanpilot/` **and** `/etc/lanpilot/secret.key`. Without the key the stored
game server passwords cannot be read and have to be entered again. LANPilot also writes a
copy of the database next to it before every schema change
(`lanpilot.db.backup-<time>-<revision>`).

### Upgrade

Install the newer package the same way. Configuration, key and data are kept, the service
is restarted, and the database is brought to the new schema on start.

After upgrading the **distribution** to a new release, install the LANPilot package for
that release: the old one was built for the previous Python version.

### Removal

```bash
sudo apt remove lanpilot       # or: dnf remove lanpilot / pacman -R lanpilot
```

Configuration, key and data stay in place, so installing again brings LANPilot back as it
was. To delete them as well use `sudo apt purge lanpilot` on Debian and Ubuntu; on the
other distributions remove `/etc/lanpilot` and `/var/lib/lanpilot` by hand.

## Lost administrator password

```bash
sudo lanpilot user set-password admin
```

This also ends the user's sessions. `sudo lanpilot user list` shows the accounts.

## From source

For development or for distributions without a package. Requires Python 3.11 or newer.

```bash
git clone https://github.com/lan-dot-party/LANPilot.git
cd LANPilot
python3 -m venv .venv
.venv/bin/pip install .

export LANPILOT_SERVER__PUBLIC_URL=http://127.0.0.1:9030
export LANPILOT_DATA_DIR=./data
export LANPILOT_SECRET_KEY_FILE=./data/secret.key
mkdir -p data && .venv/bin/lanpilot secret generate-key --output data/secret.key
.venv/bin/lanpilot user create admin
.venv/bin/lanpilot serve
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the development setup.
