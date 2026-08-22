from kevin.cogs.fun import (
    C4_COLUMNS,
    C4_RED,
    C4_ROWS,
    C4_YELLOW,
    c4_drop,
    c4_is_full,
    c4_winning_cells,
    new_c4_board,
    render_c4_board,
)


def empty_board() -> list[list[int]]:
    return new_c4_board()


def test_new_board_is_empty_and_sized() -> None:
    board = empty_board()
    assert len(board) == C4_ROWS
    assert all(len(row) == C4_COLUMNS for row in board)
    assert all(cell == 0 for row in board for cell in row)


def test_discs_stack_from_the_bottom() -> None:
    board = empty_board()
    assert c4_drop(board, 3, C4_RED) == C4_ROWS - 1
    assert c4_drop(board, 3, C4_YELLOW) == C4_ROWS - 2
    assert board[C4_ROWS - 1][3] == C4_RED
    assert board[C4_ROWS - 2][3] == C4_YELLOW


def test_full_column_rejects_further_drops() -> None:
    board = empty_board()
    for _ in range(C4_ROWS):
        assert c4_drop(board, 0, C4_RED) is not None
    assert c4_drop(board, 0, C4_YELLOW) is None


def test_horizontal_win_detected() -> None:
    board = empty_board()
    for column in range(4):
        c4_drop(board, column, C4_RED)
    cells = c4_winning_cells(board, C4_RED)
    assert cells is not None and len(cells) == 4
    assert c4_winning_cells(board, C4_YELLOW) is None


def test_vertical_win_detected() -> None:
    board = empty_board()
    for _ in range(4):
        c4_drop(board, 5, C4_YELLOW)
    assert c4_winning_cells(board, C4_YELLOW) is not None


def test_diagonal_wins_in_both_directions() -> None:
    ascending = empty_board()
    for index in range(4):
        row = C4_ROWS - 1 - index
        ascending[row][index] = C4_RED
    assert c4_winning_cells(ascending, C4_RED) is not None
    assert c4_winning_cells(ascending, C4_YELLOW) is None

    descending = empty_board()
    for index in range(4):
        descending[index][index] = C4_YELLOW
    assert c4_winning_cells(descending, C4_YELLOW) is not None
    assert c4_winning_cells(descending, C4_RED) is None


def test_scattered_pieces_have_no_winner() -> None:
    board = empty_board()
    c4_drop(board, 0, C4_RED)
    c4_drop(board, 2, C4_RED)
    c4_drop(board, 4, C4_RED)
    c4_drop(board, 6, C4_RED)
    assert c4_winning_cells(board, C4_RED) is None


def test_board_full_detection() -> None:
    board = empty_board()
    assert not c4_is_full(board)
    for column in range(C4_COLUMNS):
        for _ in range(C4_ROWS):
            c4_drop(board, column, C4_RED)
    assert c4_is_full(board)


def test_rendered_board_has_header_and_discs() -> None:
    board = empty_board()
    c4_drop(board, 0, C4_RED)
    rendered = render_c4_board(board)
    assert "7️⃣" in rendered
    assert rendered.count("🔴") == 1
    assert rendered.count("⚪") == C4_ROWS * C4_COLUMNS - 1
