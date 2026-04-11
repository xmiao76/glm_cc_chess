"""Chess engine with minimax, alpha-beta pruning, and iterative deepening.

Provides a search function that returns the best move for the current position.
Uses piece-square tables for evaluation and MVV-LVA for move ordering.
"""

from __future__ import annotations

import time
from src.board import Board, PIECE_VALUES
from src.moves import Move, generate_legal_moves, is_in_check


# Piece-square tables (from white's perspective, flip for black)
# Values are in centipawns, added to base piece value

PAWN_TABLE = [
     0,  0,  0,  0,  0,  0,  0,  0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
     5,  5, 10, 25, 25, 10,  5,  5,
     0,  0,  0, 20, 20,  0,  0,  0,
     5, -5,-10,  0,  0,-10, -5,  5,
     5, 10, 10,-20,-20, 10, 10,  5,
     0,  0,  0,  0,  0,  0,  0,  0,
]

KNIGHT_TABLE = [
    -50,-40,-30,-30,-30,-30,-40,-50,
    -40,-20,  0,  0,  0,  0,-20,-40,
    -30,  0, 10, 15, 15, 10,  0,-30,
    -30,  5, 15, 20, 20, 15,  5,-30,
    -30,  0, 15, 20, 20, 15,  0,-30,
    -30,  5, 10, 15, 15, 10,  5,-30,
    -40,-20,  0,  5,  5,  0,-20,-40,
    -50,-40,-30,-30,-30,-30,-40,-50,
]

BISHOP_TABLE = [
    -20,-10,-10,-10,-10,-10,-10,-20,
    -10,  0,  0,  0,  0,  0,  0,-10,
    -10,  0,  5, 10, 10,  5,  0,-10,
    -10,  5,  5, 10, 10,  5,  5,-10,
    -10,  0, 10, 10, 10, 10,  0,-10,
    -10, 10, 10, 10, 10, 10, 10,-10,
    -10,  5,  0,  0,  0,  0,  5,-10,
    -20,-10,-10,-10,-10,-10,-10,-20,
]

ROOK_TABLE = [
     0,  0,  0,  0,  0,  0,  0,  0,
     5, 10, 10, 10, 10, 10, 10,  5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
     0,  0,  0,  5,  5,  0,  0,  0,
]

QUEEN_TABLE = [
    -20,-10,-10, -5, -5,-10,-10,-20,
    -10,  0,  0,  0,  0,  0,  0,-10,
    -10,  0,  5,  5,  5,  5,  0,-10,
     -5,  0,  5,  5,  5,  5,  0, -5,
      0,  0,  5,  5,  5,  5,  0, -5,
    -10,  5,  5,  5,  5,  5,  0,-10,
    -10,  0,  5,  0,  0,  0,  0,-10,
    -20,-10,-10, -5, -5,-10,-10,-20,
]

KING_MIDDLEGAME_TABLE = [
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -20,-30,-30,-40,-40,-30,-30,-20,
    -10,-20,-20,-20,-20,-20,-20,-10,
     20, 20,  0,  0,  0,  0, 20, 20,
     20, 30, 10,  0,  0, 10, 30, 20,
]

PST = {
    "P": PAWN_TABLE,
    "N": KNIGHT_TABLE,
    "B": BISHOP_TABLE,
    "R": ROOK_TABLE,
    "Q": QUEEN_TABLE,
    "K": KING_MIDDLEGAME_TABLE,
}

# Move ordering: MVV-LVA values
VICTIM_ORDER = {"Q": 5, "R": 4, "B": 3, "N": 2, "P": 1, "K": 0}
ATTACKER_ORDER = {"P": 5, "N": 4, "B": 3, "R": 2, "Q": 1, "K": 0}

# Score constants
INF = 999999
CHECKMATE_SCORE = 100000


