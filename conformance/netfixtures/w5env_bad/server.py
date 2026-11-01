class C:
    def _decomposition_origin(self, task_id, task_type=None, sensitivity=None):
        dec = next(iter(self.log.events("anything")), None)
        return dec.id
