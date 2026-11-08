# DJ Library

A Mac app that keeps your DJ music organised. Give it a playlist and it finds the songs you don't own yet on
[Soulseek](https://www.slsknet.org), checks each download really is the right song, and files it into a dated
folder (a "crate") such as `08112026`. It also tells you which songs you already have, finds duplicate files, upgrades
MP3s to lossless versions, and exports everything to Rekordbox.

Nothing is ever deleted. Wrong or replaced files are moved to a side folder so you can look at them first.

**You need:** a Mac with an Apple chip (M1 or newer) and an internet connection. Intel Macs: see
[For developers](#for-developers).

---

## 1. Install (about 5 minutes)

1. Go to the **[Releases page](../../releases/latest)** and download **DJ-Library-mac.zip**.
2. Double-click the zip to unpack it, then drag **DJ Library** into your **Applications** folder.
3. **The first time only:** the app isn't signed by Apple, so macOS blocks a normal double-click.
   Right-click (or Control-click) **DJ Library** in Applications, choose **Open**, then **Open** again.
   If you only see "cannot be opened": open **System Settings, Privacy & Security**, scroll down and click
   **Open Anyway**.

## 2. First-time setup (about 5 minutes)

Open the app and click **Settings**.

1. **Make a free Soulseek account** at [slsknet.org](https://www.slsknet.org). Pick any username and password, no
   email needed. You don't need to install their program.
2. In **Soulseek setup**, click **Install downloader**. It downloads once and shows a green tick.
3. Type your Soulseek username and password and click **Save login**. They are stored only on your Mac.
4. **DJ folder** is where your music lives. The default is a folder called `DJ` on your Desktop. Change it if your
   music is somewhere else, then click **Rescan now** so the app learns what you already have.

> **Important:** if you use the Soulseek app *Nicotine+* or *SoulseekQt*, quit it before downloading. Soulseek allows
> one login at a time, and the app will warn you if the other one is running.

## 3. Download a playlist

1. **Get your playlist as a CSV file.** Export it from Spotify with a free tool such as
   [Exportify](https://exportify.net), or from your DJ software. Or, if you connect Spotify (below), just paste a
   playlist link.
2. Open **Playlists**. Drag the CSV onto the box (or click it to choose the file). Songs you already own are marked
   as owned straight away.
3. Click the playlist row. The **Crate** box fills in with today's date; that's the folder the new music goes into.
4. Click **Preview** to see what it would download without downloading anything, then **Download**.
5. Watch progress on the **Jobs** screen. A playlist of 50 songs takes roughly 10 to 30 minutes. You can close the
   window; the app keeps running while it's open.

### Spotify (optional)

Pasting Spotify links is optional; CSV import works without it. To connect, open **Settings, Spotify** and follow the
three steps on screen (create a free "app" on Spotify's developer page and paste its Client ID). Spotify only allows
reading playlists you own or share with someone, and doesn't provide BPM or key.

## 4. When something needs a look

- **Review** lists downloads the app wasn't sure about. Click **It's right** to keep the file, or **Wrong song** to set it aside (it is never deleted).
- **Failed** lists songs nobody has (or that keep failing). The app retries them for you over several days, then
  marks them **Buy** so you know to purchase them.
- **Library** shows everything you own, searchable by artist, key, BPM and format.

## 5. Get it into Rekordbox, Serato or Traktor

On **Library**, click **Export for Rekordbox**. The app writes a file and keeps it up to date automatically.

- **Rekordbox:** open *Preferences, Advanced, Database* and set **rekordbox xml** to the file shown in the app. Then
  open the **rekordbox xml** section in the left sidebar. Your crates and playlists appear there; drag them into your
  collection.
- **Serato / Traktor / others:** use the `.m3u8` playlists in the `m3u` folder next to the export.

## 6. Extras

- **Upgrades:** lists tracks you only have as MP3/M4A. Tick some and click **Upgrade N selected**. The app looks for a lossless
  copy (FLAC/WAV/AIFF) of the same version and length, copies your tags onto it and moves the old file to
  `Desktop/soulseek/downloads/replaced`. If the track was already in Rekordbox, right-click it there and choose
  **Relocate** to keep your cue points.
- **Duplicates:** finds the same song stored twice, even with different file names or formats. You choose which copy
  to keep; the other is moved to `Desktop/soulseek/downloads/duplicates`. This needs a small free helper: open the
  **Terminal** app once and run `brew install chromaprint` (needs [Homebrew](https://brew.sh)). The screen tells you
  if it's missing.

## Troubleshooting

| What you see | What to do |
|---|---|
| "Nicotine+ is running" | Quit Nicotine+ or SoulseekQt, then try again. |
| "A test search returned nothing" | The network is blocking Soulseek (common on office or school Wi-Fi). Try home Wi-Fi or a phone hotspot. |
| "sockseek was not found" | Settings, **Install downloader**. |
| Login errors | Re-enter your Soulseek username and password in Settings. |
| Lots of songs "not found" | Normal for rare tracks; Soulseek only has what people share. Check **Failed** later. |
| The window is blank | Quit and reopen the app; if it persists, open <http://127.0.0.1:8765> in your browser. |

Your data (the track database) is in `~/Library/Application Support/djlib/`. Delete that folder to start fresh. Your
music is never touched by that.

---

## For developers

Python 3.12, FastAPI, SQLAlchemy/SQLite, React + TypeScript. Soulseek access goes through
[sockseek](https://github.com/fiso64/sockseek) (AGPL, downloaded at first run, not bundled).

```sh
brew install uv node chromaprint
uv sync
(cd web-src && npm ci && npm run build)   # builds the UI into src/djlib/web
uv run djlib serve                        # http://127.0.0.1:8765
uv run python -m playwright install chromium && uv run pytest   # tests
packaging/build_app.sh                    # dist/DJ Library.app
```

Command line: `uv run djlib --help` (import, download, dedupe, export, upgrade, duplicates).

The app never writes to Rekordbox's own database, never deletes library files, and verifies every download (title,
artist, measured duration) before filing it.
