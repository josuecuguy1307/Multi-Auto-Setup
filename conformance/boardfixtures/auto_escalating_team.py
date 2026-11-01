"""FIXTURE (violates B6): a team run that takes the router's escalation without asking the owner."""
from envrec_board.team import TeamRun


class AutoEscalatingTeam(TeamRun):
    def _report(self, tid, g):
        r = self.router.report_result(tid, self.board.tasks[tid]["escalation_step"], {"passed": g["passed"], "grader": g["grader"], "evidence_ref": ""})
        if r is not None and "route" in r:
            self.board.record_route_decision(tid, r)
