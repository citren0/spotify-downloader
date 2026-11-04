#!/usr/bin/env python3
"""
Spotify Playlist Downloader
===========================
Download a Spotify playlist to local MP3 files.

How it works:
  1. Track metadata (title, artists, album, cover art) is read from the
     Spotify Web API.
  2. Audio for each track is downloaded from YouTube (via yt-dlp), picking
     the search result whose duration best matches the Spotify track.
  3. The audio is converted to MP3 (ffmpeg) and tagged with full ID3
     metadata, including embedded album art.

Usage:
    python download_playlist.py <playlist_url_or_id> [options]

Spotify credentials (Client ID + Secret from
https://developer.spotify.com/dashboard) can be provided via:
    --client-id / --client-secret   (command line)
    SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET or SPOTIPY_CLIENT_ID /
    SPOTIPY_CLIENT_SECRET  (environment variables)
    config.json next to this script   {"client_id": "...", "client_secret": "..."}
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:
    import spotipy
    import yt_dlp
    from mutagen.id3 import (
        APIC,
        ID3,
        ID3NoHeaderError,
        TALB,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
    )
except ImportError as exc:
    sys.exit(
        f"Missing dependency: {exc.name}\n"
        f"Install everything with:  pip install -r requirements.txt"
    )

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

# OAuth settings for user-level access (private/collaborative playlists).
# The redirect URI must be added to your app in the Spotify dashboard.
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"
TOKEN_CACHE = SCRIPT_DIR / ".spotify_token_cache.json"

# yt-dlp player clients tried in order when YouTube blocks the default one.
PLAYER_CLIENT_FALLBACKS = [None, "web_safari", "tv"]

# Filename characters that are problematic on common filesystems.
INVALID_FS_CHARS = r'<>:"/\|?*\x00-\x1f'
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Track:
    position: int
    title: str
    artists: list[str]
    album: str | None
    duration_ms: int | None
    track_number: int | None
    total_tracks: int | None
    disc_number: int | None
    release_date: str | None
    cover_url: str | None
    is_local: bool = False

    @property
    def display(self) -> str:
        artist = ", ".join(self.artists) if self.artists else "Unknown Artist"
        return f"{artist} - {self.title}"

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else "Unknown Artist"


@dataclass
class Result:
    track: Track
    status: str  # "ok" | "skip" | "fail"
    path: Path | None = None
    error: str | None = None


# --------------------------------------------------------------------------
# Filename helpers
# --------------------------------------------------------------------------

def sanitize(text: str, max_len: int = 120) -> str:
    """Make a string safe to use as a filename component."""
    text = re.sub(f"[{INVALID_FS_CHARS}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "Untitled"
    if text.upper() in WINDOWS_RESERVED:
        text = f"_{text}"
    return text[:max_len].rstrip(" .")


def build_filename(track: Track, indexed: bool = True) -> str:
    artist = sanitize(track.primary_artist)
    title = sanitize(track.title)
    if indexed:
        stem = f"{track.position:02d} - {artist} - {title}"
    else:
        stem = f"{artist} - {title}"
    return sanitize(stem, max_len=180) + ".mp3"


def unique_path(directory: Path, name: str) -> Path:
    """Return a path in `directory` that does not collide with existing files."""
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


# --------------------------------------------------------------------------
# Spotify
# --------------------------------------------------------------------------

def load_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve Spotify API credentials: CLI > environment > config.json."""
    client_id = args.client_id
    client_secret = args.client_secret

    if not client_id or not client_secret:
        client_id = client_id or os.environ.get("SPOTIFY_CLIENT_ID") or os.environ.get("SPOTIPY_CLIENT_ID")
        client_secret = client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET") or os.environ.get("SPOTIPY_CLIENT_SECRET")

    if (not client_id or not client_secret) and CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            print(cfg)
        except json.JSONDecodeError as exc:
            sys.exit(f"Could not parse {CONFIG_PATH}: {exc}")
        client_id = client_id or cfg.get("client_id")
        client_secret = client_secret or cfg.get("client_secret")

    if not client_id or not client_secret:
        sys.exit(
            "Spotify API credentials are required.\n"
            "  1. Create an app at https://developer.spotify.com/dashboard\n"
            "  2. Copy its Client ID and Client Secret, then either:\n"
            "     - pass  --client-id ... --client-secret ...\n"
            "     - set   SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET\n"
            f"     - create {CONFIG_PATH}  with {{\"client_id\": \"...\", \"client_secret\": \"...\"}}"
        )
    return client_id, client_secret


