"""
iwara.py — Iwara.tv / Iwara.ai video downloader.

Importable module (used by download.py) or standalone CLI.

Module usage:
    from iwara import download
    download(video_id="laOLZIqV5BJA5W", output_path="source.mkv")

CLI usage:
    python3 iwara.py <video_id>
    python3 iwara.py laOLZIqV5BJA5W
"""

import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlparse


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _format_time(seconds: float) -> str:
    if seconds < 0 or seconds == float('inf'):
        return "??:??:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name)


def _get_remote_file_size(url: str, base_headers: dict) -> int:
    cdn_headers = {
        "User-Agent": base_headers["User-Agent"],
        "Referer":    base_headers["Referer"],
        "Origin":     base_headers["Origin"],
    }
    try:
        req = urllib.request.Request(url, headers=cdn_headers, method='HEAD')
        with urllib.request.urlopen(req, timeout=5) as resp:
            length = resp.getheader('Content-Length')
            if length:
                return int(length)
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers=cdn_headers, method='GET')
        req.add_header('Range', 'bytes=0-0')
        with urllib.request.urlopen(req, timeout=5) as resp:
            cr = resp.getheader('Content-Range')
            if cr and '/' in cr:
                return int(cr.split('/')[-1])
    except Exception:
        pass
    return 0


def _fetch_json(url: str, req_headers: dict):
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode()
        if e.code == 301:
            try:
                err_data = json.loads(error_body)
                if err_data.get("message") == "errors.differentSite":
                    site_map = {"iwara_ai": "www.iwara.ai", "iwara": "www.iwara.tv"}
                    site_id = err_data.get("siteId")
                    if site_id in site_map:
                        new_site = site_map[site_id]
                        req_headers["X-Site"]  = new_site
                        req_headers["Origin"]  = f"https://{new_site}"
                        req_headers["Referer"] = f"https://{new_site}/"
                        retry = urllib.request.Request(url, headers=req_headers)
                        with urllib.request.urlopen(retry) as r:
                            return json.loads(r.read().decode())
            except Exception:
                pass
        print(f"\n[!] HTTP Error {e.code}: {error_body}")
        sys.exit(1)


# ─── Public API ───────────────────────────────────────────────────────────────

