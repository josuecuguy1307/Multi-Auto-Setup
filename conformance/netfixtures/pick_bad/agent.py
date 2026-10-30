class Adapter:
    async def _offer(self, args, ctx):
        wid = args["machine"]
        snap = self.offer_for_waiting(wid, "agent")  # VIOLATION: the offer is made before anything checks for the owner's pick
        if self.require_owner_pick(wid):
            return None
        return snap
