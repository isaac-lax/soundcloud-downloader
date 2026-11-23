# SoundCloud Downloader

Paste a SoundCloud link in your browser, click Download, and the track lands
in Apple Music with dead space trimmed, loudness-normalized to −14.5 LUFS, as a
320 kbps MP3 with tags and cover art. No Terminal needed.

It replaces the manual workflow of: download → Audacity (trim silence, Loudness
Normalization to −14.5 LUFS) → Export MP3 (320 kbps, 44.1 kHz, stereo) → save
into Apple Music's "Automatically Add to Music" folder.

**The Mac app updates itself.** Every time you open it, it fetches the latest
version of this repository before starting, so improvements pushed here are
live on every Mac the next time the app is opened. Install it once per Mac.

## Install (Mac, once per computer)

1. Download `SoundCloud Downloader.zip` from the [mac-app](mac-app/) folder of this repo
   and unzip it.
2. Drag `SoundCloud Downloader.app` into your Applications folder.
3. **First launch only:** right-click the app and choose **Open**, then **Open**
   again in the warning. macOS shows this once for apps not from the App Store.
4. The first run sets things up: it downloads the latest code, plus yt-dlp and
   ffmpeg into a private folder (about a minute, needs internet). A notification
   shows while it works.
5. Your browser opens at `http://localhost:8765`.

If a dialog asks to install Apple's **command line developer tools**, click
Install, wait for it to finish, then open the app again. That only happens on
Macs that have never had Python. Alternatively install Python from python.org first.

## Use

1. Open **SoundCloud Downloader**. If it's already running, this just brings the
   page back up. If a newer version was pushed, it restarts with the new one.
   You can also click **Update & restart** at the bottom of the page at any time:
   it fetches the latest version, restarts, and reloads the page by itself. The
   page says when a newer version is available.
2. Paste one or more SoundCloud links (tracks, sets, or profiles), one per line.
3. Click **Download**. Links are resolved into a list of tracks (a set or profile
   becomes one row per track), each with its own progress bar through download,
   silence check, loudness, and export. With several tracks an overall bar shows
   how far the batch is. A bad link fails on its own row without stopping the rest.
   Apple Music imports each track within a few seconds of it finishing.

Options on the page:
- **Trim silence at start/end**: removes dead space longer than 3 seconds at the
  beginning or end (anything quieter than −50 dB). Keeps 0.3 s before the first
  sound and 1 s after the last, so fade-outs survive. Silence in the middle of a
  track is never touched.
- **Normalize to −14.5 LUFS**: untick to keep the original loudness, or change
  the number. Loudness is measured after trimming.

The version running is shown at the bottom of the page.

Files save to `~/Music/Music/Media.localized/Automatically Add to Music.localized`
when that folder exists, otherwise to a `downloads` folder.

## Quitting

The app runs quietly with no Dock icon. To stop it: Activity Monitor, search
`Python`, select the one whose path ends in `SoundCloud Downloader/venv/bin/python`,
and Quit it. Leaving it running is harmless.

## If something breaks

- Log: `~/Library/Application Support/SoundCloud Downloader/app.log`
- Full reset: delete `~/Library/Application Support/SoundCloud Downloader` and open the app again.
- If the update download fails (offline), the app keeps using the last version it fetched.

## How it's put together

- `scdl.py` – download (yt-dlp), silence trim + loudness measure + MP3 export (ffmpeg).
  Also a command-line tool: `python3 scdl.py <url>` with `--no-trim`, `--no-normalize`, `--lufs N`, `-o folder`.
- `scdl_web.py` + `index.html` – the local web page.
- `launch.sh` – sets up the Python environment and starts the page. Updatable.
- `VERSION` – bump this with every change; the app restarts when it sees a new one.
- `mac-app/` – the double-click app. It only downloads this repo and runs `launch.sh`,
  with a bundled copy as an offline fallback.

## Note

Only download audio you have the right to keep: your own uploads, tracks the
artist has marked as free to download, or Creative Commons material.
