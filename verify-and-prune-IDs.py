#!/usr/bin/env python3
"""
verify_ids.py
-------------
Clean dead asset IDs out of a grouped Countryball list using Roblox's
ASSET-DELIVERY API, which (unlike the thumbnail API) reports the real state of
an asset -- alive / archived / deleted / moderated -- and does NOT suffer the
"Pending" false-negative that made the old script keep junk and drop nothing.

INPUT FORMAT
    Lines of the form:

        123456789 — Poland

    i.e. an asset ID of 6+ digits at the start of the line, then a separator,
    then anything. The separator may be ANY of these dashes:

        -  hyphen-minus   U+002D
        –  en dash        U+2013
        —  em dash        U+2014
        ―  horizontal bar U+2015
        −  minus sign     U+2212
        ー katakana bar   U+30FC

    Surrounding spaces are optional, so `123456789—Poland` matches too.
    Anything that is not an ID line is copied to the output untouched and is
    never a candidate for removal. The 6-digit minimum means ordinary numbered
    prose ("1 — Introduction") can never be mistaken for an asset ID.
    The file extension is irrelevant -- .md, .txt, whatever.

Why asset-delivery is more accurate than thumbnails
    * thumbnail API renders on demand  -> first hit says "Pending" for GOOD
      assets, and serves a CACHED image for ARCHIVED ones (so they look fine).
    * asset-delivery serves the stored file and returns a CustomErrorCode:
        13 AssetNotFound       -> GONE       (deleted / never existed)
        22 AssetArchived       -> ARCHIVED   (also flagged by isArchived:true)
        12 / 20 moderation     -> MODERATED
        18 pending review      -> PENDING    (kept, rare for decals)
        1 / 11 / 19 permission -> RESTRICTED (kept + flagged, may need a cookie)
      A populated `location` with no blocking error -> ALIVE.

WHAT GETS REMOVED
    GONE      always
    ARCHIVED  unless --keep-archived   (archiving is reversible by the owner)
    MODERATED unless --keep-moderated
    Everything else -- PENDING / RESTRICTED / UNKNOWN -- is KEPT and printed
    under "KEPT, worth a look". A dropped connection or a rate-limit wall
    degrades to UNKNOWN, so a bad network run can never silently gut the file.

    Removal deletes the WHOLE LINE, not just the number. The input file is
    never modified; output goes to the path you pass.

USAGE
    python3 verify_ids.py countryballs.txt cleaned.txt
    python3 verify_ids.py in.txt out.txt --dry-run
    python3 verify_ids.py in.txt out.txt --keep-archived --keep-moderated
    python3 verify_ids.py in.txt out.txt --cookie "<.ROBLOSECURITY value>"
    python3 verify_ids.py in.txt out.txt --encoding cp1252
    ROBLOSECURITY=xxxx python3 verify_ids.py in.txt out.txt   # cookie via env

NOTES
    * A .ROBLOSECURITY cookie is OPTIONAL. Public decals usually resolve without
      one, but supplying it eliminates "RESTRICTED" false positives. If a large
      share come back RESTRICTED, re-run with --cookie.
    * A log of every removed line is written next to the output as
      <outfile>.removed.txt so the record survives your terminal scrollback.
    * Always sanity-check the "Found N unique asset IDs" line before letting it
      write. If N is far off, stop -- something is wrong with the input.

Standard library only.
"""

import os
import re
import sys
import json
import time
import random
import argparse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

ASSET_URL = "https://assetdelivery.roblox.com/v1/assetId/{id}"
MAX_RETRIES = 4
WORKERS = 6

# Any of the common dash characters, with optional whitespace on either side.
# 6+ digits so ordinary numbered prose ("1 - Introduction") can't be mistaken
# for an asset ID and deleted.
DASHES = "\u002d\u2013\u2014\u2015\u2212\u30fc"
ID_LINE = re.compile(r'^\s*(\d{6,})\s*[' + DASHES + r']\s*')

HEADER = re.compile(r'^(#{1,6}\s.*?)\s*\((\d+)\)\s*$')   # trailing (N) on a header

