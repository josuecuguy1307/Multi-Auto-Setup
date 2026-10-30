class Adapter:
    async def _offer(self, args, ctx):
        wid = args["machine"]
        refusal = self.require_owner_pick(wid)
        if refusal:
            return refusal
        return self.offer_for_waiting(wid, "agent")
