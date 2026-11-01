class C:
    def _decomposition_origin(self, task_id, task_type=None, sensitivity=None):
        rec = next((e for e in self.log.events("decomposition_approved")), None)
        decided = self._owner_decision(rec.payload["owner_event"], rec.payload["parent"], rec.payload["subtasks"])
        listed = next((x for x in rec.payload["subtasks"] if x.get("id") == self.mine.payload.get("subtask_id")), None)
        first = self.first
        if first.payload.get("task") != task_id:
            return None
        if task_type != listed["type"]:
            return None
        if sensitivity != listed["sensitivity"]:
            return None
        return decided.id
