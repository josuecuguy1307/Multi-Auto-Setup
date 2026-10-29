"""Closed vocabularies. Values are the strings that appear in JSON."""

from __future__ import annotations

from enum import StrEnum


class TargetKind(StrEnum):
    host = "host"
    usb = "usb"
    serial = "serial"
    ble = "ble"
    net_host = "net_host"
    instrument = "instrument"
    signal = "signal"
    app = "app"
    repo = "repo"
    sensor = "sensor"
    phone = "phone"
    remote = "remote"  # a computer the owner enrolled in targets.yaml, reached over a transport (the connect protocol's target)
    door = "door"  # this computer as the way in for a computer that is not enrolled yet: key, one-time link, pairing, a proposal (enrolled as a `local` entry with scope door)
    pairing = "pairing"  # the coordinator's pairing of a new node: a code, a key exchange, the owner's yes (protocol-pair; walked by code, not by a model)
    node = "node"  # a node's own connection to its coordinator: card, connect, heartbeat, reconnect (protocol-node-connect)
    media = "media"  # this computer's own Bluetooth, audio output and media apps, enrolled in targets.yaml as a `local` entry
    scope = "scope"  # the candidates a goal covers: the model picks one of them (protocol-choose)
    unknown = "unknown"


class FactStatus(StrEnum):
    unknown = "unknown"
    hypothesis = "hypothesis"
    tested = "tested"
    verified = "verified"
    contradicted = "contradicted"


# Order used when merging statuses that agree. contradicted is handled separately.
STATUS_RANK = {
    FactStatus.unknown: 0,
    FactStatus.hypothesis: 1,
    FactStatus.tested: 2,
    FactStatus.verified: 3,
}


class Tier(StrEnum):
    observe = "observe"
    read = "read"
    write_logged = "write_logged"
    write_approval = "write_approval"


class Origin(StrEnum):
    builtin = "builtin"
    adapter = "adapter"
    generated = "generated"
    skill = "skill"


class ActionType(StrEnum):
    call_tool = "call_tool"
    extend_to = "extend_to"
    expand_fact = "expand_fact"
    research = "research"
    propose_skill = "propose_skill"
    record_unknown = "record_unknown"
    ask_human = "ask_human"
    advance = "advance"
    done = "done"


class Decision(StrEnum):
    allow = "allow"
    allow_logged = "allow_logged"
    needs_approval = "needs_approval"
    deny = "deny"


class Actor(StrEnum):
    loop = "loop"
    worker = "worker"
    policy = "policy"
    verifier = "verifier"
    human = "human"


class CreatedBy(StrEnum):
    probe = "probe"
    adapter = "adapter"
    model = "model"
    human = "human"
    verifier = "verifier"
    loop = "loop"


class RingDecision(StrEnum):
    deepen = "deepen"
    extend = "extend"
    stop = "stop"


class RingPolicy(StrEnum):
    fixed_order = "fixed_order"
    model_decides = "model_decides"


class ModelKind(StrEnum):
    openai_compat = "openai_compat"
    claude_cli = "claude_cli"
    scripted = "scripted"


class HypothesisOutcome(StrEnum):
    pending = "pending"
    confirmed = "confirmed"
    contradicted = "contradicted"
