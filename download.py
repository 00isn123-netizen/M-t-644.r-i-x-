"""
download.py — Unified download router for the Matrix AV1 Encoder.

URL routing:
  tg_file: / t.me/   →  tg_handler.py  (Pyrogram bot download)
  magnet:             →  blocked (exit 1)
  iwara.tv / iwara.ai →  iwara.py       (Iwara API, Source quality)
  anibd.app           →  anibd.py       (HLS segment pipeline)
  *.m3u8 / platforms  →  yt-dlp + aria2c
  everything else     →  aria2c direct  (curl pre-resolves redirects)

Outputs:
  source.mkv      — downloaded file
  tg_fname.txt    — human-readable filename for rename step

CLI usage (local pipeline — called by main.py):
  python3 download.py <url> [--output source.mkv] [--episode N] [--season N]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

# ─── ENV (GitHub Actions) ─────────────────────────────────────────────────────
URL        = os.environ.get("VIDEO_URL", "").strip()
CUSTOM     = os.environ.get("CUSTOM",    "").strip()
BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "").strip()
CHAT_ID    = os.environ.get("TG_CHAT_ID",   "").strip()
RUN_NUMBER = os.environ.get("GITHUB_RUN_NUMBER", "?")

# ─── CDN → Referer map ────────────────────────────────────────────────────────
CDN_REFERER_MAP = {
    "uwucdn.top":           "https://kwik.cx/",
    "owocdn.top":           "https://kwik.cx/",
    "kwik.cx":              "https://kwik.cx/",
    "vdownload.hembed.com": "https://hanime1.me/",
    "hembed.com":           "https://hanime1.me/",
}

# Platforms always routed through yt-dlp
YTDLP_DOMAINS = (
    "bilibili.com",
    "nicovideo.jp",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "kwik.cx",
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _run(cmd: list, label: str = ""):
    tag = f"[{label}] " if label else ""
    print(f"{tag}▶ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"❌ {tag}command failed (exit {result.returncode})", flush=True)
        sys.exit(result.returncode)


def _resolve_filename(url: str) -> str:
    """Best-effort human-readable filename from URL."""
    try:
        script = Path(__file__).parent / "resolve_filename.py"
        if script.exists():
            out = subprocess.check_output(
                ["python3", str(script), url],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            if out:
                return out
    except Exception:
        pass
    raw = urllib.parse.urlparse(url).path.split("/")[-1]
    raw = re.sub(r"\?.*", "", raw)
    return urllib.parse.unquote(raw)


def _ensure_video_ext(name: str) -> str:
    if not re.search(r"\.(mkv|mp4|webm)$", name, re.IGNORECASE):
        return name + ".mkv"
    return name


def _write_fname(name: str):
    with open("tg_fname.txt", "w", encoding="utf-8") as f:
        f.write(name)
    print(f"📝 tg_fname.txt → {name}", flush=True)


def _detect_referer(url: str) -> tuple[str | None, str | None]:
    for cdn_domain, referer in CDN_REFERER_MAP.items():
        if cdn_domain in url:
            print(f"🔗 CDN referer: {referer}  (matched: {cdn_domain})", flush=True)
            ffmpeg_headers = (
                "-allowed_extensions ALL "
                "-extension_picky 0 "
                "-protocol_whitelist file,http,https,tcp,tls,crypto "
                f"-headers 'Referer: {referer}\\r\\nUser-Agent: Mozilla/5.0\\r\\n'"
            )
            return referer, ffmpeg_headers
    return None, None


def _notify_start(method: str, output_name: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    message = (
        "<code>"
        "┌─── 📥 [ MATRIX.DOWNLOAD.INIT ] ───┐\n"
        "│\n"
        f"│ 📂 FILE : {output_name}\n"
        f"│ ⚙️  VIA  : {method}\n"
        f"│ 🔢 RUN  : #{RUN_NUMBER}\n"
        "│\n"
        "│ 🚀 STATUS: Acquiring source...\n"
        "└────────────────────────────────────┘"
        "</code>"
    )
    subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"chat_id": CHAT_ID, "text": message,
                          "parse_mode": "HTML"}),
    ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ─── Download routes ─────────────────────────────────────────────────────────

def _download_telegram():
    """Delegate to tg_handler.py (Pyrogram)."""
    print("📡 Telegram URL → tg_handler.py", flush=True)
    _run(["python3", "tg_handler.py"], label="TG")


def _download_iwara(url: str, output: Path):
    """Import iwara.py and call its download() function (Source quality)."""
    print("🌸 Iwara URL → iwara.py", flush=True)

    # Extract video ID from URL patterns:
    #   https://www.iwara.tv/videos/<id>
    #   https://www.iwara.tv/video/<id>
    #   https://iwara.tv/videos/<id>/<slug>
    m = re.search(r'/videos?/([A-Za-z0-9]+)', url)
    if not m:
        print("❌ Could not extract Iwara video ID from URL.")
        sys.exit(1)
    video_id = m.group(1)
    print(f"🔑 Iwara video ID: {video_id}", flush=True)

    candidates = [Path(__file__).parent / "iwara.py", Path.cwd() / "iwara.py"]
    iwara_path = next((p for p in candidates if p.exists()), None)
    if not iwara_path:
        print("❌ iwara.py not found.")
        sys.exit(1)

    spec  = importlib.util.spec_from_file_location("iwara", iwara_path)
    iwara = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(iwara)

    orig_cwd = Path.cwd()
    try:
        os.chdir(output.parent.resolve())
        iwara.download(video_id=video_id, output_path=str(output.name))
    finally:
        os.chdir(orig_cwd)

    # Ensure output landed at the expected path
    written = output.parent / output.name
    if not written.exists():
        print(f"❌ Iwara download did not produce {output}")
        sys.exit(1)


def _download_anibd(url: str, output: Path, episode: int | None, season: int):
    """Import anibd.py and call its pipeline download() function."""
    print("🎌 anibd.app URL → anibd.py", flush=True)
    if episode is not None:
        os.environ["EPISODE"] = str(episode)
    os.environ["SEASON"] = str(season)

    candidates = [Path(__file__).parent / "anibd.py", Path.cwd() / "anibd.py"]
    anibd_path = next((p for p in candidates if p.exists()), None)
    if not anibd_path:
        print("❌ anibd.py not found.")
        sys.exit(1)

    spec  = importlib.util.spec_from_file_location("anibd", anibd_path)
    anibd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anibd)

    orig_cwd = Path.cwd()
    try:
        os.chdir(output.parent.resolve())
        anibd.download(url)
    finally:
        os.chdir(orig_cwd)

    written = output.parent / "source.mkv"
    if written.resolve() != output.resolve() and written.exists():
        written.rename(output)


def _download_hls_or_platform(url: str):
    """yt-dlp + aria2c for HLS streams and known platforms."""
    output_name = _ensure_video_ext(CUSTOM if CUSTOM else _resolve_filename(url))
    _write_fname(output_name)
    _notify_start("yt-dlp (HLS/platform)", output_name)

    referer, ffmpeg_headers = _detect_referer(url)

    # kwik.cx: CF-bypass proxy + aria2c
    if "kwik.cx" in url:
        ref     = referer or "https://kwik.cx/"
        proxied = f"https://universal-proxy.cloud-dl.workers.dev/?url={url}"
        print(f"🌐 kwik.cx → proxy: {proxied}", flush=True)
        _run([
            "aria2c",
            "-x", "16", "-s", "16", "-k", "1M",
            "--user-agent=Mozilla/5.0",
            "--console-log-level=warn",
            "--summary-interval=10",
            "--retry-wait=5", "--max-tries=10",
            f"--header=Referer: {ref}",
            "-o", "source.mkv",
            proxied,
        ], label="aria2c")
        return

    cmd = [
        "yt-dlp",
        "--add-header", "User-Agent:Mozilla/5.0",
        "--extractor-args", "generic:impersonate",
        "--downloader", "aria2c",
        "--downloader-args",
        "aria2c:-x 16 -s 16 -k 1M --console-log-level=warn "
        "--summary-interval=10 --retry-wait=5 --max-tries=10",
        "--merge-output-format", "mkv",
        "-o", "source.mkv",
    ]
    if referer:
        cmd += ["--referer", referer]
    if ffmpeg_headers:
        cmd += ["--downloader-args", f"ffmpeg_i:{ffmpeg_headers}"]
    cmd.append(url)
    print(f"📡 HLS/platform → yt-dlp  [{output_name}]", flush=True)
    _run(cmd, label="yt-dlp")


def _download_direct(url: str):
    """aria2c for plain CDN/direct links (curl pre-resolves redirects)."""
    output_name = _ensure_video_ext(CUSTOM if CUSTOM else _resolve_filename(url))
    _write_fname(output_name)
    _notify_start("aria2c (direct)", output_name)

    print("🔗 Resolving final URL…", flush=True)
    resolved = subprocess.check_output([
        "curl", "-s", "-o", "/dev/null", "-w", "%{url_effective}", "-L",
        "--globoff", "--user-agent", "Mozilla/5.0", url,
    ], text=True).strip()
    print(f"✅ Resolved: {resolved}", flush=True)

    referer, _ = _detect_referer(url)
    cmd = [
        "aria2c",
        "-x", "16", "-s", "16", "-k", "1M",
        "--user-agent=Mozilla/5.0",
        "--console-log-level=warn",
        "--summary-interval=10",
        "--retry-wait=5", "--max-tries=10",
        "-o", "source.mkv",
    ]
    if referer:
        cmd += [f"--header=Referer: {referer}"]
    cmd.append(resolved)
    print(f"📥 Direct → aria2c  [{output_name}]", flush=True)
    _run(cmd, label="aria2c")


# ─── Public API (for main.py local pipeline) ──────────────────────────────────

def download(
    url:     str,
    output:  Path = Path("source.mkv"),
    episode: int | None = None,
    season:  int        = 1,
) -> None:
    """
    Route *url* to the appropriate downloader.
    Writes source.mkv and tg_fname.txt in the current working directory.
    """
    url    = url.strip()
    output = Path(output)

    if url.startswith("tg_file:") or "t.me/" in url:
        _download_telegram()
        return

    if url.startswith("magnet:"):
        print("❌ Magnet links are disabled.")
        sys.exit(1)

    if "iwara.tv" in url or "iwara.ai" in url:
        _download_iwara(url, output)
        return

    if "anibd.app" in url:
        _download_anibd(url, output, episode, season)
        return

    is_hls      = "m3u8" in url
    is_platform = any(d in url for d in YTDLP_DOMAINS)
    if is_hls or is_platform:
        _download_hls_or_platform(url)
        return

    _download_direct(url)


# ─── Router (GitHub Actions: reads VIDEO_URL env var) ─────────────────────────

def _route_env():
    if not URL:
        print("❌ VIDEO_URL is empty.")
        sys.exit(1)
    download(
        url     = URL,
        episode = int(os.environ.get("EPISODE", "0") or 0) or None,
        season  = int(os.environ.get("SEASON",  "1") or 1),
    )


# ─── CLI entry point ──────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Download video from URL → source.mkv")
    p.add_argument("url",                              help="URL to download")
    p.add_argument("--output",  default="source.mkv", help="Output filename")
    p.add_argument("--episode", type=int, default=None)
    p.add_argument("--season",  type=int, default=1)
    return p.parse_args()


if __name__ == "__main__":
    if URL:
        # GitHub Actions mode — read VIDEO_URL from env
        _route_env()
    else:
        # Local CLI mode
        args = _parse_args()
        download(args.url, Path(args.output), episode=args.episode, season=args.season)