def download(video_id: str, output_path="source.mkv") -> None:
    """
    Download an Iwara video (Source quality) and save it to *output_path*.

    Args:
        video_id:    The Iwara video ID (e.g. "laOLZIqV5BJA5W").
        output_path: Destination file. download.py passes "source.mkv".
    """
    output_path   = Path(output_path)
    site_hostname = "www.iwara.tv"
    api_url       = f"https://api.iwara.tv/video/{video_id}"

    headers = {
        "Accept":       "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        "Origin":  f"https://{site_hostname}",
        "Referer": f"https://{site_hostname}/",
        "X-Site":  site_hostname,
    }

    # ── 1. Fetch Metadata ──────────────────────────────────────────────────
    print("[*] Initializing connection to Iwara API...", flush=True)
    data      = _fetch_json(api_url, headers)
    file_url  = data.get("fileUrl")
    raw_title = data.get("title", "Unknown_Video")

    if not file_url:
        print("\n[!] Error: 'fileUrl' missing. Video may be private or region-locked.")
        sys.exit(1)

    if file_url.startswith("//"):
        file_url = "https:" + file_url

    # ── 2. Generate X-Version hash ─────────────────────────────────────────
    parsed_url   = urlparse(file_url)
    last_segment = parsed_url.path.strip('/').split('/')[-1]
    expires      = parse_qs(parsed_url.query).get("expires", [""])[0]
    secret       = "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
    x_version    = hashlib.sha1(
        f"{last_segment}_{expires}_{secret}".encode('utf-8')
    ).hexdigest()

    # ── 3. Fetch quality list ──────────────────────────────────────────────
    dl_name      = quote(f"Iwara - {raw_title} [{video_id}].mp4")
    final_url    = f"{file_url}&download={dl_name}"
    file_headers = {**headers, "X-Version": x_version}

    req = urllib.request.Request(final_url, headers=file_headers)
    with urllib.request.urlopen(req) as resp:
        files_data = json.loads(resp.read().decode())

    # ── 4. Always pick Source quality (highest) ────────────────────────────
    available = []
    for src in files_data:
        q   = src.get('name', src.get('resolution', 'Unknown'))
        url = src.get('src', {}).get('download') or src.get('src', {}).get('view')
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        available.append((q, url))

    if not available:
        print("[!] No valid download links found.")
        sys.exit(1)

    # Prefer "Source"; fall back to first entry (usually highest quality)
    target = next((item for item in available if item[0] == "Source"), available[0])
    quality_label, download_url = target
    display_name = _sanitize_filename(f"[{quality_label}] {raw_title} [{video_id}].mp4")

    print(
        f"[*] Quality : {quality_label}\n"
        f"[*] Title   : {raw_title}\n"
        f"[*] Output  : {output_path}",
        flush=True,
    )

    # ── 5. File size probe ─────────────────────────────────────────────────
    total_size = _get_remote_file_size(download_url, headers)

    # ── 6. TUI Download ────────────────────────────────────────────────────
    print("\n" * 12, flush=True)

    dl_req = urllib.request.Request(
        download_url,
        headers={"User-Agent": headers["User-Agent"], "Referer": headers["Referer"]},
    )

    try:
        with urllib.request.urlopen(dl_req) as resp, open(output_path, 'wb') as out_file:
            downloaded     = 0
            chunk_size     = 8192
            start_time     = time.time()
            last_ui_update = 0

            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)

                now = time.time()
                if now - last_ui_update > 0.1:
                    elapsed  = now - start_time
                    speed    = downloaded / elapsed if elapsed > 0 else 0
                    percent  = (downloaded / total_size * 100) if total_size > 0 else 0
                    eta      = ((total_size - downloaded) / speed) if (speed > 0 and total_size > 0) else float('inf')
                    speed_mb = speed / (1024 * 1024)
                    done_mb  = downloaded / (1024 * 1024)
                    tot_mb   = total_size / (1024 * 1024) if total_size > 0 else 0
                    filled   = int(15 * percent / 100) if total_size > 0 else 0
                    bar      = '▰' * filled + '▱' * (15 - filled)

                    sys.stdout.write(
                        f"\033[14A\r┌─── 🛰️ [ IWARA.DOWNLOAD.PROCESS ] ───┐\n"
                        f"│                                    \n"
                        f"│ 📂 FILE: {display_name[:55].ljust(55)}\n"
                        f"│ ⚡ SPEED: {speed_mb:.2f} MB/s\n"
                        f"│ ⏳ TIME: {_format_time(elapsed)} / ETA: {_format_time(eta)}\n"
                        f"│ 🕒 DONE: {done_mb:.2f} MB / "
                        f"{'%.2f MB' % tot_mb if total_size > 0 else 'Unknown'}\n"
                        f"│                                    \n"
                        f"│ 📊 PROG: [{bar}] {percent:.1f}%\n"
                        f"│                                    \n"
                        f"│ 🛠️ SETTINGS: HTTPS | Chunk: 8KB | IPv4\n"
                        f"│ 🎞️ VIDEO: Iwara ({quality_label}) | H.264 | MP4\n"
                        f"│ 🔊 AUDIO: AAC @ 128k\n"
                        f"│ 📦 SIZE: {done_mb:.2f} MB → ~{tot_mb:.2f} MB est\n"
                        f"│                                    \n"
                        f"└────────────────────────────────────┘"
                    )
                    sys.stdout.flush()
                    last_ui_update = now

            # Final 100% render
            elapsed  = time.time() - start_time
            speed_mb = (downloaded / elapsed) / (1024 * 1024) if elapsed > 0 else 0
            done_mb  = downloaded / (1024 * 1024)
            sys.stdout.write(
                f"\033[14A\r┌─── 🛰️ [ IWARA.DOWNLOAD.PROCESS ] ───┐\n"
                f"│                                    \n"
                f"│ 📂 FILE: {display_name[:55].ljust(55)}\n"
                f"│ ⚡ SPEED: {speed_mb:.2f} MB/s (Average)\n"
                f"│ ⏳ TIME: {_format_time(elapsed)} / ETA: 00:00:00\n"
                f"│ 🕒 DONE: {done_mb:.2f} MB / {done_mb:.2f} MB\n"
                f"│                                    \n"
                f"│ 📊 PROG: [{'▰' * 15}] 100.0%\n"
                f"│                                    \n"
                f"│ 🛠️ SETTINGS: HTTPS | Chunk: 8KB | IPv4\n"
                f"│ 🎞️ VIDEO: Iwara ({quality_label}) | H.264 | MP4\n"
                f"│ 🔊 AUDIO: AAC @ 128k\n"
                f"│ 📦 SIZE: {done_mb:.2f} MB → ~{done_mb:.2f} MB est\n"
                f"│                                    \n"
                f"└────────────────────────────────────┘\n\n"
                f"[✓] Download Complete! → {output_path}\n"
            )
            sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n\n[!] Download Cancelled by User.")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n[!] Download Failed: {e}")
        sys.exit(1)


# ─── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 iwara.py <video_id>")
        print("Example: python3 iwara.py laOLZIqV5BJA5W")
        sys.exit(1)
    download(video_id=sys.argv[1])
