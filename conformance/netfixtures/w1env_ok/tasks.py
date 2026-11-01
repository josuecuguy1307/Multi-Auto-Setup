class Desk:
    def start(self, node_id, task_type):
        if task_type in T.WRITE_TYPES:
            return self._refuse()
        session.send("task", {})

    def start_environment_run(self, node_id, task_id, entry, params, decision):
        origin = self.origins(task_id)
        if not origin:
            return self._refuse()
        session = self.sessions(node_id)
        session.send("task", {"token": origin})
