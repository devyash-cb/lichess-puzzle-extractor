import csv
import io
import os
import random
import struct
import sys
import urllib.request
from pathlib import Path

import zstandard as zstd

URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
OUT_FILE = Path("puzzles.bin")
TEMP_FILE = Path("puzzles.bin.tmp")

# Keep this binary layout compatible with the existing reader:
# uint32 puzzle count
# repeated: uint16 rating, uint8 theme ID, uint8 FEN byte length,
#           FEN bytes, uint8 moves byte length, moves bytes.
THEMES_MAP = {
    "mateIn1": 1, "mateIn2": 2, "mateIn3": 3, "mateIn4": 4, "mate": 5,
    "fork": 6, "pin": 7, "skewer": 8, "discoveredAttack": 9, "sacrifice": 10,
    "deflection": 11, "attraction": 12, "clearance": 13, "interference": 14,
    "xRayAttack": 15, "backRankMate": 16, "smotheredMate": 17, "anastasiaMate": 18,
    "arabianMate": 19, "hookMate": 20, "pawnEndgame": 21, "rookEndgame": 22,
    "queenEndgame": 23, "knightEndgame": 24, "bishopEndgame": 25, "endgame": 26,
    "opening": 27, "middlegame": 28, "zugzwang": 29, "quietMove": 30,
}

TARGET_TIER3 = 300_000  # shorter puzzles
TARGET_TIER2 = 100_000  # medium puzzles
TARGET_TIER1 = 10_000   # longer puzzles


def get_theme_id(themes_str):
    """Return the first recognized theme ID, or 0 when none is recognized."""
    for theme in themes_str.split():
        if theme in THEMES_MAP:
            return THEMES_MAP[theme]
    return 0


def parse_puzzle(row):
    """Parse a Lichess CSV row; return None for invalid/unsupported rows."""
    # Lichess columns: PuzzleId, FEN, Moves, Rating, RatingDeviation,
    # Popularity, NbPlays, Themes, GameUrl, OpeningTags.
    if len(row) < 8:
        return None

    try:
        rating = int(row[3])
        deviation = int(row[4])
        plays = int(row[6])
    except (ValueError, TypeError):
        return None

    if not (1000 <= rating <= 1800) or deviation > 80:
        return None

    fen = row[1].strip()
    moves_str = row[2].strip()
    if not fen or not moves_str:
        return None

    fen_bytes = fen.encode("utf-8")
    moves_bytes = moves_str.encode("utf-8")

    # The existing binary format stores each string length in one byte.
    # Skip rows that cannot be represented safely rather than crashing.
    if len(fen_bytes) > 255 or len(moves_bytes) > 255:
        return None

    plies = len(moves_str.split())
    themes = row[7]
    theme_id = get_theme_id(themes)
    item = (rating, theme_id, fen, moves_str)

    if plies < 8 and plays >= 200:
        return 3, item
    if 8 <= plies < 16 and plays >= 200:
        return 2, item
    if plies >= 16 and plays >= 50:
        return 1, item
    return None


def write_binary(puzzles, destination):
    """Write the established binary format to a temporary file."""
    with destination.open("wb") as output:
        output.write(struct.pack("<I", len(puzzles)))
        for rating, theme_id, fen, moves_str in puzzles:
            fen_bytes = fen.encode("utf-8")
            moves_bytes = moves_str.encode("utf-8")
            output.write(struct.pack("<HB", rating, theme_id))
            output.write(struct.pack("B", len(fen_bytes)))
            output.write(fen_bytes)
            output.write(struct.pack("B", len(moves_bytes)))
            output.write(moves_bytes)


def main():
    tier3_pool = []
    tier2_pool = []
    tier1_pool = []

    print("Downloading the official Lichess puzzle database...", flush=True)
    request = urllib.request.Request(
        URL,
        headers={"User-Agent": "lichess-puzzle-extractor/2.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            decompressor = zstd.ZstdDecompressor()
            with decompressor.stream_reader(response) as reader:
                text_stream = io.TextIOWrapper(reader, encoding="utf-8", newline="")
                rows = csv.reader(text_stream)

                header = next(rows, None)
                if not header or len(header) < 8:
                    raise RuntimeError("Downloaded CSV has no valid Lichess puzzle header.")

                print("Streaming and filtering puzzle rows...", flush=True)
                scanned = 0
                for row in rows:
                    scanned += 1
                    parsed = parse_puzzle(row)
                    if parsed is None:
                        continue

                    tier, item = parsed
                    if tier == 3 and len(tier3_pool) < TARGET_TIER3:
                        tier3_pool.append(item)
                    elif tier == 2 and len(tier2_pool) < TARGET_TIER2:
                        tier2_pool.append(item)
                    elif tier == 1 and len(tier1_pool) < TARGET_TIER1:
                        tier1_pool.append(item)

                    if scanned % 1_000_000 == 0:
                        print(
                            f"Scanned {scanned:,} rows | "
                            f"Tier 3: {len(tier3_pool):,} | "
                            f"Tier 2: {len(tier2_pool):,} | "
                            f"Tier 1: {len(tier1_pool):,}",
                            flush=True,
                        )

                    if (
                        len(tier3_pool) >= TARGET_TIER3
                        and len(tier2_pool) >= TARGET_TIER2
                        and len(tier1_pool) >= TARGET_TIER1
                    ):
                        break

                # Flush TextIOWrapper before leaving the reader context.
                text_stream.detach()

    except Exception as exc:
        print(f"ERROR while downloading or processing the puzzle database: {exc}", file=sys.stderr)
        raise

    all_puzzles = tier3_pool + tier2_pool + tier1_pool
    if not all_puzzles:
        raise RuntimeError("No puzzles were collected; refusing to replace puzzles.bin.")

    random.shuffle(all_puzzles)
    print(
        f"Collected {len(all_puzzles):,} puzzles "
        f"(Tier 3={len(tier3_pool):,}, Tier 2={len(tier2_pool):,}, "
        f"Tier 1={len(tier1_pool):,}).",
        flush=True,
    )

    try:
        write_binary(all_puzzles, TEMP_FILE)
        if TEMP_FILE.stat().st_size < 4:
            raise RuntimeError("Generated binary file is unexpectedly small.")

        # Validate the header count before replacing the last good file.
        with TEMP_FILE.open("rb") as check:
            stored_count = struct.unpack("<I", check.read(4))[0]
        if stored_count != len(all_puzzles):
            raise RuntimeError("Binary validation failed: puzzle count mismatch.")

        os.replace(TEMP_FILE, OUT_FILE)
    finally:
        if TEMP_FILE.exists():
            TEMP_FILE.unlink()

    print(f"SUCCESS: wrote {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes).", flush=True)


if __name__ == "__main__":
    main()
