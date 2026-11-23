#!/usr/bin/env python3
"""
scdl.py - SoundCloud downloader with loudness normalization and Apple Music import.

One command replaces: download -> Audacity "Loudness Normalization" -> Export MP3
-> save into Apple Music's "Automatically Add to Music" folder.

Usage:
  python3 scdl.py <url> [<url> ...] [-o OUTDIR] [--lufs -14.5] [--no-normalize]

Defaults match the Audacity workflow:
  * trim silence (dead space) longer than a few seconds at the start and end
  * normalize perceived (integrated, EBU R128) loudness to -14.5 LUFS,
    pure gain change (no limiting), mono treated as dual-mono
  * export MP3, 320 kbps, 44100 Hz, stereo, with tags and cover art
  * save to ~/Music/Music/Media.localized/Automatically Add to Music.localized
    if that folder exists (Apple Music picks it up), otherwise ./downloads

Only download audio you have the right to keep: your own uploads, tracks the
artist has marked as free to download, or Creative Commons material.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# macOS Python installs often lack root certificates; use certifi's bundle if available.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    CERTIFI = True
except ImportError:
    CERTIFI = False

try:
    import yt_dlp
    from yt_dlp.postprocessor import PostProcessor
except ImportError:
    sys.exit("yt-dlp is not installed. Run:  pip3 install yt-dlp")

APPLE_MUSIC_DROP = Path.home() / "Music/Music/Media.localized/Automatically Add to Music.localized"


def find_ffmpeg() -> str | None:
    """Prefer the pip-installed ffmpeg (imageio-ffmpeg), fall back to one on PATH."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()


# ----------------------------------------------------------------------------
# Loudness measurement + MP3 export (the Audacity + Export step)
# ----------------------------------------------------------------------------

SILENCE_DB = -50.0      # anything quieter than this counts as silence
SILENCE_MIN = 3.0       # ...if it lasts at least this many seconds
PAD_START = 0.3         # seconds of silence to keep before the first sound
PAD_END = 1.0           # seconds to keep after the last sound (protects fade-outs)


def fmt_time(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def detect_silence(path: Path, threshold_db=SILENCE_DB, min_len=SILENCE_MIN):
    """Return (keep_start, keep_end, duration): the region of real audio, in seconds."""
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
           "-af", f"silencedetect=noise={threshold_db}dB:d={min_len}", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    if not m:
        raise RuntimeError("could not read duration:\n" + out[-800:])
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", out)]

    keep_start, keep_end = 0.0, duration
    if starts and starts[0] <= 0.5:                     # silence from the very beginning
        keep_start = ends[0] if ends else duration
    if starts and (len(ends) < len(starts) or ends[-1] >= duration - 0.5):   # silence to the very end
        keep_end = starts[-1]
    keep_start = max(0.0, keep_start - PAD_START)
    keep_end = min(duration, keep_end + PAD_END)
    if keep_end <= keep_start:                          # whole file is silent; leave it alone
        return 0.0, duration, duration
    return keep_start, keep_end, duration


def trim_filter(keep_start: float, keep_end: float, duration: float) -> str:
    """ffmpeg filter that cuts to [keep_start, keep_end]; empty string if nothing to cut."""
    if keep_start < 0.05 and keep_end > duration - 0.05:
        return ""
    return f"atrim=start={keep_start:.3f}:end={keep_end:.3f},asetpts=PTS-STARTPTS"


def measure_lufs(path: Path, pre_filter: str = "") -> float:
    """Integrated loudness (LUFS) per EBU R128, measuring mono as dual-mono."""
    af = (pre_filter + "," if pre_filter else "") + "aformat=channel_layouts=stereo,ebur128=framelog=quiet"
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-af", af, "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
    m = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+) LUFS", out)
    if not m:
        raise RuntimeError("could not measure loudness:\n" + out[-800:])
    return float(m.group(1))


