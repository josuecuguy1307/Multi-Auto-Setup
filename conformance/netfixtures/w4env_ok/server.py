class C:
    def _owner_origin(self, task_id):
        for e in reversed(self.log.events("environment_task_origin")):
            if e.effective_tag == "OWNER" and e.payload.get("task") == task_id:
                return e.id
        return None
