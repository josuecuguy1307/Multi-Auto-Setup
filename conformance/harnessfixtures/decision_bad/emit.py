"""Violates HA11: three ways of asking the owner to decide as free text."""
from strings import ASK_WHICH


class Harness:
    def sync(self, run):
        self._add(run.id, "text", "GUARD", "internal", {}, "Which machine do you want to set up?", "k1")  # a question as text
        self._add(run.id, "progress", "GUARD", "internal", {}, t(self.lang, "pick_one"), "k2")  # an instruction to pick, by key
        self._add(run.id, "show_data", "GUARD", "internal", {}, ASK_WHICH, "k3")  # a constant that asks
