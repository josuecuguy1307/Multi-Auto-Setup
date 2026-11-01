"""FIXTURE (violates B2): an emitter that hands every intent to every sink, confidential ones included."""
from envrec_board.intents import IntentEmitter


class LeakyEmitter(IntentEmitter):
    def _deliver(self, i):
        for s in self.sinks:
            s.send(i)
