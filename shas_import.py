"""One-shot: restore historical shasradio ratings into mercuryradio.

shasradio stored ratings as (fid, uid, score) with score on the SAME asymmetric
scale mercuryradio uses (hate -4, dislike -1, shrug 0, like +1, love +2), so the
score copies over with no conversion. Match each old file to a current library
track by artist+title (shasradio files carry no album), map the old username to a
Discord user id, and upsert. Users absent from the mapping are skipped — ratings
key on Discord identity, so a shasradio user who isn't on the Discord has nowhere
to land.

    python shas_import.py --dump <mysqldump> --db <mercuryradio.db> [--commit] [--overwrite]

Dry run by default: reports track-match rates and per-user counts, writes nothing.
Fill SHAS_MAP (shasradio username -> Discord user id) before a --commit run.
Conflict policy: skip a track the user already rated, unless --overwrite.
"""

import argparse
import json
import os
import re
import unicodedata

import db

# shasradio username -> Discord user id. Real ids + names are PII and stay OUT of
# the repo (this may be open-sourced): put them in shas_map.json (gitignored)
# next to this file, as {"username": "discord_id", ...}. An unmapped user is
# reported and skipped. Example: {"shas": "212364275153371138"}.
_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shas_map.json")
SHAS_MAP = {}
if os.path.exists(_MAP_FILE):
    with open(_MAP_FILE) as f:
        SHAS_MAP = json.load(f)

# shasradio score is already mercuryradio's scale; kept explicit as a guard.
VALID_SCORES = {db.HATE, db.DISLIKE, db.SHRUG, db.LIKE, db.LOVE}


def _match_key(artist: str, title: str) -> str:
    """Album-less match key: NFC + casefold, artist and title only (shasradio
    files have no album). Mirrors db.norm_key's normalization."""
    a = unicodedata.normalize("NFC", (artist or "").strip()).casefold()
    t = unicodedata.normalize("NFC", (title or "").strip()).casefold()
    return a + "\x1f" + t


def _parse_row(body: str) -> list:
    """Split one MySQL VALUES tuple body into fields, honouring '...' strings
    (with \\' and \\\\ escapes) and NULL. Numbers come back as strings."""
    fields, i, n = [], 0, len(body)
    while i < n:
        if body[i] == "'":
            i += 1
            buf = []
            while i < n:
                c = body[i]
                if c == "\\" and i + 1 < n:
                    buf.append(body[i + 1])
                    i += 2
                    continue
                if c == "'":
                    i += 1
                    break
                buf.append(c)
                i += 1
            fields.append("".join(buf))
            while i < n and body[i] != ",":  # skip to the field separator
                i += 1
            i += 1
        else:
            j = body.find(",", i)
            if j < 0:
                j = n
            tok = body[i:j].strip()
            fields.append(None if tok == "NULL" else tok)
            i = j + 1
    return fields


def _rows(dump: str, table: str):
    """Yield each INSERT row of `table` as a field list (one row per statement,
    the way mysqldump 3.23 writes them)."""
    pat = re.compile(r"^INSERT INTO " + re.escape(table) + r" VALUES \((.*)\);\s*$")
    for line in dump.splitlines():
        m = pat.match(line)
        if m:
            yield _parse_row(m.group(1))


def load_shas(dump_text: str):
    """Return (uid->username, fid->(artist,title), list of (fid,uid,score))."""
    users = {int(r[0]): r[1] for r in _rows(dump_text, "users")}
    files = {int(r[0]): (r[2], r[7]) for r in _rows(dump_text, "files")}  # fid, artist, title
    ratings = [(int(r[0]), int(r[1]), int(r[2])) for r in _rows(dump_text, "ratings")]
    return users, files, ratings


def track_index(conn) -> dict:
    """artist+title match key -> mercuryradio track id (first wins on collision)."""
    idx = {}
    for r in conn.execute("SELECT id, artist, title FROM tracks"):
        idx.setdefault(_match_key(r["artist"], r["title"]), r["id"])
    return idx


def run(dump_text: str, conn, *, commit: bool, overwrite: bool) -> dict:
    users, files, ratings = load_shas(dump_text)
    idx = track_index(conn)
    stats = {"per_user": {}, "unmapped": {}, "no_track": 0, "applied": 0, "skipped_existing": 0}
    for fid, uid, score in ratings:
        uname = users.get(uid)
        discord_id = SHAS_MAP.get(uname)
        if discord_id is None:
            stats["unmapped"][uname] = stats["unmapped"].get(uname, 0) + 1
            continue
        if score not in VALID_SCORES:
            continue
        af = files.get(fid)
        if not af:
            stats["no_track"] += 1
            continue
        track_id = idx.get(_match_key(*af))
        if track_id is None:
            stats["no_track"] += 1
            continue
        bucket = stats["per_user"].setdefault(uname, {"matched": 0, "applied": 0, "skipped": 0})
        bucket["matched"] += 1
        if not overwrite and db.get_rating(conn, discord_id, track_id) is not None:
            bucket["skipped"] += 1
            stats["skipped_existing"] += 1
            continue
        if commit:
            db.upsert_user(conn, discord_id, uname)
            db.set_rating(conn, discord_id, track_id, score)
        bucket["applied"] += 1
        stats["applied"] += 1
    if commit:
        conn.commit()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True, help="shasradio mysqldump file")
    ap.add_argument("--db", default=None, help="mercuryradio sqlite db (default: DB_PATH)")
    ap.add_argument("--commit", action="store_true", help="write ratings (default: dry run)")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing rating")
    args = ap.parse_args()

    with open(args.dump, encoding="latin-1") as f:
        dump_text = f.read()
    conn = db.connect(args.db)
    stats = run(dump_text, conn, commit=args.commit, overwrite=args.overwrite)

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"[{mode}] applied={stats['applied']} skipped_existing={stats['skipped_existing']} "
          f"no_track_match={stats['no_track']}")
    for uname, b in sorted(stats["per_user"].items(), key=lambda kv: -kv[1]["matched"]):
        print(f"  {uname:16} matched={b['matched']:6} applied={b['applied']:6} skipped={b['skipped']:6}")
    if stats["unmapped"]:
        top = sorted(stats["unmapped"].items(), key=lambda kv: -kv[1])[:15]
        print("  UNMAPPED (add to SHAS_MAP to import): "
              + ", ".join(f"{u}({c})" for u, c in top))


if __name__ == "__main__":
    main()
