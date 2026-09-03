[简体中文](README.md) | English

# SaveSync — Game Save Cloud Sync

Sync your game saves between two machines. Scans save files on both macOS
(including Windows games inside CrossOver bottles) and Windows, packs them up
to Nutstore / any S3-compatible storage, and restores them with one click when
you switch machines.

## Why

Plenty of people own both a Mac and a Windows PC, and play the same games on
each.

If the game is on Steam, Steam Cloud already handles it — nothing to do here.

The trouble is **locally installed games**. Whether it's a Windows game running
on a Mac through CrossOver, or a game installed straight onto a drive, its saves
are scattered across Documents, AppData, and folders inside the game's install
directory. Getting them from one machine to the other means manually copying
files — easy to forget, easy to copy the wrong version.

SaveSync exists for exactly that: when you play local games on both macOS and
Windows, it packs your saves up to the cloud and restores them in one click on
the other machine.

## Features

- **Zero dependencies** — Python standard library only, nothing to `pip install`
- **Cross-platform** — macOS (including Windows games in CrossOver bottles) and Windows
- **Whole-disk scan** — newly installed games are detected and added to the sync list automatically
- **Conflict protection** — refuses to upload when the cloud copy is newer, refuses to
  overwrite when you have unsynced local changes
- **Automatic backups** — backs up local saves before overwriting: 10 local copies,
  5 in the cloud

## Getting started

### macOS

Download `SaveSync-macOS.zip` from the
[Releases](https://github.com/RedDevil233666/savesync/releases) page, unzip, and
drag `存档同步.app` into Applications.

First launch may show "from an unidentified developer": System Settings →
Privacy & Security → Open Anyway.

### Windows

Download `SaveSync-Windows.exe` from the
[Releases](https://github.com/RedDevil233666/savesync/releases) page and double-click.
Python is not required.

If SmartScreen shows "Windows protected your PC": click **More info** → **Run anyway**.
(Single-file executables built from Python are not code-signed, so this prompt is expected.)

To build it yourself: install
[Python 3.8+](https://www.python.org/downloads/) (check "Add python.exe to PATH"
during setup), put `build_windows.bat`, `savesync.py` and `savesync_gui.py` in the
same folder, and double-click the bat. Output lands at `dist\SaveSync.exe`.

## Interface

Four buttons:

| Button | What it does |
| --- | --- |
| Scan | Scans every game save on this machine; new games are added to the sync list automatically |
| Upload | Packs local saves and uploads them to the cloud (click this after playing) |
| Download | Overwrites local saves with the cloud copy (click this before playing on the other machine) |
| Status | Shows the sync list with local/cloud status |

Use the "⚙ Configure cloud" button in the bottom-left corner to enter your
Nutstore credentials — once per machine.

## Configuring the cloud (Nutstore WebDAV)

1. Log in to the Nutstore (坚果云) web app → Account Info → Security → **Add App Password**
2. Fill in the dialog:

```
Server: https://dav.jianguoyun.com/dav/
Account: your Nutstore email
Password: the app password you just generated (not your login password)
```

Any generic WebDAV server or S3-compatible bucket works too — see the CLI section below.

## What gets scanned

Scanning treats every user environment (your machine plus each CrossOver bottle)
as a separate Windows user directory and walks through all of them:

- `Documents/My Games/<game>`
- `Documents/<vendor>/<game>`
- `Saved Games/<game>`
- `AppData/LocalLow/<vendor>/<game>`
- `AppData/Local/<game>` (including Unreal Engine's `Saved/SaveGames`)
- `AppData/Roaming/<game>`
- Steam libraries: parses `libraryfolders.vdf` to find every library, then scans
  `common/<game>/` for subdirectories named save / 存档 / uds
- Game install directories: `~/games` (plus `Games` on every drive letter on Windows),
  looking for save / saves / savedata / savefiles / uds subdirectories

A directory counts as a save location if it holds a file with one of 12 known save
extensions (`.sav`, `.sl2`, `.lsv`, `.ess`, …) or a file whose name starts with
save / autosave / quicksave / slot / manualsave.

**Custom scan directories**: if your games live elsewhere, add them to
`~/.savesync/config.json`:

```json
{
  "scan_dirs": ["D:/Games", "/Volumes/SSD/Games"]
}
```

## Command line

The GUI covers everyday use; the CLI is there for scripting and troubleshooting.

```bash
# Scan (--add-known adds discovered games to the config automatically)
python savesync.py scan --add-known

# Upload / download
python savesync.py push              # everything
python savesync.py push elden-ring   # a specific game
python savesync.py pull --force      # overwrite local (backing up first)

# Status
python savesync.py status

# Configure the cloud
python savesync.py setup-webdav --url https://dav.jianguoyun.com/dav \
    --user you@example.com --password app-password

python savesync.py setup-s3 --endpoint <endpoint> --bucket <bucket> \
    --ak <AccessKey> --sk <SecretKey> --region auto

# Add or remove games manually
python savesync.py add my-game --name "My Game" --path "~/Documents/My Games/MyGame"
python savesync.py remove my-game
```

## File locations

```
~/.savesync/config.json    game list + cloud config (contains your password — keep it private)
~/.savesync/state.json     last sync time and other state
~/.savesync/backups/       local backups taken before overwriting (10 kept)
```

## Known limitations

- Games whose save folder isn't named save / saves / saved / savedata / savefiles /
  uds / 存档 won't be found — add them with the `add` command, or list the
  directory under `scan_dirs`
- Native macOS games (not running through CrossOver) are not added to the sync list
- Nutstore's free plan has a monthly traffic cap; save files are small so it's
  rarely an issue

## License

MIT