def evaluate(board: Board) -> int:
    """Evaluate the board position from white's perspective (positive = white advantage)."""
    score = 0
    for row in range(8):
        for col in range(8):
            piece = board.get_piece(row, col)
            if piece is None:
                continue
            color = piece[0]
            piece_type = piece[1]
            # Material value
            value = PIECE_VALUES[piece_type]
            # Piece-square table value
            if color == "w":
                pst_idx = row * 8 + col
            else:
                pst_idx = (7 - row) * 8 + col  # Flip for black
            value += PST[piece_type][pst_idx]

            if color == "w":
                score += value
            else:
                score -= value

    return score


def order_moves(board: Board, moves: list[Move], killers: list[Move] | None = None) -> list[Move]:
    """Order moves for better alpha-beta pruning.

    Priority: captures (MVV-LVA), killer moves, then quiet moves.
    """
    scored = []
    killer_set = set(killers) if killers else set()

    for move in moves:
        from_r, from_c, to_r, to_c, promo = move
        score = 0

        # Captures: MVV-LVA
        captured = board.get_piece(to_r, to_c)
        if captured is not None:
            victim_val = VICTIM_ORDER.get(captured[1], 0)
            attacker_val = ATTACKER_ORDER.get(board.get_piece(from_r, from_c)[1] if board.get_piece(from_r, from_c) else "P", 0)
            score = 10 * victim_val - attacker_val

        # Promotions
        if promo is not None:
            score += 20

        # Killer moves
        if move in killer_set:
            score += 5

        scored.append((score, move))

    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored]