def make_user_auth_client(client_id: str, client_secret: str, open_browser: bool) -> spotipy.Spotify:
    """User-level client (OAuth authorization code).

    Spotify requires a user login for playlist track data. The first run
    opens a browser for a one-time login; the token is cached in
    TOKEN_CACHE and refreshed automatically on later runs.
    """
    auth = spotipy.SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_handler=spotipy.cache_handler.CacheFileHandler(
            cache_path=str(TOKEN_CACHE),
        ),
        open_browser=open_browser,
    )
    return spotipy.Spotify(auth_manager=auth)


def parse_playlist_arg(value: str) -> str:
    """Accept a playlist URL, URI, or bare ID."""
    match = re.search(r"(?:spotify:playlist:|open\.spotify\.com/playlist/)([A-Za-z0-9]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", value):
        return value
    sys.exit(
        f"Could not parse a playlist from: {value!r}\n"
        "Expected something like:\n"
        "  https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M\n"
        "  spotify:playlist:37i9dQZF1DXcBWIGoYBM5M\n"
        "  37i9dQZF1DXcBWIGoYBM5M"
    )


def fetch_playlist(sp: spotipy.Spotify, playlist_id: str) -> tuple[str, list[Track]]:
    """Fetch playlist name and all its tracks (handling pagination).

    Raises spotipy.exceptions.SpotifyException on API errors.
    """
    playlist = sp.playlist(playlist_id)

    name = playlist.get("name") or f"playlist_{playlist_id}"

    tracks: list[Track] = []
    position = 0
    skipped = {"episode": 0, "unavailable": 0}

    results = sp.playlist_tracks(playlist_id, limit=100)
    while True:
        for item in results.get("items", []):
            position += 1
            t = item.get("item")
            if not t:
                skipped["unavailable"] += 1
                continue
            if t.get("type") == "episode":
                skipped["episode"] += 1
                continue

            album = t.get("album") or {}
            images = album.get("images") or []
            cover_url = images[0]["url"] if images else None

            tracks.append(Track(
                position=position,
                title=t.get("name") or "Unknown Title",
                artists=[a["name"] for a in t.get("artists", []) if a.get("name")],
                album=album.get("name"),
                duration_ms=t.get("duration_ms"),
                track_number=t.get("track_number"),
                total_tracks=album.get("total_tracks"),
                disc_number=t.get("disc_number"),
                release_date=album.get("release_date"),
                cover_url=cover_url,
                is_local=bool(t.get("is_local")),
            ))

        if not results.get("next"):
            break
        results = sp.next(results)

    if skipped["episode"]:
        log(f"Note: skipped {skipped['episode']} podcast episode(s).")
    if skipped["unavailable"]:
        log(f"Note: skipped {skipped['unavailable']} unavailable/removed track(s).")

    return name, tracks


# --------------------------------------------------------------------------
# YouTube (yt-dlp)
# --------------------------------------------------------------------------

