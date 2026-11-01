"""FIXTURE (violates B7): a team run that routes and runs a task the owner's plan does not list."""
from envrec_board.team import TeamRun


class UnapprovedTeam(TeamRun):
    def _origin(self, tid):
        return True