def export_mp3(src: Path, dst: Path, gain_db: float, tags: dict, cover: Path | None,
               pre_filter: str = "") -> None:
    """Re-encode to 320 kbps / 44.1 kHz / stereo MP3 with trim + gain applied, adding tags + cover art."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if cover:
        cmd += ["-i", str(cover)]
    af = (pre_filter + "," if pre_filter else "") + f"volume={gain_db:.3f}dB"
    cmd += ["-map", "0:a:0", "-af", af,
            "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "320k",
            "-map_metadata", "-1", "-id3v2_version", "3"]
    if cover:
        # MP3 cover art must be JPEG or PNG; convert anything else (e.g. webp) to JPEG
        codec = ["-c:v", "copy"] if cover.suffix.lower() in (".jpg", ".jpeg", ".png") else ["-c:v", "mjpeg", "-pix_fmt", "yuvj420p"]
        cmd += ["-map", "1:v:0", *codec, "-disposition:v", "attached_pic",
                "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    for k, v in tags.items():
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    cmd.append(str(dst))
    subprocess.run(cmd, check=True)


class NormalizeAndExport(PostProcessor):
    """Runs once per downloaded track, after yt-dlp has embedded tags and artwork."""

    def __init__(self, target_lufs, normalize, trim=True, stage_cb=None):
        super().__init__()
        self.target = target_lufs
        self.normalize = normalize
        self.trim = trim
        self.stage_cb = stage_cb or (lambda *a, **k: None)

    def run(self, info):
        src = Path(info["filepath"])
        dst = src.with_suffix(".mp3")     # yt-dlp moves this to the final folder afterwards

        pre = ""
        if self.trim:
            self.stage_cb("Checking for silence", 72)
            ks, ke, dur = detect_silence(src)
            pre = trim_filter(ks, ke, dur)
            if pre:
                cut_start, cut_end = ks, dur - ke
                parts = []
                if cut_start >= 1:
                    parts.append(f"{fmt_time(cut_start)} of silence at the start")
                if cut_end >= 1:
                    parts.append(f"{fmt_time(cut_end)} of silence at the end")
                self.to_screen(f"Trimming {' and '.join(parts)}: {fmt_time(dur)} -> {fmt_time(ke - ks)}")
            else:
                self.to_screen("No silence to trim at start or end")

        gain = 0.0
        if self.normalize:
            self.stage_cb("Measuring loudness", 78)
            measured = measure_lufs(src, pre)
            gain = self.target - measured
            self.to_screen(f"Loudness {measured:.1f} LUFS -> {self.target} LUFS (gain {gain:+.1f} dB)")

        tags = {
            "title": info.get("track") or info.get("title"),
            "artist": info.get("artist") or info.get("uploader"),
            "album": info.get("album"),
            "genre": info.get("genre"),
            "date": (info.get("upload_date") or "")[:4],
            "comment": info.get("webpage_url"),
        }
        cover = next((Path(t["filepath"]) for t in info.get("thumbnails") or []
                      if t.get("filepath") and Path(t["filepath"]).exists()), None)

        self.to_screen(f"Exporting MP3 320 kbps / 44.1 kHz / stereo: {dst.name}")
        self.stage_cb("Exporting MP3", 88, file=dst.name, title=tags["title"], artist=tags["artist"])
        export_mp3(src, dst, gain, tags, cover, pre)
        self.stage_cb("Saving", 98)
        src.unlink()                      # remove the intermediate download
        if cover:
            cover.unlink()
            info.get("__files_to_move", {}).pop(str(cover), None)   # don't move it to the output folder
        info["filepath"] = str(dst)
        return [], info


# ----------------------------------------------------------------------------
# Download (yt-dlp)
# ----------------------------------------------------------------------------

def progress(d: dict) -> None:
    if d["status"] == "downloading":
        pct = d.get("_percent_str", "").strip()
        name = Path(d.get("filename", "")).name
        print(f"\r  {pct:>6}  {name}", end="", flush=True)
    elif d["status"] == "finished":
        print("\r  downloaded                                                  ", flush=True)


def build_options(outdir: Path, workdir: Path, progress_hook=progress, logger=None) -> dict:
    return {
        "format": "bestaudio/best",
        "paths": {"home": str(outdir), "temp": str(workdir)},
        "ffmpeg_location": FFMPEG,
        "outtmpl": "%(uploader)s - %(title)s.%(ext)s",
        "writethumbnail": True,            # cover art, embedded by NormalizeAndExport
        "ignoreerrors": True,              # keep going if one track in a set fails
        "quiet": False,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        **({"logger": logger} if logger else {}),
    }


def default_outdir() -> Path:
    """Apple Music's watch folder if it exists, otherwise ./downloads."""
    return APPLE_MUSIC_DROP if APPLE_MUSIC_DROP.is_dir() else Path("downloads").resolve()


def expand_urls(urls, logger=None, _depth=0):
    """Resolve track / set / profile URLs into a flat list of tracks: [{url, title, artist}]."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "ffmpeg_location": FFMPEG,
            **({"logger": logger} if logger else {})}
    tracks = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for url in urls:
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as e:
                tracks.append({"url": url, "title": url, "artist": "", "error": friendly_error(str(e).split("ERROR: ")[-1])})
                continue
            if info.get("_type") == "playlist":
                for e in info.get("entries") or []:
                    if e is None:
                        continue
                    sub = e.get("url") or e.get("webpage_url")
                    if e.get("_type") in ("playlist", "url") and "/sets/" in (sub or "") and _depth < 2:
                        tracks += expand_urls([sub], logger, _depth + 1)     # a set inside a profile
                    else:
                        tracks.append({"url": sub, "title": e.get("title") or sub,
                                       "artist": e.get("uploader") or e.get("artist") or ""})
            else:
                tracks.append({"url": info.get("webpage_url") or url, "title": info.get("title") or url,
                               "artist": info.get("uploader") or info.get("artist") or ""})
    return tracks


def friendly_error(msg: str) -> str:
    """Turn yt-dlp's error text into something a person can act on."""
    msg = re.sub(r"^\[[^\]]+\]\s*[^:]*:\s*", "", msg.strip())        # drop "[soundcloud] id: "
    msg = re.sub(r"\s*\(caused by .*\)$", "", msg)
    if "404" in msg:
        return "Not found. The link may be wrong, private, or the track was removed."
    if "403" in msg or "401" in msg:
        return "SoundCloud refused access to this track (it may be private or region-locked)."
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        return "Secure connection failed (certificate problem). Try Update & restart."
    if "Unable to download webpage" in msg or "getaddrinfo" in msg or "Connection" in msg:
        return "Could not reach SoundCloud. Check your internet connection and try again."
    return msg


