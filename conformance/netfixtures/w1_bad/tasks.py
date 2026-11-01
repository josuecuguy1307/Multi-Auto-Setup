class Desk:
    def start(self, node_id, task_type):
        session.send("task", {})  # the generic start sends any type, a write too

    def start_setup_step(self, node_id, plan, step, action="run"):
        session = self.sessions(node_id)
        session.send("task", {"plan": plan})  # sent without looking for the owner's approval

    def start_other(self, plan, step):
        approval = self.approvals(plan, step, "run")
        session.send("task", {})
