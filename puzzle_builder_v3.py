import csv
import struct
import json
import io
import os
import zstandard as zstd
from collections import defaultdict

# 1. Canonical Theme Order (Used for bitmask IDs 0-38)
THEMES = [
    "arabianMate", "pin", "zugzwang", "knightEndgame", "rookEndgame", "endgame", "exposedKing", 
    "discoveredAttack", "bodenMate", "intermezzo", "opening", "interference", "xRayAttack", 
    "hookMate", "smotheredMate", "sacrifice", "clearance", "castling", "quietMove", "mateIn2", 
    "middlegame", "skewer", "queenEndgame", "deflection", "doubleCheck", "defensiveMove", 
    "hangingPiece", "enPassant", "trappedPiece", "bishopEndgame", "mateIn1", "anastasiaMate", 
    "mateIn4", "mateIn3", "underPromotion", "fork", "attraction", "pawnEndgame", "mateIn5"
]
THEME_MAP = {t: i for i, t in enumerate(THEMES)}

# 2. Priority List: Rarest & most specific tactical motifs first, broad phases last
PRIORITY_THEMES = [
    # Rare mates & specific rules
    "underPromotion", "bodenMate", "castling", "mateIn5", "smotheredMate", 
    "arabianMate", "anastasiaMate", "hookMate", "mateIn4", "enPassant",
    # Specific piece motifs / endgames
    "knightEndgame", "queenEndgame", "bishopEndgame", "rookEndgame", "pawnEndgame",
    "interference", "xRayAttack", "doubleCheck", "trappedPiece", "intermezzo", 
    "hangingPiece", "skewer", "discoveredAttack", "clearance", "exposedKing", 
    "fork", "sacrifice", "pin", "deflection", "attraction", "zugzwang", 
    "quietMove", "defensiveMove",
    # Mate lengths
    "mateIn1", "mateIn2", "mateIn3",
    # Broad game phases (catch-all at the absolute bottom)
    "opening", "endgame", "middlegame"
]

# Per-bracket limits (100 rating span)
BRACKET_LIMITS = {1: 3000, 2: 1000, 3: 250}

# Hard total limits per theme & tier
THEME_TIER_MAX = {1: 30000, 2: 10000, 3: 3500}

# Reserve limits (1200-1299)
RESERVE_LIMITS = {1: 9000, 2: 1000, 3: 100}

def get_tier(moves_str):
    ply = len(moves_str.strip().split(' '))
    if ply <= 5: return 1
    elif ply <= 9: return 2
    else: return 3

def pack_standard_puzzle(rating, fen, moves):
    fen_b = fen.encode('utf-8')
    mov_b = moves.encode('utf-8')
    return struct.pack(f'<H B {len(fen_b)}s B {len(mov_b)}s', rating, len(fen_b), fen_b, len(mov_b), mov_b)

def pack_reserve_puzzle(theme_id, rating, fen, moves):
    fen_b = fen.encode('utf-8')
    mov_b = moves.encode('utf-8')
    return struct.pack(f'<B H B {len(fen_b)}s B {len(mov_b)}s', theme_id, rating, len(fen_b), fen_b, len(mov_b), mov_b)