def _download_pct(d: dict) -> float | None:
    """0..1 fraction of the download finished, from a yt-dlp progress dict."""
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    if total and d.get("downloaded_bytes") is not None:
        return min(1.0, d["downloaded_bytes"] / total)
    if d.get("fragment_count") and d.get("fragment_index") is not None:
        return min(1.0, d["fragment_index"] / d["fragment_count"])
    return None


def run_download(urls, outdir: Path, lufs: float = -14.5, normalize: bool = True,
                 progress_hook=progress, logger=None, trim: bool = True, on_event=None) -> int:
    """Download, trim, normalize and export every track behind the URLs. Returns the number of failures.

    on_event(kind, index, **data) is called with kind in:
      "queue"  (tracks=[...])            once, after the URLs are resolved
      "start"  / "progress" (pct, stage) / "done" (file) / "failed" (error)   per track
    """
    emit = on_event or (lambda *a, **k: None)
    outdir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="scdl-"))

    tracks = expand_urls(urls, logger)
    emit("queue", None, tracks=tracks)
    failures = 0
    try:
        for i, t in enumerate(tracks):
            if t.get("error"):
                failures += 1
                emit("failed", i, error=t["error"])
                continue
            emit("start", i)
            if len(tracks) > 1:
                print(f"\n[{i + 1}/{len(tracks)}] {t['title']}")

            def hook(d, i=i):
                progress_hook(d)
                if d["status"] == "downloading":
                    f = _download_pct(d)
                    if f is not None:
                        emit("progress", i, pct=5 + 65 * f, stage="Downloading")
                elif d["status"] == "finished":
                    emit("progress", i, pct=70, stage="Downloaded")

            def stage(name, pct, i=i, **data):
                emit("progress", i, pct=pct, stage=name, **data)

            opts = build_options(outdir, workdir, hook, logger)
            opts["ignoreerrors"] = False
            opts["noplaylist"] = True
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.add_post_processor(NormalizeAndExport(lufs, normalize, trim, stage), when="post_process")
                    ydl.download([t["url"]])
                emit("done", i)
            except yt_dlp.utils.DownloadError as e:
                failures += 1
                msg = friendly_error(str(e).split("ERROR: ")[-1])
                print(f"\nFailed: {t['url']}\n  {msg}", file=sys.stderr)
                emit("failed", i, error=msg)
            except Exception as e:  # noqa: BLE001  ffmpeg or filesystem trouble
                failures += 1
                print(f"\nFailed: {t['url']}\n  {e}", file=sys.stderr)
                emit("failed", i, error=str(e))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return failures


def main() -> int:
    p = argparse.ArgumentParser(description="Download SoundCloud audio, normalize loudness, export MP3 to Apple Music.")
    p.add_argument("urls", nargs="+", help="SoundCloud track / set / profile URLs")
    p.add_argument("-o", "--outdir", default=None,
                   help="output folder (default: Apple Music's 'Automatically Add to Music' folder if present, else ./downloads)")
    p.add_argument("--lufs", type=float, default=-14.5, help="target integrated loudness in LUFS (default -14.5)")
    p.add_argument("--no-normalize", action="store_true", help="skip loudness normalization")
    p.add_argument("--no-trim", action="store_true", help="keep silence at the start/end of tracks")
    args = p.parse_args()

    bad = [u for u in args.urls if "soundcloud.com" not in u]
    if bad:
        p.error(f"not a SoundCloud URL: {', '.join(bad)}")
    if not FFMPEG:
        sys.exit("ffmpeg not found. Run:  pip3 install imageio-ffmpeg   (or: brew install ffmpeg)")

    outdir = Path(args.outdir).expanduser().resolve() if args.outdir else default_outdir()

    print(f"Saving to {outdir}")
    if outdir == APPLE_MUSIC_DROP:
        print("(Apple Music will import these automatically)")
    print()

    failures = run_download(args.urls, outdir, args.lufs, not args.no_normalize, trim=not args.no_trim)
    print(f"\nDone. Files are in {outdir}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
