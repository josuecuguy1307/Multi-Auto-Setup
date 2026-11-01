class Desk:
    def start(self, node_id, task_type):
        if task_type in T.WRITE_TYPES:
            return self._refuse()
        session.send("task", {})

    def start_setup_step(self, node_id, plan, step, action="run"):
        approval = self.approvals(plan, step, action)
        if not approval:
            return self._refuse()
        session = self.sessions(node_id)
        session.send("task", {"token": approval})
