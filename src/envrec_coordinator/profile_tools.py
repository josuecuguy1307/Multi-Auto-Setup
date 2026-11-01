"""The owner's profile, the harness's proposal and the cancelling of a run, as services tools (mixed into `Services`).

Nothing here decides for the owner or the model: the questions and their options come from data (`skills/owner-profile`), the proposal is built from the catalog crossed with what is detected on the machines, and
every answer is the owner's, relayed by the chat. A task's sensitivity is derived here from the profile; when the harness cannot tell it asks the owner, it never guesses."""

from __future__ import annotations

import hashlib
import json
import secrets

from envrec_coordinator import services_strings as S
from envrec_coordinator.profile import LEVELS, Profile, ProfileError
from envrec_coordinator.router import strings as RS
from envrec_coordinator.router.catalog import build_candidates
from envrec_coordinator.router.model import EntryChat, Task
from envrec_net import envrun as E

ASK_TYPES = "ask"  # the chat questions of this module share one key family


class ProfileTools:
    # ---- the profile --------------------------------------------------------------------------------------------------------------------------

    def _init_profile(self) -> None:
        self.profile = Profile(self.coord.state / "profile.json", log=lambda t, p: self.coord.log.add(t, {**p, "tag": "GUARD"}))
        self.profile.apply(self.policy)
        self._multi: dict[str, list[str]] = {}  # a multiple-choice question being answered one option at a time

    def _question(self, ch, field: str | None = None, machine: str | None = None):
        """Put one profile question to the owner as a chat question; returns its text (None when nothing is left to ask)."""
        p, h = self.profile, self.coord.harness
        if field is None and machine is None:
            todo = p.missing()
            field = todo[0] if todo else None
            if field is None:
                machine = next((r.node_id for r in self.coord.registry.all() if r.node_id not in p.roles), None)
                if machine is None:
                    return None
        if machine is not None:
            opts = [{"id": o["id"], "label": o["label"]} for o in p.role_options()]
            text = p.schema["machine_roles"]["question"].format(machine=self._name(machine))
            i = h.ask_chat(None, key=f"profile:role:{machine}", text=text, options=opts, on_choice=lambda o, m=machine, c=ch: self._on_role(c, m, o))
            return i.text_fallback
        f = p.fields[field]
        if f["kind"] == "multi":
            chosen = self._multi.get(field, [])
            opts = [{"id": o["id"], "label": o["label"]} for o in f["options"] if o["id"] not in chosen]
            opts.append({"id": "done" if chosen else "none", "label": "That is all" if chosen else f["none_label"]})
            text = f["question"] + (f" So far: {', '.join(chosen)}." if chosen else "")
        elif f["kind"] == "number":
            opts = [{"id": str(n), "label": str(n)} for n in range(f["min"], f["max"] + 1)]
            text = f["question"]
        else:
            opts = [{"id": o["id"], "label": o["label"]} for o in f["options"]]
            text = f["question"]
        return h.ask_chat(None, key=f"profile:{field}", text=text, options=opts, on_choice=lambda o, fl=field, c=ch: self._on_field(c, fl, o)).text_fallback

    def _on_field(self, ch, field: str, option: str) -> str:
        p, kind = self.profile, self.profile.fields[field]["kind"]
        if kind == "multi" and option not in ("none", "done"):
            self._multi.setdefault(field, []).append(option)
            return self._question(ch, field)  # the same question again, without what he already chose
        value = self._multi.pop(field, []) if kind == "multi" else int(option) if kind == "number" else option
        p.set(field, [] if option == "none" else value)
        p.apply(self.policy)
        return self._question(ch) or S.PROFILE_COMPLETE

    def _on_role(self, ch, machine: str, option: str) -> str:
        self.profile.set_role(machine, option)
        self.profile.apply(self.policy)
        return self._question(ch) or S.PROFILE_COMPLETE

    def _more_questions(self, text: str, ch) -> str:
        """First run: while the profile is incomplete the harness itself puts its next question after a tool's result (once, while one is not already waiting)."""
        if any(x.extra.get("state") == "pending" and str(x.extra.get("base", "")).startswith("profile:") for log in self.coord.harness._logs.values() for x in log):
            return text
        q = self._question(ch)
        return text + ("\n\n" + S.PROFILE_FIRST_RUN + "\n" + q if q else "")

    def profile_tool(self, ch, *, action: str = "show", field: str | None = None, value=None, machine: str | None = None, role: str | None = None, what: str | None = None, id: str | None = None) -> dict:
        p = self.profile
        try:
            if action == "show":
                text = S.PROFILE_HEAD + "\n" + "\n".join(f"- {k}: {json.dumps(v)}" for k, v in p.summary()["values"].items())
                text += "\n" + S.PROFILE_ROLES.format(roles=", ".join(f"{self._name(m)}: {r}" for m, r in p.roles.items()) or "none yet")
                text += "\n" + S.PROFILE_CLOSED.format(closed="; ".join(f"{k}: {', '.join(v)}" for k, v in p.closed.items() if v) or "nothing")
                q = self._question(ch)
                return self._out(True, text + (("\n\n" + q) if q else ""), **p.summary())
            if action == "ask":
                q = self._question(ch)
                return self._out(True, q or S.PROFILE_COMPLETE)
            if action == "set":
                if field is None or value is None:
                    return self._out(False, S.PROFILE_NEEDS_VALUE.format(fields=", ".join(p.fields)))
                v = p.set(field, int(value) if p.fields.get(field, {}).get("kind") == "number" and str(value).isdigit() else value)
                p.apply(self.policy)
                return self._out(True, S.PROFILE_SET.format(field=field, value=json.dumps(v)))
            if action == "role":
                nid = self.coord.harness._resolve_node(machine)
                if nid is None or role is None:
                    return self._out(False, S.PROFILE_NEEDS_ROLE.format(roles=", ".join(o["id"] for o in p.role_options())))
                p.set_role(nid, role)
                p.apply(self.policy)
                return self._out(True, S.PROFILE_ROLE_SET.format(machine=self._name(nid), role=role))
            if action in ("close", "open"):
                kind = {"environment": "environments", "subtype": "subtypes", "access": "accesses"}.get(str(what))
                if kind is None or not id:
                    return self._out(False, S.PROFILE_NEEDS_CLOSE)
                if action == "open":
                    changed = p.close(kind, id, False)
                    p.apply(self.policy)
                    return self._out(True, S.PROFILE_OPENED.format(what=what, id=id) if changed else S.PROFILE_NOT_CLOSED.format(what=what, id=id))
                if id in p.closed[kind]:
                    return self._out(True, S.PROFILE_ALREADY_CLOSED.format(what=what, id=id))
                opts = [{"id": "close", "label": S.PROFILE_CLOSE_YES.format(what=what, id=id)}, {"id": "keep", "label": S.PROFILE_CLOSE_NO}]
                i = self.coord.harness.ask_chat(None, key=f"profile:close:{kind}:{id}", text=S.PROFILE_CLOSE_ASK.format(what=what, id=id), options=opts, on_choice=lambda o, k=kind, x=id: self._on_close(k, x, o))
                return self._out(True, i.text_fallback, intent_id=i.id)
        except ProfileError as exc:
            return self._out(False, str(exc))
        return self._out(False, S.PROFILE_BAD_ACTION)

    def _on_close(self, kind: str, ident: str, option: str) -> str:
        if option == "close":
            self.profile.close(kind, ident, True)
            self.profile.apply(self.policy)
            return S.PROFILE_CLOSED_DONE.format(id=ident)
        return S.PROFILE_KEPT.format(id=ident)

    # ---- a task's sensitivity ---------------------------------------------------------------------------------------------------------------

    def _level_or_question(self, ch, text: str, task_type: str, touches, check):
        """(level, None) when the profile settles it, else (None, result dict): the first-run questions, or a question about this task. The model's `touches` only says which of the owner's categories the text
        concerns; it never gives a level."""
        needed = [f for f in ("confidential", "default_sensitivity") if f in self.profile.missing()]
        if needed:
            q = self._question(ch)
            return None, self._out(False, S.TASK_NEEDS_PROFILE + (("\n\n" + q) if q else ""))
        level, why = self.profile.level_for(touches)
        if level is not None:
            return level, None
        cats = ", ".join(self.profile.categories())
        opts = [{"id": lv, "label": lv} for lv in LEVELS]
        key = f"profile:level:{hashlib.sha256((task_type + text).encode()).hexdigest()[:10]}"
        i = self.coord.harness.ask_chat(None, key=key, text=S.TASK_ASK_LEVEL.format(why=S.LEVEL_WHY.get(why, why), categories=cats), options=opts,
                                        on_choice=lambda o, c=ch, t=text, tt=task_type, k=check: self._after_level(c, t, tt, o, k))
        return None, self._out(True, i.text_fallback, intent_id=i.id, question=True)

    def _after_level(self, ch, text: str, task_type: str, level: str, check) -> str:
        return self._create(ch, text=text, task_type=task_type, sensitivity=level, check=check)["text"]

    # ---- the harness's proposal ---------------------------------------------------------------------------------------------------------------

    def _proposal(self) -> tuple[str, str]:
        """(text, hash): what is available on the owner's machines now and what it would take to enable the rest, from the catalog crossed with what is detected. Presence only; nothing is measured or run."""
        types = sorted({t for e in self.envs.values() for t in e.get("task_types", [])})
        usable: dict[tuple[str, str, str], set[str]] = {}
        needs: dict[tuple[str, str], tuple[str, set[str]]] = {}
        for tt in types:
            probe = Task(task_id="proposal", task_type=tt, sensitivity="public", entry_chat=EntryChat(kind="local", name="proposal"), created_by="OWNER", payload_ref="proposal")
            detail: dict = {}
            cands, left = build_candidates(probe, self.envs, self.facts, self.policy, detail)
            for c in cands:
                usable.setdefault((c.route.environment_id, c.route.access_id, c.route.machine_id), set()).add(tt)
            have = {(c.route.environment_id, c.route.machine_id) for c in cands}
            for tag, why in left.items():
                env, _, rest = tag.partition("@")
                env = env.split(" via ")[0]
                mid = rest if rest else ""
                if tag.count("@") == 1 and (env, mid) not in have and why in ("not_installed", "server_not_running", "not_signed_in", "no_access", "sandbox_missing", "automated_use_unreviewed"):
                    reason = RS.REASONS.get(why, why) if why != "no_access" or not detail.get(tag) else RS.ACCESS_MISSING.format(names=", ".join(detail[tag]["missing"]))
                    needs.setdefault((env, mid), (reason, set()))[1].add(tt)
        lines = [S.PROPOSAL_HEAD]
        if not self.coord.registry.all():
            return S.PROPOSAL_NO_MACHINES, "none"
        for (env, acc, mid), tts in sorted(usable.items()):
            e = self.envs[env]
            lines.append(S.PROPOSAL_USE.format(env=env, access=acc, machine=self._name(mid), types=", ".join(sorted(tts)), where="cloud" if E.is_cloud(e, acc) else "local"))
        if len(lines) == 1:
            lines.append(S.PROPOSAL_NOTHING)
        todo = [(k, v) for k, v in sorted(needs.items()) if not any(k[0] == u[0] and k[1] == u[2] for u in usable)]
        if todo:
            lines.append(S.PROPOSAL_NEEDS_HEAD)
            lines += [S.PROPOSAL_NEEDS.format(env=k[0], machine=self._name(k[1]), types=", ".join(sorted(v[1])), reason=v[0]) for k, v in todo]
        closed = {k: v for k, v in self.profile.closed.items() if v}
        if closed:
            lines.append(S.PROFILE_CLOSED.format(closed="; ".join(f"{k}: {', '.join(v)}" for k, v in closed.items())))
        text = "\n".join(lines)
        return text, hashlib.sha256(text.encode()).hexdigest()[:12]

    def proposal_text(self, ch=None) -> str:
        """The proposal AND the one question that goes with it (use it as it is, or change something by talking). Asked again only when the proposal changed."""
        text, h = self._proposal()
        if h == "none":
            return text
        i = self.coord.harness.ask_chat(None, key=f"proposal:{h}", text=text + "\n" + S.PROPOSAL_ASK, options=[{"id": "ok", "label": S.PROPOSAL_OK}, {"id": "change", "label": S.PROPOSAL_CHANGE}],
                                        on_choice=lambda o, hh=h: self._on_proposal(hh, o))
        return i.text_fallback

    def _on_proposal(self, h: str, option: str) -> str:
        self.coord.log.add("proposal_answered", {"hash": h, "option": option, "tag": "OWNER"}, owner=True)
        return S.PROPOSAL_ACCEPTED if option == "ok" else S.PROPOSAL_CHANGE_HOW

    def propose(self, ch) -> dict:
        return self._out(True, self.proposal_text(ch))

    # ---- cancelling a run -------------------------------------------------------------------------------------------------------------------------

    def cancel(self, ch, *, run_id: str) -> dict:
        ok, why = self.coord.runs.cancel(str(run_id)[:40], by="owner")
        return self._out(ok, S.CANCELLED.format(run_id=run_id) if ok else S.CANCEL_REFUSED.format(run_id=run_id, why=why))
