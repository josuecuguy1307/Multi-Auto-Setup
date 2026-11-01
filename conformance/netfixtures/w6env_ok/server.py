class C:
    def _owner_decision(self, event_id, parent, subtasks):
        e = next((x for x in self.log.events("decomposition_decision") if x.id == event_id), None)
        if e is None or e.effective_tag != "OWNER" or e.payload.get("gesture") not in ER.HUMAN_GESTURES:
            return None
        return e if ER.decomposition_canon(e.payload["parent"], e.payload["subtasks"]) == ER.decomposition_canon(parent, subtasks) else None