def find_ffmpeg() -> str:
    """Locate an ffmpeg executable; fall back to the pip-bundled one."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit(
            "ffmpeg was not found.\n"
            "Install it with your package manager, or run:  pip install imageio-ffmpeg"
        )


def base_ydl_opts(quiet: bool = True) -> dict:
    return {
        "quiet": quiet,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
    }


def with_player_client(opts: dict, player_client: str | None) -> dict:
    if player_client:
        opts = dict(opts)
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    return opts


def search_videos(query: str, limit: int) -> list[dict]:
    """Return search results (flat metadata) for a query."""
    opts = base_ydl_opts()
    opts.update({
        "skip_download": True,
        "extract_flat": "in_playlist",  # resolve the search, keep entries flat (fast, includes duration)
        "default_search": f"ytsearch{limit}",
    })
    last_error: yt_dlp.utils.DownloadError | None = None
    for player_client in PLAYER_CLIENT_FALLBACKS:
        try:
            with yt_dlp.YoutubeDL(with_player_client(opts, player_client)) as ydl:
                info = ydl.extract_info(query, download=False)
            return [e for e in (info.get("entries") or []) if e and e.get("id")]
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            if not _is_transient_block(exc):
                raise
    raise last_error  # type: ignore[misc]


def _is_transient_block(exc: yt_dlp.utils.DownloadError) -> bool:
    """True when retrying with a different player client might help."""
    msg = str(exc).lower()
    return any(k in msg for k in ("bot", "sign in", "403", "timeout", "temporarily"))


def pick_best_video(entries: list[dict], target_ms: int | None) -> dict | None:
    """Choose the entry whose duration best matches the Spotify track."""
    if not entries:
        return None
    if not target_ms:
        return entries[0]
    target_s = target_ms / 1000
    best, best_diff = None, None
    for entry in entries:
        duration = entry.get("duration")
        if not duration:
            continue
        diff = abs(duration - target_s)
        if best_diff is None or diff < best_diff:
            best, best_diff = entry, diff
    # If nothing came close (e.g. only live/cover versions found), take it anyway.
    if best is None or best_diff > 90:
        return best or entries[0]
    return best


def download_video(video_id: str, tmp_dir: Path, bitrate: str, ffmpeg_path: str,
                   cookies: str | None, verbose: bool) -> Path:
    """Download a video's audio and convert it to MP3. Returns the MP3 path."""
    opts = base_ydl_opts(quiet=not verbose)
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": str(tmp_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "ffmpeg_location": ffmpeg_path,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": bitrate,
        }],
    })
    if cookies:
        opts["cookiefile"] = cookies

    url = f"https://www.youtube.com/watch?v={video_id}"
    last_error: Exception | None = None
    for player_client in PLAYER_CLIENT_FALLBACKS:
        try:
            with yt_dlp.YoutubeDL(with_player_client(opts, player_client)) as ydl:
                ydl.extract_info(url, download=True)
            mp3_files = list(tmp_dir.glob("*.mp3"))
            if not mp3_files:
                raise RuntimeError("download finished but no MP3 was produced")
            return mp3_files[0]
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            if not _is_transient_block(exc):
                raise
    raise last_error  # type: ignore[misc]


# --------------------------------------------------------------------------
# Tagging
# --------------------------------------------------------------------------

def fetch_url_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def apply_tags(mp3_path: Path, track: Track) -> None:
    """Write ID3v2.4 tags and embedded cover art to an MP3 file."""
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    tags.delall("TIT2"); tags.delall("TPE1"); tags.delall("TALB")
    tags.delall("TPE2"); tags.delall("TRCK"); tags.delall("TPOS")
    tags.delall("TDRC"); tags.delall("APIC")

    tags.add(TIT2(encoding=3, text=track.title))
    if track.artists:
        tags.add(TPE1(encoding=3, text=", ".join(track.artists)))
        tags.add(TPE2(encoding=3, text=track.primary_artist))
    if track.album:
        tags.add(TALB(encoding=3, text=track.album))
    if track.track_number:
        total = f"/{track.total_tracks}" if track.total_tracks else ""
        tags.add(TRCK(encoding=3, text=f"{track.track_number}{total}"))
    if track.disc_number:
        tags.add(TPOS(encoding=3, text=str(track.disc_number)))
    if track.release_date:
        tags.add(TDRC(encoding=3, text=track.release_date))

    if track.cover_url:
        try:
            data = fetch_url_bytes(track.cover_url)
            tags.add(APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # front cover
                desc="Cover",
                data=data,
            ))
        except Exception:
            pass  # cover art is nice-to-have

    tags.save(mp3_path, v2_version=4)


# --------------------------------------------------------------------------
# Per-track pipeline
# --------------------------------------------------------------------------

def build_queries(track: Track) -> list[str]:
    """Search query variations, best first."""
    artist = track.primary_artist
    title = track.title
    candidates = [
        f"{artist} - {title} audio",
        f"{artist} - {title}",
        f"{', '.join(track.artists[:2])} {title}",
        f"{title} {artist} topic",
    ]
    seen, queries = set(), []
    for q in candidates:
        if q not in seen:
            seen.add(q)
            queries.append(q)
    return queries