class ChessEngine:
    """Chess engine using minimax with alpha-beta pruning and iterative deepening."""

    def __init__(self, max_depth: int = 4, time_limit: float = 2.0) -> None:
        self.max_depth = max_depth
        self.time_limit = time_limit
        self.nodes_searched = 0
        self.best_move: Move | None = None
        self._start_time = 0.0
        self._time_up = False
        self._killer_moves: dict[int, list[Move]] = {}
        self._history: dict[tuple, int] = {}

    def get_best_move(self, board: Board, time_limit: float | None = None) -> Move | None:
        """Find the best move using iterative deepening."""
        if time_limit is not None:
            self.time_limit = time_limit

        legal_moves = generate_legal_moves(board, board.active_color)
        if not legal_moves:
            return None

        if len(legal_moves) == 1:
            return legal_moves[0]

        self._start_time = time.time()
        self._time_up = False
        self.best_move = legal_moves[0]  # Fallback
        self._killer_moves = {}

        # Iterative deepening
        for depth in range(1, self.max_depth + 1):
            if self._time_up:
                break

            self.nodes_searched = 0
            score, move = self._search_root(board, depth)

            if not self._time_up and move is not None:
                self.best_move = move

            elapsed = time.time() - self._start_time
            if elapsed > self.time_limit * 0.6:
                break  # Not enough time for another depth

        return self.best_move

    def _search_root(self, board: Board, depth: int) -> tuple[int, Move | None]:
        """Search from root with alpha-beta."""
        color = board.active_color
        is_maximizing = color == "w"
        alpha = -INF
        beta = INF
        best_score = -INF if is_maximizing else INF
        best_move = None

        legal_moves = generate_legal_moves(board, color)
        killers = self._killer_moves.get(0, [])
        legal_moves = order_moves(board, legal_moves, killers)

        for move in legal_moves:
            if self._time_up:
                break

            from_r, from_c, to_r, to_c, promo = move
            captured = board.get_piece(to_r, to_c)
            # En passant capture check
            moving_piece = board.get_piece(from_r, from_c)
            is_ep = (moving_piece is not None and moving_piece[1] == "P"
                     and from_c != to_c and captured is None)

            # Make move using game state
            from src.game import GameState
            game = GameState(board.copy())
            game.make_move(move)

            score = self._alphabeta(game, depth - 1, alpha, beta, not is_maximizing)

            if is_maximizing:
                if score > best_score:
                    best_score = score
                    best_move = move
                alpha = max(alpha, score)
            else:
                if score < best_score:
                    best_score = score
                    best_move = move
                beta = min(beta, score)

        return best_score, best_move

    def _alphabeta(self, game: 'GameState', depth: int, alpha: int, beta: int,
                   is_maximizing: bool) -> int:
        """Alpha-beta pruning search."""
        self.nodes_searched += 1

        if time.time() - self._start_time > self.time_limit:
            self._time_up = True
            return evaluate(game.board)

        # Check for game over
        is_over, reason = game.is_game_over()
        if is_over:
            if "Checkmate" in reason:
                if is_maximizing:
                    return -CHECKMATE_SCORE + (self.max_depth - depth)
                else:
                    return CHECKMATE_SCORE - (self.max_depth - depth)
            return 0  # Draw

        if depth == 0:
            return self._quiescence(game, alpha, beta, is_maximizing, 4)

        color = game.board.active_color
        legal_moves = generate_legal_moves(game.board, color)
        ply = self.max_depth - depth
        killers = self._killer_moves.get(ply, [])
        legal_moves = order_moves(game.board, legal_moves, killers)

        if is_maximizing:
            max_eval = -INF
            best_move = None
            for i, move in enumerate(legal_moves):
                game.make_move(move)
                eval_score = self._alphabeta(game, depth - 1, alpha, beta, False)
                game.unmake_move()

                if self._time_up:
                    return eval_score

                if eval_score > max_eval:
                    max_eval = eval_score
                    best_move = move
                alpha = max(alpha, eval_score)
                if beta <= alpha:
                    # Store killer move
                    if captured_piece := game.board.get_piece(move[2], move[3]) is None:
                        self._killer_moves.setdefault(ply, []).insert(0, move)
                        if len(self._killer_moves[ply]) > 2:
                            self._killer_moves[ply] = self._killer_moves[ply][:2]
                    break
            return max_eval
        else:
            min_eval = INF
            best_move = None
            for i, move in enumerate(legal_moves):
                game.make_move(move)
                eval_score = self._alphabeta(game, depth - 1, alpha, beta, True)
                game.unmake_move()

                if self._time_up:
                    return eval_score

                if eval_score < min_eval:
                    min_eval = eval_score
                    best_move = move
                beta = min(beta, eval_score)
                if beta <= alpha:
                    if game.board.get_piece(move[2], move[3]) is None:
                        self._killer_moves.setdefault(ply, []).insert(0, move)
                        if len(self._killer_moves[ply]) > 2:
                            self._killer_moves[ply] = self._killer_moves[ply][:2]
                    break
            return min_eval

    def _quiescence(self, game: 'GameState', alpha: int, beta: int,
                    is_maximizing: bool, depth: int) -> int:
        """Quiescence search — only consider captures to avoid horizon effect."""
        stand_pat = evaluate(game.board)

        if depth == 0:
            return stand_pat

        if is_maximizing:
            if stand_pat >= beta:
                return beta
            alpha = max(alpha, stand_pat)

            color = game.board.active_color
            legal_moves = generate_legal_moves(game.board, color)
            # Only captures
            captures = [m for m in legal_moves
                        if game.board.get_piece(m[2], m[3]) is not None
                        or (game.board.get_piece(m[0], m[1]) is not None
                            and game.board.get_piece(m[0], m[1])[1] == "P"
                            and m[4] is not None)]  # Promotions too

            captures = order_moves(game.board, captures)

            for move in captures:
                game.make_move(move)
                eval_score = self._quiescence(game, alpha, beta, False, depth - 1)
                game.unmake_move()
                alpha = max(alpha, eval_score)
                if beta <= alpha:
                    break
            return alpha
        else:
            if stand_pat <= alpha:
                return alpha
            beta = min(beta, stand_pat)

            color = game.board.active_color
            legal_moves = generate_legal_moves(game.board, color)
            captures = [m for m in legal_moves
                        if game.board.get_piece(m[2], m[3]) is not None
                        or (game.board.get_piece(m[0], m[1]) is not None
                            and game.board.get_piece(m[0], m[1])[1] == "P"
                            and m[4] is not None)]

            captures = order_moves(game.board, captures)

            for move in captures:
                game.make_move(move)
                eval_score = self._quiescence(game, alpha, beta, True, depth - 1)
                game.unmake_move()
                beta = min(beta, eval_score)
                if beta <= alpha:
                    break
            return beta