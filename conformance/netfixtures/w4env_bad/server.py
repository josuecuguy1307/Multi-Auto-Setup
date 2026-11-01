class C:
    def _owner_origin(self, task_id):
        for e in reversed(self.log.events("anything")):
            return e.id
        return None
