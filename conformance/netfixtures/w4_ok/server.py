class Coordinator:
    def _owner_approval(self, plan_hash, step, action):
        for e in reversed(self.log.events("setup_step_approved")):
            p = e.payload
            if e.effective_tag == "OWNER" and p.get("plan_hash") == plan_hash and p.get("step") == step and p.get("action") == action:
                return e.id
        return None
