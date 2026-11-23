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

    def __init__(self, target_lufs, normalize, trim=True):
        super().__init__()
        self.target = target_lufs
        self.normalize = normalize
        self.trim = trim

    def run(self, info):
        src = Path(info["filepath"])
        dst = src.with_suffix(".mp3")     # yt-dlp moves this to the final folder afterwards

        pre = ""
        if self.trim:
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
        export_mp3(src, dst, gain, tags, cover, pre)
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


def run_download(urls, outdir: Path, lufs: float = -14.5, normalize: bool = True,
                 progress_hook=progress, logger=None, trim: bool = True) -> int:
    """Download, normalize and export every URL. Returns the number of failures."""
    outdir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="scdl-"))
    failures = 0
    try:
        with yt_dlp.YoutubeDL(build_options(outdir, workdir, progress_hook, logger)) as ydl:
            ydl.add_post_processor(NormalizeAndExport(lufs, normalize, trim), when="post_process")
            for url in urls:
                try:
                    ydl.download([url])
                except yt_dlp.utils.DownloadError as e:
                    failures += 1
                    print(f"\nFailed: {url}\n  {e}", file=sys.stderr)
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
