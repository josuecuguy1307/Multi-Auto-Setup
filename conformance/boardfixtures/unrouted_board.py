"""FIXTURE (violates B4): a board that lets a worker take a task nobody routed."""
from envrec_board.board import Board


class UnroutedBoard(Board):
    def claim(self, task_id, node_id, *, actor, lease_s=None):
        with self._lock:
            return self._lease(self._task(task_id, actor), node_id, lease_s, actor)