ALIVE, GONE, ARCHIVED, MODERATED, PENDING, RESTRICTED, UNKNOWN = (
    "ALIVE", "GONE", "ARCHIVED", "MODERATED", "PENDING", "RESTRICTED", "UNKNOWN")

# CustomErrorCode -> category
CODE = {
    13: GONE, 14: GONE,
    22: ARCHIVED,
    12: MODERATED, 20: MODERATED,
    18: PENDING, 21: PENDING,
    1: RESTRICTED, 2: RESTRICTED, 11: RESTRICTED, 19: RESTRICTED, 23: RESTRICTED,
}


def decide(status, data):
    """Map an asset-delivery response to one of our categories."""
    codes, is_archived, location = set(), False, None
    if isinstance(data, dict):
        is_archived = bool(data.get("isArchived"))
        location = data.get("location")
        for e in (data.get("errors") or []):
            c = e.get("CustomErrorCode")
            if c is not None:
                codes.add(c)

    # definitive error codes win
    for c in codes:
        cat = CODE.get(c)
        if cat in (GONE, ARCHIVED, MODERATED):
            return cat
    if is_archived:
        return ARCHIVED
    for c in codes:
        cat = CODE.get(c)
        if cat in (PENDING, RESTRICTED):
            return cat

    # fall back to HTTP status
    if status == 404:
        return GONE
    if status in (409, 410):
        return GONE
    if status == 403:
        return RESTRICTED          # ambiguous (mod vs permission) -> keep+flag
    if location:
        return ALIVE
    if status == 200:
        return ALIVE if location else UNKNOWN
    return UNKNOWN


def fetch(_id, cookie):
    url = ASSET_URL.format(id=_id)
    headers = {"User-Agent": "asset-verify/1.0", "Accept": "application/json"}
    if cookie:
        headers["Cookie"] = ".ROBLOSECURITY=" + cookie
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {"location": raw[:1] and "binary"}  # non-JSON body = served
                return decide(resp.status, data)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(8, 0.7 * 2 ** attempt) + random.random())
                continue
            try:
                data = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                data = None
            return decide(e.code, data)
        except Exception:
            time.sleep(0.5 * attempt + random.random() * 0.3)
    return UNKNOWN


def check_all(ids, cookie, workers):
    status = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, i, cookie): i for i in ids}
        for fut in as_completed(futs):
            _id = futs[fut]
            status[_id] = fut.result()
            done += 1
            if done % 25 == 0 or done == len(ids):
                print(f"  checked {done}/{len(ids)}")

    # second pass on PENDING (decals rarely stay pending; resolve transient ones)
    pend = [i for i, s in status.items() if s == PENDING]
    if pend:
        print(f"Re-checking {len(pend)} pending ...")
        time.sleep(2)
        for i in pend:
            status[i] = fetch(i, cookie)
    return status


def survivors(lines, header_idx, removed):
    n = 0
    for j in range(header_idx + 1, len(lines)):
        if lines[j].startswith("#"):
            break
        if ID_LINE.match(lines[j]) and j not in removed:
            n += 1
    return n


def read_lines(path, encoding):
    """Read the input, failing loudly and usefully on an encoding mismatch."""
    try:
        with open(path, encoding=encoding) as fh:
            return fh.read().splitlines()
    except UnicodeDecodeError as e:
        sys.exit(
            f"Could not decode {path} as {encoding}: {e}\n"
            "  Most lists are plain UTF-8 -- try the default first (drop the\n"
            "  --encoding flag entirely). Otherwise:\n"
            "        --encoding cp1252    (Windows Notepad / Excel exports)\n"
            "        --encoding utf-16    (Notepad 'Unicode' save)\n"
            "        --encoding latin-1   (last resort, never fails)"
        )


