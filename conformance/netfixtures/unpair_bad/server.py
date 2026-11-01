class Coordinator:
    async def unpair_node(self, ref):  # tells the node first, removes it later: a hung send keeps the node trusted; and _node_session accepts anyone
        rec = self.registry.get(ref)
        await self.sessions[rec.node_id].send("reject", {"reason": "revoked"})
        self.registry.remove(rec.node_id)

    async def _node_session(self, reader, writer, cert_der, source):
        rec = self.registry.by_cert(cert_der)
        await self.serve(rec)
