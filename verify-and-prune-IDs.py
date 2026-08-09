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

    i.e. an asset ID of 6+ digits, then a separator, then anything. Leading
    whitespace is allowed, so entries may be indented under their headings --
    the indentation is preserved verbatim in the output. The separator may be
    ANY of these dashes:

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
    ARCHIVED  unless "keep archived" is ON (archiving is reversible by the owner)
    MODERATED unless "keep moderated" is ON
    Everything else -- PENDING / RESTRICTED / UNKNOWN -- is KEPT and printed
    under "KEPT, worth a look". A dropped connection or a rate-limit wall
    degrades to UNKNOWN, so a bad network run can never silently gut the file.

    Removal deletes the WHOLE LINE, not just the number. The input file is
    never modified; output goes to the path you pass.

USAGE
    python3 verify-and-prune-IDs.py

    There are no command-line flags and no built-in filenames. On start the
    script loads the cookie, then asks for the input and output paths -- both
    are required, every run. After that you land on a menu for the remaining
    settings: keep archived, keep moderated, worker count, input encoding.
    Toggle a setting by typing its number, press enter to run, q to quit.

COOKIE
    A .ROBLOSECURITY cookie is REQUIRED, and is read from cookie.txt sitting
    next to this script. The script refuses to start without it. To create it:

        browser -> F12 -> Application / Storage -> Cookies -> roblox.com
        -> .ROBLOSECURITY -> paste the whole value into cookie.txt

    Copy the value verbatim, "DO-NOT-SHARE" banner and all. cookie.txt is
    gitignored -- never commit or share it, it is a full login to your account.

NOTES
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
import urllib.request
import urllib.error
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor, as_completed

ASSET_URL = "https://assetdelivery.roblox.com/v1/assetId/{id}"
MAX_RETRIES = 4
WORKERS = 6

# Always next to the script, so it does not matter where you launch from.
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt")

# Any of the common dash characters, with optional whitespace on either side.
# 6+ digits so ordinary numbered prose ("1 - Introduction") can't be mistaken
# for an asset ID and deleted.
DASHES = "\u002d\u2013\u2014\u2015\u2212\u30fc"
ID_LINE = re.compile(r'^\s*(\d{6,})\s*[' + DASHES + r']\s*')

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


def read_lines(path, encoding):
    """Read the input, failing loudly and usefully on an encoding mismatch."""
    try:
        with open(path, encoding=encoding) as fh:
            return fh.read().splitlines()
    except UnicodeDecodeError as e:
        sys.exit(
            f"Could not decode {path} as {encoding}: {e}\n"
            "  Most lists are plain UTF-8 -- try the default utf-8-sig first.\n"
            "  Otherwise set encoding (menu option 8) to one of:\n"
            "        cp1252     (Windows Notepad / Excel exports)\n"
            "        utf-16     (Notepad 'Unicode' save)\n"
            "        latin-1    (last resort, never fails)"
        )


def run(cfg):
    """Do the actual verify + prune pass using an already-populated config."""
    workers = max(1, cfg.workers)
    lines = read_lines(cfg.infile, cfg.encoding)

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
            "  Try encoding cp1252 / utf-16 / latin-1, and check the file for a\n"
            "  bullet or quote character in front of the IDs."
        )

    print(f"Found {len(ids)} unique asset IDs\n")

    # --- verify ------------------------------------------------------------
    status = check_all(ids, cfg.cookie, workers)

    remove = {GONE}
    if not cfg.keep_archived:
        remove.add(ARCHIVED)
    if not cfg.keep_moderated:
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
        out.append(ln)

    log_path = cfg.outfile + ".removed.txt"
    with open(cfg.outfile, "w", encoding="utf-8") as fh:
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
            if k == ARCHIVED and cfg.keep_archived:
                kept = "  (kept)"
            elif k == MODERATED and cfg.keep_moderated:
                kept = "  (kept)"
            print(f"  {k:11} {tally[k]}{kept}")
    print(f"\n  lines removed : {len(removed_idx)}")
    print(f"  written to    : {cfg.outfile}")
    if removed_log:
        print(f"  removal log   : {log_path}")

    if tally.get(RESTRICTED, 0) > len(ids) * 0.2:
        print(f"\n  ! Many RESTRICTED results -- the cookie in {COOKIE_FILE}"
              "\n    is probably stale. Grab a fresh one and re-run.")

    if removed_log:
        print("\n--- REMOVED ---")
        for st, txt in removed_log:
            print(f"  [{st}] {txt}")
    if flagged_log:
        print("\n--- KEPT, worth a look ---")
        for st, txt in flagged_log:
            print(f"  [{st}] {txt}")


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

