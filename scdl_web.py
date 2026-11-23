#!/usr/bin/env python3
"""
scdl_web.py - browser interface for scdl.py.

Run it, open http://localhost:8765, paste SoundCloud links, click Download.
Tracks are normalized to -14.5 LUFS, exported as 320 kbps MP3 and saved into
Apple Music's "Automatically Add to Music" folder.

Only download audio you have the right to keep.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scdl  # noqa: E402

PORT = 8765
VERSION = os.environ.get("SCDL_VERSION", "dev")
REPO = os.environ.get("SCDL_REPO", "isaac-lax/soundcloud-downloader")
LATEST_URL = f"https://raw.githubusercontent.com/{REPO}/main/VERSION"
HERE = Path(__file__).resolve().parent


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []
        self.done: list[str] = []
        self.failed = 0
        self.outdir = scdl.default_outdir()
        self.latest = None          # newest version on GitHub, if we could check
        self.restarting = False
        threading.Thread(target=self._check_latest_loop, daemon=True).start()

    def _check_latest_loop(self):
        while True:
            try:
                with urllib.request.urlopen(LATEST_URL, timeout=8) as r:
                    self.latest = r.read().decode().strip() or None
            except Exception:  # noqa: BLE001  offline or blocked: just don't show it
                pass
            time.sleep(600)

    def restart(self):
        """Relaunch the app (which fetches the latest code) and exit this server."""
        with self.lock:
            if self.running:
                return False
            self.restarting = True

        def go():
            time.sleep(0.4)                       # let the HTTP reply go out
            boot = os.environ.get("SCDL_BOOTSTRAP")
            if boot and Path(boot).exists():
                cmd = ["bash", boot]
            elif sys.platform == "darwin":
                cmd = ["open", "-a", "SoundCloud Downloader"]
            else:
                cmd = ["bash", str(HERE / "launch.sh"), "--no-browser"]
            subprocess.Popen(cmd, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.3)
            os._exit(0)
        threading.Thread(target=go, daemon=True).start()
        return True

    # yt-dlp logger interface
    def debug(self, msg):
        if msg.startswith("[debug] "):
            return
        self.add(msg)

    def info(self, msg):
        self.add(msg)

    def warning(self, msg):
        if "ffprobe not found" in msg:     # harmless with the bundled ffmpeg
            return
        self.add("Warning: " + msg)

    def error(self, msg):
        self.failed += 1
        self.add("Error: " + msg)

    def add(self, line: str):
        line = line.strip()
        if not line:
            return
        with self.lock:
            # collapse progress updates onto one line
            if line.startswith("[download] ") and self.log and self.log[-1].startswith("[download] "):
                self.log[-1] = line
            else:
                self.log.append(line)
            if line.startswith("[MoveFiles] Moving file"):
                name = line.rsplit(" to \"", 1)[-1].rstrip("\"")
                self.done.append(Path(name).name)

    def progress(self, d):
        if d["status"] == "downloading":
            self.add(f"[download] {d.get('_percent_str', '').strip():>6}  {Path(d.get('filename', '')).name}")
        elif d["status"] == "finished":
            self.add("[download] downloaded")

    def start(self, urls, lufs, normalize, trim):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.log, self.done, self.failed = [], [], 0
        threading.Thread(target=self._run, args=(urls, lufs, normalize, trim), daemon=True).start()
        return True

    def _run(self, urls, lufs, normalize, trim):
        try:
            self.add(f"Saving to {self.outdir}")
            scdl.run_download(urls, self.outdir, lufs, normalize,
                              progress_hook=self.progress, logger=self, trim=trim)
            if self.failed:
                self.add(f"Finished with {self.failed} error(s). Nothing was added for the failed link(s).")
            elif not self.done:
                self.add("Finished, but no files were produced.")
            else:
                self.add("Done.")
        except Exception as e:  # noqa: BLE001
            self.add(f"Error: {e}")
            self.failed += 1
        finally:
            self.running = False

    def status(self):
        with self.lock:
            return {"running": self.running, "log": self.log[-200:], "done": self.done,
                    "failed": self.failed, "outdir": str(self.outdir),
                    "apple_music": self.outdir == scdl.APPLE_MUSIC_DROP,
                    "version": VERSION, "certifi": scdl.CERTIFI,
                    "latest": self.latest, "restarting": self.restarting}


job = Job()
PAGE = (Path(__file__).resolve().parent / "index.html").read_text()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence request logging
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/status":
            self._send(200, job.status())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/update":
            if job.restart():
                return self._send(200, {"ok": True})
            return self._send(409, {"error": "Wait for the current download to finish, then try again."})
        if self.path != "/download":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad request"})
        urls = [u.strip() for u in body.get("urls", "").split() if u.strip()]
        bad = [u for u in urls if "soundcloud.com" not in u]
        if not urls:
            return self._send(400, {"error": "Paste at least one SoundCloud link."})
        if bad:
            return self._send(400, {"error": "Not a SoundCloud link: " + ", ".join(bad)})
        try:
            lufs = float(body.get("lufs", -14.5))
        except (TypeError, ValueError):
            return self._send(400, {"error": "Loudness must be a number."})
        if not job.start(urls, lufs, bool(body.get("normalize", True)), bool(body.get("trim", True))):
            return self._send(409, {"error": "A download is already running."})
        self._send(200, {"ok": True})


def main():
    if not scdl.FFMPEG:
        sys.exit("ffmpeg not found. Run:  pip3 install imageio-ffmpeg")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"SoundCloud Downloader running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
