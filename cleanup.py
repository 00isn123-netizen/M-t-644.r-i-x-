#!/usr/bin/env python3
"""
cleanup.py — Remove intermediate files after a successful pipeline run.

Two modes
─────────
Local mode  (default):
    Deletes part_*.mkv, encoded-parts/, .tmp_* dirs, .concat_list.txt,
    /tmp/.prog_*.txt, and source.mkv from the working directory.

GitHub Actions mode  (--github):
    Calls the GitHub REST API to delete all artifacts EXCEPT 'final-result',
    mirroring the cleanup job in the original workflow.

CLI usage:
    python3 cleanup.py [--work-dir .] [--keep-source] [--dry-run]
    python3 cleanup.py --github          # requires GH_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID env
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import R, B, GR, RD, CY, YL, DIM


# ─── Local cleanup ───────────────────────────────────────────────────────────
def cleanup_local(
    work_dir: Path = Path("."),
    keep_source: bool = False,
    keep_encoded: bool = False,
    dry_run: bool = False,
) -> None:
    """
    Remove intermediate files from *work_dir*.
    Set *keep_source* to preserve source.mkv.
    Set *keep_encoded* to preserve encoded-parts/.
    """
    work_dir = Path(work_dir).resolve()
    print(f"\n{B}━━━ Cleanup ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}", flush=True)
    if dry_run:
        print(f"  {YL}(dry-run mode — nothing will be deleted){R}", flush=True)

    removed = 0

    def _remove(path: Path) -> None:
        nonlocal removed
        label = str(path.relative_to(work_dir)) if path.is_relative_to(work_dir) else str(path)
        if dry_run:
            print(f"  {DIM}[dry-run] would remove: {label}{R}")
            return
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        print(f"  🗑   Removed: {label}")
        removed += 1

    # Raw chunks
    for f in sorted(work_dir.glob("part_*.mkv")):
        _remove(f)

    # ffmpeg concat list
    for f in work_dir.glob(".concat_list.txt"):
        _remove(f)

    # Temp segment dirs created by anibd.py
    for d in work_dir.glob(".tmp_*"):
        _remove(d)

    # ffmpeg progress files in /tmp
    for f in Path("/tmp").glob(".prog_*.txt"):
        _remove(f)

    # Encoded-parts directory
    enc_dir = work_dir / "encoded-parts"
    if enc_dir.exists() and not keep_encoded:
        _remove(enc_dir)

    # source.mkv
    source = work_dir / "source.mkv"
    if source.exists() and not keep_source:
        _remove(source)

    tag = "(dry-run)" if dry_run else ""
    print(f"{GR}✅  Cleanup done {tag} — {removed} item(s) removed.{R}", flush=True)


# ─── GitHub Actions artifact cleanup ─────────────────────────────────────────
def cleanup_github(
    gh_token: str,
    repo: str,
    run_id: str,
    dry_run: bool = False,
) -> None:
    """
    Delete all artifacts for *run_id* except 'final-result'.
    Mirrors the cleanup job in the original workflow.
    """
    print(f"\n{B}━━━ GitHub Artifact Cleanup ━━━━━━━━━━━━━━━━━━━━{R}", flush=True)
    if dry_run:
        print(f"  {YL}(dry-run mode — nothing will be deleted){R}", flush=True)

    API  = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts"
    HDRS = {
        "Authorization":        f"Bearer {gh_token}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def gh_request(method: str, url: str) -> dict | None:
        req = urllib.request.Request(url, method=method, headers=HDRS)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code == 204:   # No Content → success for DELETE
                return {}
            print(f"  {RD}HTTP {e.code} — {url}{R}", flush=True)
            return None
        except Exception as e:
            print(f"  {RD}Error — {e}{R}", flush=True)
            return None

    # List artifacts
    data = gh_request("GET", f"{API}?per_page=100")
    if data is None:
        print(f"{RD}❌  Could not list artifacts.{R}")
        sys.exit(1)

    artifacts = data.get("artifacts", [])
    deleted   = 0

    for art in artifacts:
        name = art.get("name", "")
        aid  = art.get("id")
        if name == "final-result":
            print(f"  {DIM}Keeping: {name}{R}")
            continue
        print(f"  🗑   Deleting artifact: {name}  (id={aid})")
        if not dry_run:
            del_url = f"https://api.github.com/repos/{repo}/actions/artifacts/{aid}"
            gh_request("DELETE", del_url)
        deleted += 1

    tag = "(dry-run)" if dry_run else ""
    print(f"{GR}✅  GitHub cleanup done {tag} — {deleted} artifact(s) removed.{R}",
          flush=True)


# ─── CLI entry point ─────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Clean up intermediate pipeline files")
    p.add_argument("--github",       action="store_true",
                   help="GitHub Actions mode: delete intermediate artifacts via API")
    p.add_argument("--work-dir",     default=".",
                   help="Working directory for local cleanup (default: .)")
    p.add_argument("--keep-source",  action="store_true",
                   help="Keep source.mkv (local mode)")
    p.add_argument("--keep-encoded", action="store_true",
                   help="Keep encoded-parts/ directory (local mode)")
    p.add_argument("--dry-run",      action="store_true",
                   help="Print what would be removed without actually removing it")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.github:
        gh_token = os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
        repo     = os.environ.get("GITHUB_REPOSITORY", "")
        run_id   = os.environ.get("GITHUB_RUN_ID", "")

        if not all([gh_token, repo, run_id]):
            print(f"{RD}GitHub mode requires: GH_TOKEN (or GITHUB_TOKEN), "
                  f"GITHUB_REPOSITORY, GITHUB_RUN_ID{R}")
            sys.exit(1)

        cleanup_github(gh_token, repo, run_id, dry_run=args.dry_run)
    else:
        cleanup_local(
            work_dir     = Path(args.work_dir),
            keep_source  = args.keep_source,
            keep_encoded = args.keep_encoded,
            dry_run      = args.dry_run,
        )
