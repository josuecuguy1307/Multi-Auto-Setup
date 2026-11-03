"""Satisfies HA11: a decision is an ask_choice; plain facts are text."""
from strings import FACT


class Harness:
    def sync(self, run):
        self._add(run.id, "ask_choice", "GUARD", "internal", {"options": []}, "Which machine do you want to set up?\n1. a\n2. b", "k1")
        self._add(run.id, "text", "GUARD", "internal", {}, t(self.lang, "none_picked"), "k2")
        self._add(run.id, "progress", "GUARD", "internal", {}, FACT, "k3")