def process_track(track: Track, out_dir: Path, args: argparse.Namespace,
                  ffmpeg_path: str) -> Result:
    final_path = out_dir / build_filename(track, indexed=not args.no_index)
    if final_path.exists() and not args.force:
        return Result(track, "skip", path=final_path)

    last_error = "no matching video found"
    for query in build_queries(track):
        try:
            entries = search_videos(query, args.search_results)
            video = pick_best_video(entries, track.duration_ms)
            if not video:
                continue
            with tempfile.TemporaryDirectory(prefix="spdl-") as tmp:
                tmp_dir = Path(tmp)
                mp3 = download_video(
                    video["id"], tmp_dir, args.bitrate, ffmpeg_path,
                    args.cookies, args.verbose,
                )
                apply_tags(mp3, track)
                target = unique_path(out_dir, final_path.name)
                shutil.move(str(mp3), target)
            return Result(track, "ok", path=target)
        except Exception as exc:
            last_error = str(exc) or type(exc).__name__
    return Result(track, "fail", error=last_error)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def write_m3u(out_dir: Path, playlist_name: str, results: list[Result]) -> None:
    lines = ["#EXTM3U"]
    for res in results:
        if res.status not in ("ok", "skip") or res.path is None:
            continue
        duration = (res.track.duration_ms or -1) // 1000
        lines.append(f"#EXTINF:{duration},{res.track.display}")
        lines.append(res.path.name)
    (out_dir / "playlist.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="download_playlist.py",
        description="Download a Spotify playlist to local MP3 files "
                    "(metadata from Spotify, audio from YouTube).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("playlist", help="playlist URL, spotify:playlist:URI, or 22-char ID")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="output directory (default: ./downloads/<playlist name>)")
    parser.add_argument("--bitrate", default="192", choices=["128", "160", "192", "256", "320"],
                        help="MP3 bitrate in kbps")
    parser.add_argument("--threads", type=int, default=1,
                        help="number of tracks to download in parallel")
    parser.add_argument("--search-results", type=int, default=5,
                        help="number of YouTube results to consider per track")
    parser.add_argument("--force", action="store_true",
                        help="re-download tracks that already exist on disk")
    parser.add_argument("--no-index", action="store_true",
                        help="do not prefix filenames with playlist position")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the tracks and exit without downloading")
    parser.add_argument("--cookies", default=None,
                        help="cookies.txt file for YouTube (helps if downloads are blocked)")
    parser.add_argument("--no-browser", action="store_true",
                        help="print a login URL instead of opening a browser "
                             "(for headless/SSH sessions)")
    parser.add_argument("--verbose", action="store_true", help="show yt-dlp output")
    parser.add_argument("--client-id", default=None, help="Spotify API client ID")
    parser.add_argument("--client-secret", default=None, help="Spotify API client secret")
    args = parser.parse_args()

    playlist_id = parse_playlist_arg(args.playlist)

    client_id, client_secret = load_credentials(args)

    def open_playlist() -> tuple[str, list[Track]]:
        """Fetch playlist tracks with a user login (required by Spotify)."""
        sp = make_user_auth_client(client_id, client_secret,
                                   open_browser=not args.no_browser)
        return fetch_playlist(sp, playlist_id)

    try:
        playlist_name, tracks = open_playlist()
    except spotipy.oauth2.SpotifyOauthError as exc:
        sys.exit(f"Spotify login error: {exc}")
    except spotipy.exceptions.SpotifyException as exc:
        hint = {
            404: "Playlist not found. Check the ID/URL.",
            403: "Access denied.",
        }.get(exc.http_status, str(exc))
        sys.exit(f"Spotify API error: {hint}")

    log(f'Playlist: "{playlist_name}"  ({len(tracks)} tracks)')

    if args.dry_run:
        for t in tracks:
            log(f"  {t.position:3d}. {t.display}"
                + (f"  [{t.album}]" if t.album else ""))
        return

    out_dir = Path(args.output_dir) if args.output_dir else Path("downloads") / sanitize(playlist_name, 80)
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"Output:   {out_dir.resolve()}")

    ffmpeg_path = find_ffmpeg()
    results: list[Result] = []

    def run(track: Track) -> Result:
        res = process_track(track, out_dir, args, ffmpeg_path)
        total = len(tracks)
        with PRINT_LOCK:
            marker = {"ok": "OK  ", "skip": "SKIP", "fail": "FAIL"}[res.status]
            line = f"[{track.position}/{total}] {marker} {track.display}"
            if res.status == "fail":
                line += f"  ({res.error})"
            print(line, flush=True)
        return res

    try:
        if args.threads > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as pool:
                results = list(pool.map(run, tracks))
        else:
            results = [run(t) for t in tracks]
    except KeyboardInterrupt:
        log("\nInterrupted.")
        sys.exit(130)

    ok = [r for r in results if r.status == "ok"]
    skipped = [r for r in results if r.status == "skip"]
    failed = [r for r in results if r.status == "fail"]

    if ok:
        write_m3u(out_dir, playlist_name, results)

    log("")
    log(f"Done. {len(ok)} downloaded, {len(skipped)} skipped, {len(failed)} failed.")
    if failed:
        log("Failed tracks:")
        for r in failed:
            log(f"  - {r.track.display}  ({r.error})")
        sys.exit(1)


if __name__ == "__main__":
    main()
