import csv
import random
import struct
import urllib.request
import zstandard as zstd
import io

URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
OUT_FILE = "puzzles.bin"

THEMES_MAP = {
    "mateIn1": 1, "mateIn2": 2, "mateIn3": 3, "mateIn4": 4, "mate": 5,
    "fork": 6, "pin": 7, "skewer": 8, "discoveredAttack": 9, "sacrifice": 10,
    "deflection": 11, "attraction": 12, "clearance": 13, "interference": 14,
    "xRayAttack": 15, "backRankMate": 16, "smotheredMate": 17, "anastasiaMate": 18,
    "arabianMate": 19, "hookMate": 20, "pawnEndgame": 21, "rookEndgame": 22,
    "queenEndgame": 23, "knightEndgame": 24, "bishopEndgame": 25, "endgame": 26,
    "opening": 27, "middlegame": 28, "zugzwang": 29, "quietMove": 30
}

def get_theme_id(themes_str):
    for t in themes_str.split():
        if t in THEMES_MAP:
            return THEMES_MAP[t]
    return 0

def main():
    print("Downloading Lichess puzzle database...")
    req = urllib.request.urlopen(URL)
    dctx = zstd.ZstdDecompressor()
    
    tier3_pool = []
    tier2_pool = []
    tier1_pool = []
    
    target_t3 = 300000
    target_t2 = 100000
    target_t1 = 10000

    print("Streaming and filtering rows...")
    with dctx.stream_reader(req) as reader:
        wrapper = io.TextIOWrapper(reader, encoding='utf-8')
        reader_csv = csv.reader(wrapper)
        
        header = next(reader_csv)
        for row in reader_csv:
            if len(row) < 7:
                continue
            try:
                rating = int(row[3])
                deviation = int(row[4])
                plays = int(row[5])
                themes = row[7]
                moves = row[2].split()
            except ValueError:
                continue

            if not (1000 <= rating <= 1800):
                continue
            if deviation > 80:
                continue

            plies = len(moves)
            
            if plies < 8 and plays >= 200 and len(tier3_pool) < target_t3:
                tid = get_theme_id(themes)
                tier3_pool.append((rating, tid, row[1], row[2]))
            elif 8 <= plies < 16 and plays >= 200 and len(tier2_pool) < target_t2:
                tid = get_theme_id(themes)
                tier2_pool.append((rating, tid, row[1], row[2]))
            elif plies >= 16 and plays >= 50 and len(tier1_pool) < target_t1:
                tid = get_theme_id(themes)
                tier1_pool.append((rating, tid, row[1], row[2]))

            if len(tier3_pool) >= target_t3 and len(tier2_pool) >= target_t2 and len(tier1_pool) >= target_t1:
                break

    print(f"Collected: Tier3={len(tier3_pool)}, Tier2={len(tier2_pool)}, Tier1={len(tier1_pool)}")
    
    all_puzzles = tier3_pool + tier2_pool + tier1_pool
    random.shuffle(all_puzzles)
    
    print(f"Writing {len(all_puzzles)} puzzles to binary format...")
    with open(OUT_FILE, "wb") as f:
        f.write(struct.pack("<I", len(all_puzzles)))
        for rating, tid, fen, moves_str in all_puzzles:
            fen_bytes = fen.encode('utf-8')
            moves_bytes = moves_str.encode('utf-8')
            f.write(struct.pack("<HB", rating, tid))
            f.write(struct.pack("B", len(fen_bytes)))
            f.write(fen_bytes)
            f.write(struct.pack("B", len(moves_bytes)))
            f.write(moves_bytes)
    print("Done!")

if __name__ == "__main__":
    main()
