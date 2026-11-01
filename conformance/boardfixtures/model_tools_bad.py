"""FIXTURE (violates B1): a model-facing tool that answers an owner decision itself."""
from envrec_board.decisions import DecisionDesk


class BadModelTools:
    def __init__(self, desk: DecisionDesk) -> None:
        self.desk = desk

    def approve(self, decision_id: str):
        return self.desk.answer(decision_id, "approve", channel="os_dialog")
