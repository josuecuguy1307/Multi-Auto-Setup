class Desk:
    def start(self, node_id, task_type):
        session.send("task", {})

    def start_environment_run(self, node_id, task_id, entry, params, decision):
        session = self.sessions(node_id)
        session.send("task", {"token": "none"})

    def start_other(self, node_id, task_id):
        session = self.sessions(node_id)
        session.send("task", {})
        origin = self.origins(task_id)
        if not origin:
            return self._refuse()