def main():
    ap = argparse.ArgumentParser(
        description="Verify + clean Roblox asset IDs in a list of '<id> - <name>' lines")
    ap.add_argument("infile")
    ap.add_argument("outfile", help="output path you choose")
    ap.add_argument("--cookie", default=os.environ.get("ROBLOSECURITY", ""),
                    help=".ROBLOSECURITY cookie value (optional, improves accuracy)")
    ap.add_argument("--keep-moderated", action="store_true",
                    help="keep moderated assets (default removes them)")
    ap.add_argument("--keep-archived", action="store_true",
                    help="keep archived assets -- archiving is reversible by the "
                         "owner, so these are recoverable (default removes them)")
    ap.add_argument("--dry-run", action="store_true",
                    help="check everything and report, but write no files")
    ap.add_argument("--encoding", default="utf-8-sig",
                    help="input encoding (default utf-8-sig: plain UTF-8, but "
                         "also strips a leading BOM if present)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args()

    workers = max(1, args.workers)
    lines = read_lines(args.infile, args.encoding)

    # --- collect IDs -------------------------------------------------------
    ids, seen = [], set()
    for ln in lines:
        m = ID_LINE.match(ln)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))

    # Fail fast: without this, a bad encoding yields 0 IDs and the script writes
    # a byte-identical copy that looks like a successful run.
    if not ids:
        sys.exit(
            "No IDs matched the expected format '<6+ digits><dash><name>'.\n"
            "  Every common dash is accepted, so this is almost certainly an\n"
            "  encoding problem, or the numbers are not at the start of the line.\n"
            "  Try --encoding cp1252 / utf-16 / latin-1, and check the file for a\n"
            "  bullet or quote character in front of the IDs."
        )

    print(f"Found {len(ids)} unique asset IDs\n")

    # --- verify ------------------------------------------------------------
    status = check_all(ids, args.cookie, workers)

    remove = {GONE}
    if not args.keep_archived:
        remove.add(ARCHIVED)
    if not args.keep_moderated:
        remove.add(MODERATED)

    removed_idx, removed_log, flagged_log = set(), [], []
    for idx, ln in enumerate(lines):
        m = ID_LINE.match(ln)
        if not m:
            continue
        st = status.get(m.group(1), UNKNOWN)
        if st in remove:
            removed_idx.add(idx)
            removed_log.append((st, ln.strip()))
        elif st != ALIVE:
            flagged_log.append((st, ln.strip()))

    # --- write -------------------------------------------------------------
    out = []
    for idx, ln in enumerate(lines):
        if idx in removed_idx:
            continue
        if ln.startswith("#"):
            hm = HEADER.match(ln)
            if hm:
                ln = f"{hm.group(1)} ({survivors(lines, idx, removed_idx)})"
        out.append(ln)

    log_path = args.outfile + ".removed.txt"
    if args.dry_run:
        print("\n(dry run -- no files written)")
    else:
        with open(args.outfile, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        if removed_log:
            with open(log_path, "w", encoding="utf-8") as fh:
                for st, txt in removed_log:
                    fh.write(f"[{st}] {txt}\n")

    # --- summary -----------------------------------------------------------
    tally = {}
    for s in status.values():
        tally[s] = tally.get(s, 0) + 1
    print("\n================ SUMMARY ================")
    for k in (ALIVE, GONE, ARCHIVED, MODERATED, PENDING, RESTRICTED, UNKNOWN):
        if tally.get(k):
            kept = ""
            if k == ARCHIVED and args.keep_archived:
                kept = "  (kept)"
            elif k == MODERATED and args.keep_moderated:
                kept = "  (kept)"
            print(f"  {k:11} {tally[k]}{kept}")
    print(f"\n  lines removed : {len(removed_idx)}")
    if args.dry_run:
        print("  written to    : nothing (dry run)")
    else:
        print(f"  written to    : {args.outfile}")
        if removed_log:
            print(f"  removal log   : {log_path}")

    if tally.get(RESTRICTED, 0) > len(ids) * 0.2 and not args.cookie:
        print("\n  ! Many RESTRICTED results -- re-run with --cookie <.ROBLOSECURITY>"
              "\n    for accurate classification of those.")

    if removed_log:
        print("\n--- REMOVED ---")
        for st, txt in removed_log:
            print(f"  [{st}] {txt}")
    if flagged_log:
        print("\n--- KEPT, worth a look ---")
        for st, txt in flagged_log:
            print(f"  [{st}] {txt}")


if __name__ == "__main__":
    main()