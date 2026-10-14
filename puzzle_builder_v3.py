import csv
import struct
import json
import io
import os
import zstandard as zstd
from collections import defaultdict

# 39 Supported Themes
THEMES = [
    "arabianMate", "pin", "zugzwang", "knightEndgame", "rookEndgame", "endgame", "exposedKing", 
    "discoveredAttack", "bodenMate", "intermezzo", "opening", "interference", "xRayAttack", 
    "hookMate", "smotheredMate", "sacrifice", "clearance", "castling", "quietMove", "mateIn2", 
    "middlegame", "skewer", "queenEndgame", "deflection", "doubleCheck", "defensiveMove", 
    "hangingPiece", "enPassant", "trappedPiece", "bishopEndgame", "mateIn1", "anastasiaMate", 
    "mateIn4", "mateIn3", "underPromotion", "fork", "attraction", "pawnEndgame", "mateIn5"
]
THEME_MAP = {t: i for i, t in enumerate(THEMES)}

# Target limits per 100-rating bracket
LIMITS = {1: 3000, 2: 1000, 3: 250}

# 1200-1299 Reserve limits
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

def process_lichess_database(zst_path):
    # Data Stores
    categorized = defaultdict(lambda: defaultdict(list)) # theme -> tier -> list
    unclassified = defaultdict(list) # tier -> list
    reserve = {1: [], 2: [], 3: []} # tier -> list
    
    print("Streaming Lichess ZST...")
    with open(zst_path, 'rb') as f:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(f) as reader:
            text_stream = io.TextIOWrapper(reader, encoding='utf-8')
            csv_reader = csv.reader(text_stream)
            next(csv_reader) # Skip Header
            
            for row in csv_reader:
                # PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags
                if len(row) < 8: continue
                
                fen, moves = row[1], row[2]
                rating = int(row[3])
                popularity = int(row[5])
                plays = int(row[6])
                puzzle_themes = row[7].split(' ')
                
                tier = get_tier(moves)
                
                # Identify first supported theme
                primary_theme = next((t for t in puzzle_themes if t in THEME_MAP), None)
                
                # RESERVE Logic (1200-1299)
                if 1200 <= rating < 1300 and primary_theme:
                    if popularity >= 90 and plays >= 400:
                        reserve[tier].append((rating, fen, moves, primary_theme))
                    continue
                
                if rating < 1300:
                    continue
                
                # MAIN Logic (1300+)
                if primary_theme:
                    categorized[primary_theme][tier].append((rating, fen, moves))
                else:
                    unclassified[tier].append((rating, fen, moves))

    # Compile and Build Binaries
    print("Writing Binaries and Meta JSON...")
    os.makedirs('puzzlesbin/unclassified', exist_ok=True)
    os.makedirs('puzzlesbin/reserve', exist_ok=True)
    
    meta_json = {}
    total_saved = 0

    # 1. Main 39 Themes
    for theme in THEMES:
        os.makedirs(f'puzzlesbin/{theme}', exist_ok=True)
        meta_json[theme] = {}
        
        for tier in [1, 2, 3]:
            meta_json[theme][f"t{tier}"] = {"total": 0, "brackets": {}}
            puzzles = categorized[theme][tier]
            puzzles.sort(key=lambda x: x[0]) # Sort strictly by rating
            
            # Group into brackets
            brackets = defaultdict(list)
            for p in puzzles:
                brackets[ (p[0] // 100) * 100 ].append(p)
                
            final_list = []
            for b_val in sorted(brackets.keys()):
                selected = brackets[b_val][:LIMITS[tier]]
                start_idx = len(final_list)
                final_list.extend(selected)
                end_idx = len(final_list) - 1
                meta_json[theme][f"t{tier}"]["brackets"][str(b_val)] = [start_idx, end_idx]
                
            if not final_list:
                continue

            meta_json[theme][f"t{tier}"]["total"] = len(final_list)
            total_saved += len(final_list)
            
            with open(f'puzzlesbin/{theme}/{tier}.bin', 'wb') as bf:
                bf.write(struct.pack('<I', len(final_list)))
                for p in final_list:
                    bf.write(pack_standard_puzzle(p[0], p[1], p[2]))

    # 2. Unclassified (Fill to ~1.7M)
    remaining_quota = 1700000 - total_saved
    meta_json["unclassified"] = {}
    for tier in [1, 2, 3]:
        if remaining_quota <= 0: break
        
        puzzles = unclassified[tier]
        puzzles.sort(key=lambda x: x[0])
        
        # We take heavily from unclassified if needed, capped by our quota limits
        tier_quota = min(remaining_quota, len(puzzles))
        final_list = puzzles[:tier_quota]
        
        if final_list:
            meta_json["unclassified"][f"t{tier}"] = {"total": len(final_list), "brackets": {}}
            # Simplify brackets for unclassified just to index boundaries
            cur_bracket = -1
            start_idx = 0
            for idx, p in enumerate(final_list):
                b_val = (p[0] // 100) * 100
                if b_val != cur_bracket:
                    if cur_bracket != -1:
                        meta_json["unclassified"][f"t{tier}"]["brackets"][str(cur_bracket)] = [start_idx, idx-1]
                    cur_bracket = b_val
                    start_idx = idx
            meta_json["unclassified"][f"t{tier}"]["brackets"][str(cur_bracket)] = [start_idx, len(final_list)-1]

            remaining_quota -= len(final_list)
            
            with open(f'puzzlesbin/unclassified/{tier}.bin', 'wb') as bf:
                bf.write(struct.pack('<I', len(final_list)))
                for p in final_list:
                    bf.write(pack_standard_puzzle(p[0], p[1], p[2]))

    # 3. Reserve 1200 (Single unified file)
    reserve_final = []
    for tier in [1, 2, 3]:
        puzzles = reserve[tier]
        puzzles.sort(key=lambda x: x[0])
        reserve_final.extend(puzzles[:RESERVE_LIMITS[tier]])
    
    reserve_final.sort(key=lambda x: x[0])
    
    with open('puzzlesbin/reserve/1200.bin', 'wb') as bf:
        bf.write(struct.pack('<I', len(reserve_final)))
        for p in reserve_final:
            bf.write(pack_reserve_puzzle(THEME_MAP[p[3]], p[0], p[1], p[2]))

    with open('puzzlesbin/puzzle_meta.json', 'w') as mf:
        json.dump(meta_json, mf)
        
    print(f"Done! Extracted ~{1700000 - remaining_quota} main puzzles and {len(reserve_final)} reserve puzzles.")

if __name__ == "__main__":
    process_lichess_database("lichess_db_puzzle.csv.zst")
                
