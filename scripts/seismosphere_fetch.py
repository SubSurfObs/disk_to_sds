#!/usr/bin/env python3
"""Fetch station-day MiniSEED from Seismosphere's web extractor.

The automated front-end to miniseed_to_sds.py: logs in, pulls one 24 h request
per station per day (duration=1440 -> no chunking), saves each `.ms.zip` into an
--out-dir drop-zone, then miniseed_to_sds.py ingests -> staging -> apply -> LT.

POLITE BY DESIGN (Seismosphere is an under-resourced, shared system): sequential
requests only, --sleep seconds between them, re-login once on a session-expiry
(HTML-instead-of-zip), skip days already downloaded, stop being greedy on errors.

CREDENTIALS: never on argv, never in the URL, never committed. Read from a file
(default ~/.config/seismo/credentials, chmod 600):
    SEISMO_USER=public
    SEISMO_PASS=<password>
The password only ever travels in the HTTPS POST body.

Usage:
  seismosphere_fetch.py --sta ARKL2 --out-dir <dir> --start 2026-09-21 --end 2026-10-15
  seismosphere_fetch.py --sta ARKL2 --out-dir <dir> --days 30        # last N days to yesterday
Then: miniseed_to_sds.py <out-dir> --sds-out <staging> --select 'DU.ARKL2.00.EHZ'
"""
from __future__ import annotations

import argparse
import io
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://seismosphere.net/eqserver"


def load_creds(path: Path):
    user = pw = None
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("SEISMO_USER="):
                user = line.split("=", 1)[1].strip()
            elif line.startswith("SEISMO_PASS="):
                pw = line.split("=", 1)[1].strip()
    except FileNotFoundError:
        sys.exit(f"ERROR: creds file not found: {path}\n"
                 f"  create it (chmod 600) with:\n    SEISMO_USER=public\n    SEISMO_PASS=<password>")
    if not user or not pw or pw.startswith("<"):
        sys.exit(f"ERROR: {path} must set SEISMO_USER and a real SEISMO_PASS")
    return user, pw


def login(s: requests.Session, user: str, pw: str):
    r = s.post(f"{BASE}/login.jsp", data={"username": user, "password": pw, "rememberMe": "on"},
               timeout=30)
    r.raise_for_status()


def zip_has_mseed(content: bytes) -> bool:
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        if z.testzip() is not None:
            return False
        return any(n.lower().endswith((".ms", ".mseed", ".seed")) and z.getinfo(n).file_size > 0
                   for n in z.namelist())
    except zipfile.BadZipFile:
        return False


def fetch_day(s, user, pw, sta, d: date, out_dir: Path, verticals: bool, timeout: int) -> str:
    yyyymmdd = d.strftime("%Y%m%d")
    dest = out_dir / f"{sta}_{yyyymmdd}.ms.zip"
    if dest.exists() and dest.stat().st_size > 0:
        return "have"
    params = {"date": yyyymmdd, "time": "0000", "duration": 1440,
              "sitechoice": "sitelist", "sitelist": sta, "fileformat": "mszip",
              "conttrig": 0, "servernum": 0, "getwave": "Get Waveform"}
    if verticals:
        params["verticals"] = "true"
    for attempt in (1, 2):
        r = s.get(f"{BASE}/eqwaveextractor", params=params, timeout=timeout)
        if "html" in r.headers.get("content-type", "").lower():
            if attempt == 1:
                login(s, user, pw)
                continue
            return "html"                      # still login page after re-login
        r.raise_for_status()
        if not zip_has_mseed(r.content):
            return "empty"                     # beyond retention / no data that day
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(r.content)
        tmp.replace(dest)
        return f"ok {len(r.content)/1e6:.1f}MB"
    return "fail"


def main(argv):
    p = argparse.ArgumentParser(description="Polite Seismosphere station-day MiniSEED fetcher")
    p.add_argument("--sta", required=True, help="station code, e.g. ARKL2")
    p.add_argument("--out-dir", required=True, help="drop-zone dir for .ms.zip files")
    p.add_argument("--creds", default=str(Path.home() / ".config/seismo/credentials"))
    p.add_argument("--start", help="first UTC day YYYY-MM-DD")
    p.add_argument("--end", help="last UTC day YYYY-MM-DD (inclusive); default yesterday")
    p.add_argument("--days", type=int, help="instead of --start: last N days up to --end")
    p.add_argument("--verticals", action="store_true", help="vertical components only")
    p.add_argument("--sleep", type=float, default=20.0, help="seconds between requests (politeness)")
    p.add_argument("--timeout", type=int, default=900, help="per-request timeout (s)")
    args = p.parse_args(argv[1:])

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    end = date.fromisoformat(args.end) if args.end else today - timedelta(days=1)
    if args.start:
        start = date.fromisoformat(args.start)
    elif args.days:
        start = end - timedelta(days=args.days - 1)
    else:
        sys.exit("give --start or --days")
    if start > end:
        sys.exit("start after end")

    user, pw = load_creds(Path(args.creds))
    s = requests.Session()
    login(s, user, pw)

    print(f"Seismosphere fetch: {args.sta}  {start} -> {end}  ({(end-start).days+1} days)  "
          f"duration=1440  verticals={args.verticals}  sleep={args.sleep}s")
    print(f"out: {out_dir}")
    counts = {}
    d = start
    while d <= end:
        status = fetch_day(s, user, pw, args.sta, d, out_dir, args.verticals, args.timeout)
        key = status.split()[0]
        counts[key] = counts.get(key, 0) + 1
        print(f"  {d}  {args.sta}: {status}", flush=True)
        if status.startswith("ok"):          # only pause after a real transfer
            time.sleep(args.sleep)
        d += timedelta(days=1)
    print(f"\nsummary: {counts}")
    print(f"files in {out_dir}: {len(list(out_dir.glob(f'{args.sta}_*.ms.zip')))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
