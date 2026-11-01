class Coordinator:
    async def unpair_node(self, ref):
        rec = self.registry.get(ref)
        self.registry.remove(rec.node_id)
        await self.sessions[rec.node_id].send("reject", {"reason": "revoked"})

    async def _node_session(self, reader, writer, cert_der, source):
        rec = self.registry.by_cert(cert_der)
        if rec is None:
            await self._reject(None, "?", "unknown_node", source, writer)
            return
