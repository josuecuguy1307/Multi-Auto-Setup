class C:
    def _owner_decision(self, event_id, parent, subtasks):
        return next((x for x in self.log.events("anything") if x.id == event_id), None)
