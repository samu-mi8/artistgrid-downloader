#!/usr/bin/env python3
"""Download the most popular unreleased songs from an ArtistGrid tracker tab.

Example:
    python artistgrid_dl.py "https://artistgrid.cx/sh/<ID>/?artist=Lil%20Uzi%20Vert" --limit 10

Standard library only.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TRACKER_API = "https://trackerapi.artistgrid.cx"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# No play counts exist, so popularity is guessed from the emoji markers (lower = better)
MARKER_RANK = {"🏆": 0, "⭐": 1, "✨": 2, "🥉": 3}
WORST_MARKER = "🗑"
FULL_LENGTHS = {"full", "og file", "tagged"}


def log(msg=""):
    print(msg, flush=True)


def http(url, *, data=None, headers=None, method=None, timeout=30):
    h = {"User-Agent": UA}
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout)


def get_json(url):
    with http(url) as r:
        return json.load(r)


def parse_source(src):
    """Accept an ArtistGrid URL or a bare sheet ID. Returns (sheet_id, artist or None)."""
    if re.fullmatch(r"[\w-]{20,}", src):
        return src, None
    u = urllib.parse.urlparse(src)
    m = re.search(r"/sh/([\w-]+)", u.path)
    if not m:
        sys.exit("Unrecognized URL, expected something like https://artistgrid.cx/sh/<ID>/?artist=Name")
    artist = urllib.parse.parse_qs(u.query).get("artist", [None])[0]
    return m.group(1), artist


def safe_name(s, maxlen=120):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return s[:maxlen].rstrip(" .") or "untitled"


def clean_title(title):
    # drop the leading emoji marker and trailing asterisks
    t = re.sub(r"^[^\w\[\(]+", "", title).strip()
    return t.rstrip("*").strip() or title


def marker_of(title):
    for m in (*MARKER_RANK, WORST_MARKER):
        if title.startswith(m):
            return m
    return None


def host_link(links):
    """Pick usable download links, pillows.su first."""
    found = {}
    for l in links:
        u = urllib.parse.urlparse(l.get("url", ""))
        mid = re.match(r"^/f/([\w-]+)", u.path)
        host = u.netloc.lower().removeprefix("www.")
        if mid and host in ("pillows.su", "imgur.gg"):
            found.setdefault(host, mid.group(1))
    return [(h, found[h]) for h in ("pillows.su", "imgur.gg") if h in found]


def collect_tracks(data, args):
    tracks = []
    for era in data["eras"]:
        if args.era and args.era.lower() not in era["name"].lower():
            continue
        for t in era["tracks"]:
            title_raw = t["name"]["title"]
            mk = marker_of(title_raw)
            if mk == WORST_MARKER and not args.include_worst:
                continue
            if args.marker and mk not in args.marker:
                continue
            if args.full_only and (t.get("available_length") or "").lower() not in FULL_LENGTHS:
                continue
            links = host_link(t.get("links") or [])
            if not links:
                continue
            tracks.append({
                "era": era["name"],
                "title": clean_title(title_raw),
                "marker": mk,
                "length": t.get("track_length"),
                "quality": t.get("quality"),
                "availability": t.get("available_length"),
                "links": links,
                "og_filename": t.get("og_filename"),
            })
    # stable sort, so sheet order is kept within the same marker
    tracks.sort(key=lambda x: MARKER_RANK.get(x["marker"], 9))
    return tracks


def resolve(host, fid):
    """Turn a host + file id into a direct download URL."""
    if host == "pillows.su":
        return f"https://api.pillows.su/api/download/{fid}"
    with http(f"https://imgur.gg/api/file/{fid}/download", data=b"{}", method="POST",
              headers={"Content-Type": "application/json"}) as r:
        return json.load(r)["url"]


def filename_from_response(r):
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    return urllib.parse.unquote(m.group(1)) if m else None


def download_track(track, dest_dir, index, retries):
    last_err = None
    for host, fid in track["links"]:
        for attempt in range(1, retries + 1):
            try:
                with http(resolve(host, fid), timeout=60) as r:
                    server_name = filename_from_response(r) or track.get("og_filename") or ""
                    ext = Path(server_name).suffix or ".mp3"
                    out = dest_dir / f"{index:02d} - {safe_name(track['title'])}{ext}"
                    if out.exists():
                        return out, "already exists"
                    total = int(r.headers.get("Content-Length") or 0)
                    part = out.with_name(out.name + ".part")
                    done = 0
                    with open(part, "wb") as f:
                        while chunk := r.read(1 << 16):
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                print(f"\r    {done / 1e6:6.1f}/{total / 1e6:.1f} MB", end="", flush=True)
                    print("\r" + " " * 40 + "\r", end="")
                    if total and done != total:
                        part.unlink(missing_ok=True)
                        raise IOError(f"incomplete download ({done}/{total} bytes)")
                    part.replace(out)
                    return out, f"ok from {host}"
            except (urllib.error.URLError, IOError, TimeoutError, KeyError, json.JSONDecodeError) as e:
                last_err = f"{host}: {e}"
                time.sleep(1.5 * attempt)
    return None, f"failed ({last_err})"


def main():
    p = argparse.ArgumentParser(description="Download popular unreleased songs from an ArtistGrid tab.")
    p.add_argument("source", help="ArtistGrid page URL (or just the sheet ID)")
    p.add_argument("-t", "--tab", default="best",
                   help="tracker tab slug: best, main (Unreleased), grails, special, recent... (default: best)")
    p.add_argument("-n", "--limit", type=int, default=10, help="how many songs to download (default 10, 0 = all)")
    p.add_argument("-o", "--out", help="output folder (default: downloads/<artist>/<tab>)")
    p.add_argument("--marker", nargs="+", choices=list(MARKER_RANK),
                   help="only keep songs with these emoji markers, e.g. --marker ⭐ 🏆")
    p.add_argument("--era", help="filter by era name (substring match)")
    p.add_argument("--full-only", action="store_true", help="skip snippets and partials")
    p.add_argument("--include-worst", action="store_true", help="also include 'worst of' songs")
    p.add_argument("--list-tabs", action="store_true", help="list the available tabs and exit")
    p.add_argument("--dry-run", action="store_true", help="show what would be downloaded and exit")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--delay", type=float, default=1.0, help="pause between downloads in seconds")
    args = p.parse_args()

    sheet_id, artist = parse_source(args.source)

    if args.list_tabs:
        meta = get_json(f"{TRACKER_API}/sh/{sheet_id}/meta")
        log(f"{meta.get('name')}  (updated {meta.get('lastUpdated')})")
        for slug, info in meta["tabs"].items():
            log(f"  {slug:14} {info['count']:5} rows")
        return

    try:
        data = get_json(f"{TRACKER_API}/sh/{sheet_id}/tab/{urllib.parse.quote(args.tab)}")
    except urllib.error.HTTPError as e:
        sys.exit(f"Tab '{args.tab}' not found ({e.code}). Use --list-tabs to see the available ones.")

    artist = artist or re.sub(r"\s*tracker\s*$", "", re.sub(r"^updated\s+", "", data.get("name", "artist"), flags=re.I), flags=re.I)
    tracks = collect_tracks(data, args)
    if args.limit:
        tracks = tracks[: args.limit]

    log(f"{artist} - tab '{data['tab']['name']}': {len(tracks)} songs selected")
    if not tracks:
        return
    for i, t in enumerate(tracks, 1):
        log(f"  {i:2}. {t['marker'] or ' '} {t['title']}  [{t['era']} · {t['length']} · {t['quality']} · {t['availability']}]")
    if args.dry_run:
        return

    dest = Path(args.out) if args.out else Path("downloads") / safe_name(artist) / safe_name(args.tab)
    dest.mkdir(parents=True, exist_ok=True)
    log(f"\nDownloading to: {dest.resolve()}\n")

    manifest, failed = [], 0
    for i, t in enumerate(tracks, 1):
        log(f"[{i}/{len(tracks)}] {t['title']}")
        path, status = download_track(t, dest, i, args.retries)
        log(f"    {status}")
        if path is None:
            failed += 1
        manifest.append({**{k: t[k] for k in ("title", "era", "quality", "availability", "length")},
                         "file": path.name if path else None, "status": status})
        time.sleep(args.delay)

    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\nDone: {len(tracks) - failed} ok, {failed} failed.")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
