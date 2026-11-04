# Spotify Playlist Downloader

Download a Spotify playlist to local MP3 files with full ID3 tags and
embedded album art.

**How it works:** Spotify's API does not allow direct audio downloads, so the
tool reads the playlist *metadata* (title, artists, album, cover art) from the
Spotify Web API, then finds and downloads each track's *audio* from YouTube via
`yt-dlp`, converts it to MP3, and tags it using the Spotify metadata.

```
Spotify Web API ──► track metadata ──┐
                                     ├──►  MP3 + ID3 tags + album art
YouTube (yt-dlp) ─► audio ───────────┘
```

## Setup

### 1. Install dependencies

Python 3.9+ required.

```bash
cd spotify-playlist-downloader
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

(On Windows use `venv\Scripts\pip ...`.) `ffmpeg` is needed for MP3 conversion —
if it is not installed on your system, the bundled `imageio-ffmpeg` package
provides one automatically.

### 2. Get Spotify API credentials

1. Go to <https://developer.spotify.com/dashboard> and log in with your
   Spotify account.
2. Click **Create app** (name/description don't matter).
3. In the app settings, set the **Redirect URI** to
   `http://localhost:8888/callback` (required for the login flow) and click
   **Add**.
4. Copy the **Client ID** and **Client Secret**.

Then choose one of these ways to provide them:

| Method | How |
|---|---|
| Config file (recommended) | Create `config.json` next to `download_playlist.py`: `{"client_id": "YOUR_ID", "client_secret": "YOUR_SECRET"}` |
| Environment variables | `export SPOTIFY_CLIENT_ID=...` and `export SPOTIFY_CLIENT_SECRET=...` |
| Command line | `--client-id ... --client-secret ...` |

> **About the login:** Spotify now requires a user login even for public
> playlist track data. On the first run the tool opens your browser for a
> one-time Spotify login; the token is cached in
> `.spotify_token_cache.json` and refreshed automatically, so you only log
> in once. On headless machines use `--no-browser` and paste the redirect
> URL back into the prompt.

## Usage

```bash
venv/bin/python download_playlist.py <playlist>
```

where `<playlist>` is a playlist URL, `spotify:playlist:...` URI, or a bare
playlist ID, e.g.:

```bash
venv/bin/python download_playlist.py https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
```

Files are saved to `downloads/<playlist name>/` as
`NN - Artist - Title.mp3`, plus a `playlist.m3u` file.

### Options

```
-o, --output-dir DIR    save elsewhere (default: ./downloads/<playlist name>)
--bitrate KBPS           MP3 bitrate: 128, 160, 192 (default), 256, 320
--threads N              download N tracks in parallel (default 1)
--search-results N       YouTube results to consider per track (default 5)
--force                  re-download files that already exist
--no-index               don't prefix filenames with the playlist position
--user-auth              log in with your Spotify account (private/
                         collaborative/editorial playlists)
--no-browser             print a login URL instead of opening a browser
                         (headless/SSH sessions)
--dry-run                just list the playlist's tracks
--cookies FILE          use a cookies.txt for YouTube (see troubleshooting)
--verbose                show yt-dlp output
```

Files that already exist are skipped, so you can safely re-run the command to
resume an interrupted download.

### Example

```bash
# Download "Today's Top Hits" at 320 kbps, 3 tracks in parallel
venv/bin/python download_playlist.py 37i9dQZF1DXcBWIGoYBM5M --bitrate 320 --threads 3
```

## Troubleshooting

- **"INVALID_REDIRECT_URI" on login** — open your app settings at
  <https://developer.spotify.com/dashboard>, add
  `http://localhost:8888/callback` as a Redirect URI, save, and retry.
- **"Sign in to confirm you're not a bot"** — YouTube sometimes blocks
  automated downloads, especially from cloud servers. The tool automatically
  retries with alternative players; if it still fails, export your browser's
  cookies to a `cookies.txt` file (e.g. with a browser extension) and pass
  `--cookies cookies.txt`.
- **Playlist not found** — make sure the playlist is public and the ID is
  correct.
- **A few tracks fail** — some tracks (obscure remixes, region-locked
  releases) simply aren't on YouTube. They're listed at the end of the run;
  re-running retries them.
- **yt-dlp suddenly stops working** — YouTube changes often; update with
  `venv/bin/pip install -U yt-dlp`.

## Legal note

Downloading YouTube audio may be against the YouTube Terms of Service in some
jurisdictions, and redistribution of copyrighted music is generally not
permitted. Use this tool for personal use only.