def build_puzzles(zst_path="lichess_db_puzzle.csv.zst"):
    categorized = defaultdict(lambda: defaultdict(list))
    theme_bracket_counts = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    unclassified = defaultdict(list)
    reserve = {1: [], 2: [], 3: []}

    print("Streaming and categorizing Lichess puzzle dataset...")
    
    with open(zst_path, 'rb') as f:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(f) as reader:
            text_stream = io.TextIOWrapper(reader, encoding='utf-8')
            csv_reader = csv.reader(text_stream)
            next(csv_reader) # Skip CSV header

            for row in csv_reader:
                if len(row) < 8: continue
                
                fen, moves = row[1], row[2]
                rating = int(row[3])
                popularity = int(row[5])
                plays = int(row[6])
                puzzle_tags = set(row[7].split(' '))
                
                tier = get_tier(moves)
                
                # Check match against prioritized theme list
                matched_theme = next((t for t in PRIORITY_THEMES if t in puzzle_tags), None)

                # 1200 Reserve pool
                if 1200 <= rating < 1300 and matched_theme:
                    if popularity >= 90 and plays >= 400:
                        if len(reserve[tier]) < RESERVE_LIMITS[tier]:
                            reserve[tier].append((rating, fen, moves, matched_theme))
                    continue

                if rating < 1300:
                    continue

                # Normal 1300+ distribution
                bracket = (rating // 100) * 100
                placed = False

                if matched_theme:
                    cur_tier_total = len(categorized[matched_theme][tier])
                    cur_bracket_count = theme_bracket_counts[matched_theme][tier][bracket]

                    if cur_tier_total < THEME_TIER_MAX[tier] and cur_bracket_count < BRACKET_LIMITS[tier]:
                        categorized[matched_theme][tier].append((rating, fen, moves))
                        theme_bracket_counts[matched_theme][tier][bracket] += 1
                        placed = True

                # Surplus and unclassified puzzles go to spillover
                if not placed:
                    if len(unclassified[tier]) < 200000:
                        unclassified[tier].append((rating, fen, moves))

    print("Writing binary files and metadata...")
    os.makedirs('puzzlesbin/unclassified', exist_ok=True)
    os.makedirs('puzzlesbin/reserve', exist_ok=True)
    
    meta_json = {}
    total_saved = 0

    # 1. Write the 39 Categorized Themes
    for theme in THEMES:
        os.makedirs(f'puzzlesbin/{theme}', exist_ok=True)
        meta_json[theme] = {}
        
        for tier in [1, 2, 3]:
            meta_json[theme][f"t{tier}"] = {"total": 0, "brackets": {}}
            puzzles = categorized[theme][tier]
            
            if not puzzles:
                continue

            # Sort ascending by rating
            puzzles.sort(key=lambda x: x[0])
            meta_json[theme][f"t{tier}"]["total"] = len(puzzles)
            total_saved += len(puzzles)

            # Record exact contiguous start and end indices for every bracket
            cur_bracket = -1
            start_idx = 0
            for idx, p in enumerate(puzzles):
                b_val = (p[0] // 100) * 100
                if b_val != cur_bracket:
                    if cur_bracket != -1:
                        meta_json[theme][f"t{tier}"]["brackets"][str(cur_bracket)] = [start_idx, idx - 1]
                    cur_bracket = b_val
                    start_idx = idx
            meta_json[theme][f"t{tier}"]["brackets"][str(cur_bracket)] = [start_idx, len(puzzles) - 1]

            # Write standard binary payload
            with open(f'puzzlesbin/{theme}/{tier}.bin', 'wb') as bf:
                bf.write(struct.pack('<I', len(puzzles)))
                for p in puzzles:
                    bf.write(pack_standard_puzzle(p[0], p[1], p[2]))

    # 2. Write Unclassified Spillover (filling to reach ~1.7M–1.8M)
    meta_json["unclassified"] = {}
    for tier in [1, 2, 3]:
        puzzles = unclassified[tier]
        if not puzzles: continue

        puzzles.sort(key=lambda x: x[0])
        meta_json["unclassified"][f"t{tier}"] = {"total": len(puzzles), "brackets": {}}
        total_saved += len(puzzles)

        cur_bracket = -1
        start_idx = 0
        for idx, p in enumerate(puzzles):
            b_val = (p[0] // 100) * 100
            if b_val != cur_bracket:
                if cur_bracket != -1:
                    meta_json["unclassified"][f"t{tier}"]["brackets"][str(cur_bracket)] = [start_idx, idx - 1]
                cur_bracket = b_val
                start_idx = idx
        meta_json["unclassified"][f"t{tier}"]["brackets"][str(cur_bracket)] = [start_idx, len(puzzles) - 1]

        with open(f'puzzlesbin/unclassified/{tier}.bin', 'wb') as bf:
            bf.write(struct.pack('<I', len(puzzles)))
            for p in puzzles:
                bf.write(pack_standard_puzzle(p[0], p[1], p[2]))

    # 3. Write 1200 Reserve File
    reserve_puzzles = []
    for tier in [1, 2, 3]:
        reserve_puzzles.extend(reserve[tier])
    reserve_puzzles.sort(key=lambda x: x[0])

    with open('puzzlesbin/reserve/1200.bin', 'wb') as bf:
        bf.write(struct.pack('<I', len(reserve_puzzles)))
        for p in reserve_puzzles:
            bf.write(pack_reserve_puzzle(THEME_MAP[p[3]], p[0], p[1], p[2]))

    # 4. Save Metadata JSON
    with open('puzzlesbin/puzzle_meta.json', 'w') as mf:
        json.dump(meta_json, mf)

    print(f"Build complete. Total puzzles packaged: {total_saved} (plus {len(reserve_puzzles)} reserve puzzles).")

if __name__ == "__main__":
    build_puzzles("lichess_db_puzzle.csv.zst")
                