def load_cookie():
    """Read cookie.txt next to the script, or explain how to make one and quit.

    The cookie is mandatory: running without it silently turns private and
    permission-gated assets into RESTRICTED noise, which is exactly the case
    this tool is supposed to be certain about.
    """
    try:
        with open(COOKIE_FILE, encoding="utf-8-sig") as fh:
            raw = fh.read()
    except FileNotFoundError:
        sys.exit(
            f"No cookie file at {COOKIE_FILE}\n"
            "\n"
            "  Create it before running:\n"
            "    1. log in to roblox.com in your browser\n"
            "    2. F12 -> Application / Storage -> Cookies -> roblox.com\n"
            "    3. copy the ENTIRE .ROBLOSECURITY value, warning banner and all\n"
            "    4. paste it into cookie.txt as the only contents\n"
            "\n"
            "  That value is a full login to your account -- do not share or\n"
            "  commit it. cookie.txt is gitignored."
        )
    except OSError as e:
        sys.exit(f"Could not read {COOKIE_FILE}: {e}")

    # Cookie values contain no whitespace, so this also repairs a paste that
    # got hard-wrapped across several lines.
    cookie = "".join(raw.split())
    if not cookie:
        sys.exit(
            f"{COOKIE_FILE} is empty.\n"
            "  Paste the .ROBLOSECURITY value into it, then run again."
        )
    return cookie


def count_ids(path, encoding):
    """Peek at the input so the menu can show a count before anything runs.

    Returns (count, error_message). Never raises -- a bad path or encoding
    should show up as a note in the menu, not a traceback.
    """
    try:
        with open(path, encoding=encoding) as fh:
            text = fh.read()
    except FileNotFoundError:
        return None, "file not found"
    except (UnicodeDecodeError, LookupError):
        return None, f"cannot decode as {encoding}"
    except OSError as e:
        return None, str(e)
    seen = set()
    for ln in text.splitlines():
        m = ID_LINE.match(ln)
        if m:
            seen.add(m.group(1))
    return len(seen), None


def ask(prompt_text, default=""):
    """input() that survives ctrl-c / ctrl-d and falls back to the default."""
    try:
        return input(prompt_text).strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def clean_path(raw):
    """Tidy a typed or drag-and-dropped path (quotes, ~, stray whitespace)."""
    return os.path.expanduser(raw.strip().strip('"\''))


def ask_path(prompt_text, must_exist):
    """Ask for a path until a usable one arrives. No default -- ctrl-c quits.

    Nothing here is guessed from a filename baked into the script: every run
    states its own input and output explicitly.
    """
    while True:
        try:
            path = clean_path(input(prompt_text))
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("cancelled -- nothing run.")
        if not path:
            print("  ! required. type a path, or ctrl-c to quit.")
            continue
        if must_exist and not os.path.isfile(path):
            print(f"  ! no such file: {path}")
            continue
        if not must_exist and os.path.isdir(path):
            print(f"  ! that is a directory: {path}")
            continue
        return path


