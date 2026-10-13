import csv
import io
import os
import random
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path
import zstandard as zstd

URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
OUT_DIR = Path("puzzles")
ZIP_FILE = Path("puzzles.zip")

# PRIORITY HIERARCHY: Rarest & most specific tactics are captured first.
THEME_PRIORITY = [
    "enPassant", "castling", "smotheredMate", "bodenMate", "arabianMate", "anastasiaMate", "hookMate", 
    "underPromotion", "doubleCheck", "zugzwang", "intermezzo", "interference", "xRayAttack", 
    "clearance", "defensiveMove", "quietMove", "attraction", "deflection", 
    "sacrifice", "discoveredAttack", "trappedPiece", "skewer", "pin", "fork", "hangingPiece", "exposedKing",
    "mateIn5", "mateIn4", "mateIn3", "mateIn2", "mateIn1", "mate",
    "pawnEndgame", "rookEndgame", "knightEndgame", "bishopEndgame", "queenEndgame", "endgame",
    "opening", "middlegame",
    "advantage", "crushing", "short", "long", "master", "masterVsMaster", "superGM"
]

def get_best_theme(themes_str):
    tags = themes_str.split()
    for theme in THEME_PRIORITY:
        if theme in tags:
            return theme
    return tags[0] if tags else "unclassified"

def write_binary(puzzles, filepath):
    # Binary Structure (Theme ID Dropped):
    # [Total Puzzles (uint32)]
    # Loop: [Rating (uint16)] [FEN Length (uint8)] [FEN Bytes] [Moves Length (uint8)] [Moves Bytes]
    with filepath.open("wb") as output:
        output.write(struct.pack("<I", len(puzzles)))
        for rating, fen, moves_str in puzzles:
            fen_bytes = fen.encode("utf-8")
            moves_bytes = moves_str.encode("utf-8")
            output.write(struct.pack("<H", rating))
            output.write(struct.pack("B", len(fen_bytes)))
            output.write(fen_bytes)
            output.write(struct.pack("B", len(moves_bytes)))
            output.write(moves_bytes)

def main():
    print("Downloading the official Lichess puzzle database...", flush=True)
    request = urllib.request.Request(URL, headers={"User-Agent": "lichess-puzzle-extractor/3.0"})

    grouped_puzzles = {1: {}, 2: {}, 3: {}}
    
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            decompressor = zstd.ZstdDecompressor()
            with decompressor.stream_reader(response) as reader:
                text_stream = io.TextIOWrapper(reader, encoding="utf-8", newline="")
                rows = csv.reader(text_stream)
                header = next(rows, None)

                print("Streaming and filtering puzzle rows into RAM...", flush=True)
                scanned = 0
                for row in rows:
                    scanned += 1
                    if len(row) < 8:
                        continue
                    
                    try:
                        rating = int(row[3])
                        deviation = int(row[4])
                        plays = int(row[6])
                    except ValueError:
                        continue

                    # New 1300 to 2100 rating bracket
                    if not (1300 <= rating <= 2100) or deviation > 80:
                        continue
                        
                    fen = row[1].strip()
                    moves_str = row[2].strip()
                    if not fen or not moves_str:
                        continue
                        
                    fen_bytes = fen.encode("utf-8")
                    moves_bytes = moves_str.encode("utf-8")
                    if len(fen_bytes) > 255 or len(moves_bytes) > 255:
                        continue

                    # A ply is a half-move. The first ply is the opponent's blunder.
                    user_moves = len(moves_str.split()) // 2
                    
                    if user_moves <= 3:
                        tier = 1
                        min_plays = 200
                    elif 4 <= user_moves <= 5:
                        tier = 2
                        min_plays = 200
                    else:
                        tier = 3
                        min_plays = 50
                        
                    if plays < min_plays:
                        continue
                        
                    theme = get_best_theme(row[7])
                    
                    if theme not in grouped_puzzles[tier]:
                        grouped_puzzles[tier][theme] = []
                    
                    grouped_puzzles[tier][theme].append((rating, fen, moves_str))

                    if scanned % 1_000_000 == 0:
                        t1_cnt = sum(len(v) for v in grouped_puzzles[1].values())
                        t2_cnt = sum(len(v) for v in grouped_puzzles[2].values())
                        t3_cnt = sum(len(v) for v in grouped_puzzles[3].values())
                        print(f"Scanned {scanned:,} rows | Pool T1: {t1_cnt:,} | T2: {t2_cnt:,} | T3: {t3_cnt:,}", flush=True)

                text_stream.detach()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

    print("Running Waterfall Redistribution Loop...", flush=True)
    # Waterfall distribution targets
    targets = {1: 325000, 2: 65000, 3: 13000}
    selected_puzzles = {}

    for tier, target in targets.items():
        pool = grouped_puzzles[tier]
        for theme in pool:
            random.shuffle(pool[theme])
            if theme not in selected_puzzles:
                selected_puzzles[theme] = []

        active_themes = list(pool.keys())
        remaining = target

        while remaining > 0 and active_themes:
            quota = remaining // len(active_themes)

            # If space is tiny, dish out 1 puzzle per theme until full
            if quota == 0:
                random.shuffle(active_themes)
                for theme in active_themes[:remaining]:
                    if pool[theme]:
                        selected_puzzles[theme].append(pool[theme].pop())
                break

            new_active = []
            for theme in active_themes:
                available = len(pool[theme])
                take = min(quota, available)
                selected_puzzles[theme].extend(pool[theme][:take])
                pool[theme] = pool[theme][take:]
                remaining -= take
                
                # If theme still has puzzles, keep it active for the next redistribution loop
                if pool[theme]:
                    new_active.append(theme)
            active_themes = new_active

    OUT_DIR.mkdir(exist_ok=True)
    
    total_saved = 0
    for theme, puzzles in selected_puzzles.items():
        if not puzzles:
            continue
        # Final massive shuffle so difficulty tiers are completely mixed within the .bin
        random.shuffle(puzzles)
        filepath = OUT_DIR / f"{theme}.bin"
        write_binary(puzzles, filepath)
        total_saved += len(puzzles)
        
    print(f"Successfully generated {len(selected_puzzles)} theme bins. Total puzzles: {total_saved:,}", flush=True)

    print("Zipping up all bin files into puzzles.zip...", flush=True)
    with zipfile.ZipFile(ZIP_FILE, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(OUT_DIR):
            for file in files:
                if file.endswith('.bin'):
                    filepath = os.path.join(root, file)
                    # Flattens the zip so all .bin files are right at the root
                    zipf.write(filepath, file) 
                
    print(f"Done! {ZIP_FILE} created successfully.", flush=True)

if __name__ == "__main__":
    main()
                        
