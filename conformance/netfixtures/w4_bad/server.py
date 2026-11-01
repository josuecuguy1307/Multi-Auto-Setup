class Coordinator:
    def _owner_approval(self, plan_hash, step, action):
        for e in reversed(self.log.events("setup_step_approved")):
            if e.payload.get("plan_hash") == plan_hash:  # any actor, any step, any action
                return e.id
        return None
