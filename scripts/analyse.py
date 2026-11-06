
import chess
import chess.pgn
import requests
import json
import time
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ==========================================
# CONFIGURATION
# ==========================================

API_URL = "https://chess-api.com/v1"

DEPTH = 18
VARIANTS = 2
MAX_THINKING_TIME = 100

# Delay between completed API requests.
REQUEST_DELAY = 0.1

MAX_RETRIES = 6
REQUEST_TIMEOUT = 60

OUTPUT_DIR = Path("analysis")
OUTPUT_FILE = OUTPUT_DIR / "game-analysis.json"

# ==========================================
# PGN
# ==========================================

PGN = r"""
1. b3 e5 2. Bb2 d6 3. g3 Nc6 4. Bg2 Nf6
5. d3 Bf5 6. Nd2 Be7 7. e4 Bg4 8. Ngf3 h6
9. Qe2 Qd7 10. h3 Be6 11. Nf1 Nh7 12. Ne3 Ng5
13. Nd5 Bd8 14. h4 Nxf3+ 15. Bxf3 Be7
16. Bg2 a5 17. a3 O-O 18. O-O f5 19. f4 Rae8
20. Qh5 Nd4 21. Bxd4 exd4 22. Nxe7+ Qxe7
23. Rae1 Qd7 24. e5 d5 25. Qe2 Qe7 26. Qf2 Qc5
27. Rb1 Ra8 28. c4 dxc4 29. bxc4 Qxa3
30. Bxb7 Rad8 31. Qd2 Rb8 32. Bg2 Qc3
33. Qd1 a4 34. Rxb8 Rxb8 35. Qxa4 Qxd3
36. Qc6 Bxc4 37. Rf3 Qe2 38. Qxc7 Rb1+
39. Kh2 Rb2 40. Qc8+ Kh7 41. Qxf5+ Kh8
42. Kh3 Qxg2+ 43. Kg4 Rb8 44. e6 Be2
45. Qe5 Bxf3+ 46. Kf5 Qc2+ 47. Qe4 Qxe4+ 0-1
"""

# ==========================================
# UTILITIES
# ==========================================

session = requests.Session()

def save_json(data):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    temp_file = OUTPUT_FILE.with_suffix(".tmp")

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    os.replace(temp_file, OUTPUT_FILE)

def api_request(fen):
    payload = {
        "fen": fen,
        "depth": DEPTH,
        "variants": VARIANTS,
        "maxThinkingTime": MAX_THINKING_TIME
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = session.post(
                API_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")

                try:
                    wait = float(retry_after)
                except (TypeError, ValueError):
                    wait = min(5 * (2 ** attempt), 120)

                print(f"Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue

            if response.status_code in (500, 502, 503, 504):
                wait = min(5 * (2 ** attempt), 120)
                print(f"Server error {response.status_code}. Waiting {wait}s...")
                time.sleep(wait)
                continue

            response.raise_for_status()

            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Unexpected API response format")

            if data.get("type") == "error" or data.get("error"):
                raise RuntimeError(
                    f"Chess API error: {data}"
                )

            return data

        except (requests.Timeout, requests.ConnectionError) as exc:
            wait = min(5 * (2 ** attempt), 120)
            print(f"Connection problem: {exc}")
            print(f"Retrying in {wait}s...")
            time.sleep(wait)

        except requests.HTTPError:
            raise

    raise RuntimeError("Maximum API retries exceeded")

def get_pv_san(board, continuation):
    """Convert a UCI continuation into SAN for readability."""

    result = []
    temp_board = board.copy()

    for uci in continuation or []:
        try:
            move = chess.Move.from_uci(uci)
            san = temp_board.san(move)
            result.append(san)
            temp_board.push(move)
        except (ValueError, AssertionError):
            result.append(uci)
            break

    return result

def extract_analysis(data, board):
    continuation = data.get("continuationArr", [])

    return {
        "type": data.get("type"),
        "eval": data.get("eval"),
        "centipawns": data.get("centipawns"),
        "winChance": data.get("winChance"),
        "depth": data.get("depth"),
        "mate": data.get("mate"),
        "best_move": data.get("move"),
        "best_move_san": data.get("san"),
        "continuation_uci": continuation,
        "continuation_san": get_pv_san(board, continuation),
        "text": data.get("text"),
        "time": data.get("time"),
        "taskId": data.get("taskId"),

        # Preserve the entire API response.
        "raw_response": data
    }

# ==========================================
# LOAD AND VALIDATE PGN
# ==========================================

print("Loading PGN...")

import io

game = chess.pgn.read_game(io.StringIO(PGN))

if game is None:
    raise ValueError("Unable to read PGN")

if game.errors:
    raise ValueError(f"PGN errors: {game.errors}")

if game.end().board().fen() is None:
    raise ValueError("Invalid final position")

board = game.board()

positions = []

# Initial position + every position after a played move.
positions.append({
    "ply": 0,
    "move_number": 1,
    "played_move": None,
    "played_move_san": None,
    "fen": board.fen(),
    "analysis": None,
    "status": "pending"
})

for move in game.mainline_moves():
    san = board.san(move)
    board.push(move)

    positions.append({
        "ply": board.ply(),
        "move_number": board.fullmove_number,
        "played_move": move.uci(),
        "played_move_san": san,
        "fen": board.fen(),
        "analysis": None,
        "status": "pending"
    })

total = len(positions)

print(f"Loaded {total} positions.")

# ==========================================
# OUTPUT STRUCTURE
# ==========================================

output = {
    "metadata": {
        "engine": "Stockfish 18 (Chess API)",
        "api": API_URL,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "depth_requested": DEPTH,
        "variants_requested": VARIANTS,
        "max_thinking_time_ms": MAX_THINKING_TIME,
        "request_delay_seconds": REQUEST_DELAY,
        "total_positions": total,
        "completed_positions": 0,
        "status": "running",
        "pgn": PGN.strip(),
        "headers": dict(game.headers)
    },
    "positions": positions
}

# ==========================================
# ANALYSIS
# ==========================================

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

completed = 0

for index, position in enumerate(positions):

    print(
        f"\n[{index + 1}/{total}] "
        f"Analysing ply {position['ply']}..."
    )

    try:
        response = api_request(position["fen"])

        print("FEN:", position["fen"])
        print("PLY:", position["ply"])

        analysis_board = chess.Board(position["fen"])

        position["analysis"] = extract_analysis(
            response,
            analysis_board
        )

        position["status"] = "completed"

        completed += 1

        output["metadata"]["completed_positions"] = completed

        print(
            f"Eval: {response.get('eval')} | "
            f"Win: {response.get('winChance')}% | "
            f"Depth: {response.get('depth')} | "
            f"Best: {response.get('san')}"
        )

    except Exception as exc:
        position["status"] = "error"
        position["error"] = str(exc)

        output["metadata"]["status"] = "partial"

        print(f"ERROR: {exc}")

        # Save all previous successful analyses.
        save_json(output)

        # Stop instead of silently producing incomplete results.
        raise

    # Save after every position so progress is preserved.
    save_json(output)

    # No delay needed after the final request.
    if index < total - 1:
        time.sleep(REQUEST_DELAY)

# ==========================================
# FINISH
# ==========================================

output["metadata"]["completed_positions"] = completed
output["metadata"]["status"] = (
    "completed" if completed == total else "partial"
)
output["metadata"]["finished_at"] = datetime.now(
    timezone.utc
).isoformat()

save_json(output)

session.close()

print("\n===================================")
print("ANALYSIS FINISHED")
print(f"Positions: {completed}/{total}")
print(f"Output: {OUTPUT_FILE}")
print("===================================")