def show_menu(cfg):
    n, err = count_ids(cfg.infile, cfg.encoding)
    if err:
        detail = f"  ({err})"
    else:
        detail = f"  ({n} unique IDs)"

    same = os.path.abspath(cfg.infile) == os.path.abspath(cfg.outfile)

    print("\n" + "=" * 62)
    print(" Roblox asset-ID verifier")
    print("=" * 62)
    print(f"  1) Input file      {cfg.infile}{detail}")
    print(f"  2) Output file     {cfg.outfile}"
          f"{'   ** SAME AS INPUT **' if same else ''}")
    print(f"  3) Keep archived   {'ON  (keep them)' if cfg.keep_archived else 'OFF (remove them)'}")
    print(f"  4) Keep moderated  {'ON  (keep them)' if cfg.keep_moderated else 'OFF (remove them)'}")
    print(f"  5) Workers         {cfg.workers}")
    print(f"  6) Encoding        {cfg.encoding}")
    print("-" * 62)
    print(f"     cookie          loaded, {len(cfg.cookie)} chars")
    print("  [enter] run    [q] quit")


def menu(cfg):
    while True:
        show_menu(cfg)
        choice = ask("> ").lower()

        if choice in ("q", "quit", "exit"):
            print("bye.")
            return
        if choice == "":
            n, err = count_ids(cfg.infile, cfg.encoding)
            if err:
                print(f"\n  ! cannot read {cfg.infile}: {err}")
                continue
            if not n:
                print(f"\n  ! no asset IDs found in {cfg.infile} -- check the encoding.")
                continue
            if os.path.abspath(cfg.infile) == os.path.abspath(cfg.outfile):
                warn = ask("\n  ! output is the SAME as input -- this overwrites your\n"
                           "    source list in place. type 'yes' to confirm: ")
                if warn.lower() != "yes":
                    print("  cancelled.")
                    continue
            elif os.path.exists(cfg.outfile):
                warn = ask(f"\n  ! {cfg.outfile} already exists and will be\n"
                           "    overwritten. type 'yes' to confirm: ")
                if warn.lower() != "yes":
                    print("  cancelled.")
                    continue
            print()
            run(cfg)
            ask("\n[enter] back to menu ")
            continue

        if choice == "1":
            cfg.infile = ask_path(f"  input file [{cfg.infile}]: ", must_exist=True)
        elif choice == "2":
            cfg.outfile = ask_path(f"  output file [{cfg.outfile}]: ", must_exist=False)
        elif choice == "3":
            cfg.keep_archived = not cfg.keep_archived
        elif choice == "4":
            cfg.keep_moderated = not cfg.keep_moderated
        elif choice == "5":
            raw = ask(f"  workers [{cfg.workers}]: ", str(cfg.workers))
            if raw.isdigit() and int(raw) > 0:
                cfg.workers = int(raw)
            else:
                print("  ! workers must be a positive whole number -- unchanged.")
        elif choice == "6":
            cfg.encoding = ask(f"  encoding [{cfg.encoding}]  "
                               f"(utf-8-sig / cp1252 / utf-16 / latin-1): ", cfg.encoding)
        else:
            print("  ! pick 1-6, enter to run, or q to quit.")


def main():
    if not sys.stdin.isatty():
        sys.exit("This script is interactive -- run it from a terminal.")

    cookie = load_cookie()

    print("\n Roblox asset-ID verifier")
    print(f"   cookie: {COOKIE_FILE} ({len(cookie)} chars)")
    print("   paths are asked for every run -- nothing is remembered.\n")
    infile = ask_path("  input file  (the list to check): ", must_exist=True)
    outfile = ask_path("  output file (the cleaned copy): ", must_exist=False)

    cfg = SimpleNamespace(
        infile=infile,
        outfile=outfile,
        cookie=cookie,
        keep_archived=False,
        keep_moderated=False,
        workers=WORKERS,
        encoding="utf-8-sig",
    )
    menu(cfg)


if __name__ == "__main__":
    main()