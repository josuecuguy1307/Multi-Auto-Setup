"""The board runs in a worker thread while a chat reads it (`mas_board_show`): a read must be one state of the board, never half of a change. Found in the clean-copy run of the full suite: the reader
iterated the task dictionary while the writer added a subtask ("dictionary changed size during iteration"), which the screen turned into a bare 500. SIMULATED-LOCAL (a real Board; no network)."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from envrec_board.board import Board
from envrec_board.director import DirectorView
from envrec_board.log import MemoryLog
from envrec_board.team import TeamRun
from envrec_coordinator.services import Services


class Desk:
    hooks: dict = {}

    def pending(self):
        return []


def services_stub(board, root):
    view = DirectorView(board, root, None)
    team = SimpleNamespace(tools=SimpleNamespace(view=view))
    boards = {"b1": {"board": board, "desk": Desk(), "root": root, "team": team, "state": "running", "sensitivity": "public"}}
    return SimpleNamespace(boards=boards, board_preapproved=2, _out=Services._out)


def new_board():
    board = Board(MemoryLog())
    root = board.post_task("goal", "Prepare my notes", "done", "owner", "public", "owner", actor="OWNER")
    return board, root


def show(stub):
    return Services.board_show(stub, SimpleNamespace(cloud=False), board_id="b1")


def test_a_read_waits_for_a_writer_that_is_in_the_middle_of_a_change():
    board, root = new_board()
    stub = services_stub(board, root)
    inside, release, done = threading.Event(), threading.Event(), threading.Event()

    def writer():
        with board.reading():  # what every writer of the board holds while it changes it
            inside.set()
            release.wait(5)

    w = threading.Thread(target=writer)
    w.start()
    assert inside.wait(5)
    out: list = []
    r = threading.Thread(target=lambda: (out.append(show(stub)), done.set()))
    r.start()
    assert not done.wait(0.3), "the read went on while the board was being changed"
    release.set()
    assert done.wait(5) and out[0]["ok"] is True
    w.join()
    r.join()


def test_reading_the_board_while_subtasks_are_added_never_fails():
    board, root = new_board()
    stub = services_stub(board, root)
    stop, errors = threading.Event(), []

    def write():
        for i in range(400):
            board.post_task("text", f"input {i}", "x", "contains:x", "public", "director", actor="MODEL", parent=root)
        stop.set()

    def read():
        while not stop.is_set():
            try:
                show(stub)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
                return

    readers = [threading.Thread(target=read) for _ in range(3)]
    w = threading.Thread(target=write)
    for t in readers:
        t.start()
    w.start()
    w.join(60)
    for t in readers:
        t.join(10)
    assert errors == [] and len(board.tasks) == 401


def test_the_boards_own_summary_and_sensitivity_are_read_under_the_same_lock():
    board, root = new_board()
    held, release, done = threading.Event(), threading.Event(), []

    def writer():
        with board.reading():
            held.set()
            release.wait(5)

    threading.Thread(target=writer).start()
    assert held.wait(5)
    for call in (board.view, board.max_sensitivity):
        d = threading.Event()
        threading.Thread(target=lambda c=call, d=d: (c(), d.set())).start()
        assert not d.wait(0.2), f"{call.__name__} read while the board was being changed"
        done.append(d)
    release.set()
    assert all(d.wait(5) for d in done)
