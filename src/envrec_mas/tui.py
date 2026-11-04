"""Interactive terminal for `mas` with no arguments (TTY only).

View layer only: it decides nothing for the model and writes nothing
itself. A write is handed to the ports only after the owner confirms a
question that shows the exact action and its undo; the ports run the
command that already exists, and that command asks again itself.
Without a TTY the caller keeps the plain-text path and exit codes.
There is no global "selected computer" in the product: every action
names its computer. The marker here is "target for the next action".
"""

from __future__ import annotations

import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from envrec_style import term, tokens

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ONLINE_AFTER_S = 45.0  # nodes heartbeat every 10 s; 45 s without one is Offline


def sym(sym_id: str) -> str:
    return tokens.symbol_text(sym_id, unicode=False)


# ---- data the screens show -------------------------------------------------


@dataclass
class Computer:
    name: str
    role: str  # coordinator | node
    address: str
    online: bool
    is_this: bool = False
    node_id: str = ""
    fingerprint: str = ""
    system: str = ""


@dataclass
class Question:
    question: str
    options: list[str]
    default: int
    consequence: str
    undo: str
    action: str = ""  # exact action shown before any write
    argv: list[str] = field(default_factory=list)


@dataclass
class Progress:
    title: str
    stages: list[str]
    current: int = 0
    elapsed_s: float = 0.0


@dataclass
class ErrorInfo:
    headline: str
    cause: str
    next_action: str
    detail: str


def translate_error(raw: str) -> ErrorInfo:
    """Known raw failures -> plain headline, likely cause, exact next action."""
    low = raw.lower()
    detail = raw.strip()
    if "assign requested address" in low or "address not available" in low or (
        "could not listen" in low and "operation not permitted" in low
    ):
        return ErrorInfo(
            "This computer's address has changed",
            "The address saved for the coordinator is not on this computer any more (the network gave it a new one).",
            "Next: run `mas service install --role coordinator` and pick this computer's current address, or start it with `mas coordinator start --bind <current address>`.",
            detail,
        )
    if "is not an ip address" in low or "is not allowed: choose a loopback" in low:
        return ErrorInfo(
            "That address cannot be used",
            "The coordinator can only listen on an address that belongs to this computer.",
            "Next: pick one of this computer's own addresses (shown in the setup steps), then try again.",
            detail,
        )
    if "already in use" in low or "eaddrinuse" in low or "port" in low and "busy" in low:
        return ErrorInfo(
            "The port is already busy",
            "Another program (often the coordinator already running) is using this port.",
            "Next: check `mas service status --role coordinator`; if it is running, you do not need to start it again.",
            detail,
        )
    if "not installed" in low:
        return ErrorInfo(
            "The service is not installed yet",
            "Nothing was set up to start by itself on this computer.",
            "Next: open `mas`, choose the setup steps, and install the service step.",
            detail,
        )
    if "not running" in low or "did not answer" in low or "offline" in low or "no paired" in low:
        return ErrorInfo(
            "That computer is not answering",
            "It may be off, asleep, or not connected to the same network.",
            "Next: check the computer is on and on this network, press r to refresh, then try again.",
            detail,
        )
    return ErrorInfo(
        "Something did not work",
        "The reason is in the technical detail below.",
        "Next: press d to see the detail, Esc to go back, then try again.",
        detail,
    )


# ---- screens ---------------------------------------------------------------


@dataclass
class AppState:
    screen: str = "home"  # home|computers|detail|add|help|question|progress|error
    computers: list[Computer] = field(default_factory=list)
    coordinator_running: bool = False
    this_name: str = ""
    this_role: str = ""
    this_address: str = ""
    preview: bool = False
    cursor: int = 0  # row cursor on the current screen
    selected: int = 0  # computer row cursor (kept across refresh)
    target_id: str = ""
    question: Question | None = None
    progress: Progress | None = None
    error: ErrorInfo | None = None
    show_details: bool = False
    help_from: str = "home"
    last_action: str = ""
    pending_write: Question | None = None
    request_setup: bool = False
    dirty: bool = False
    quit: bool = False
    refreshes: int = 0


HOME_ITEMS = ["Computers", "Setup steps", "Help", "Leave"]
DETAIL_ITEMS = ["Use this computer for the next action", "Details", "Forget this computer", "Back"]

HELP_TEXT = {
    "home": ("Home", "You see whether the coordinator is running, which computer this is, and how many computers are connected.",
             "Up/Down move, Enter opens the marked row, r refreshes, q leaves.", "Enter opens what the marked row says.", "Press q to leave."),
    "computers": ("Computers", "Every paired computer, with Online or Offline in words. The target marker is the computer the next action will use; it is chosen per action, there is no global switch.",
                  "Up/Down move, Enter opens a computer, r refreshes, Esc goes back.", "Enter opens the marked computer: use it as the target, see details, add or forget.", "Press Esc to go back home."),
    "detail": ("Computer", "One computer: its name, role, address and whether it is Online or Offline.",
               "Up/Down move, Enter chooses, Esc goes back.", "Enter runs the marked row. A write asks you a full question first.", "Press Esc to go back to Computers."),
    "add": ("Add a computer", "Plain steps to add a computer. Nothing here writes anything.",
            "Read the steps, Esc goes back.", "Enter does nothing here; follow the steps in your chat.", "Press Esc to go back."),
    "help": ("Help", "This help, for the screen you came from.", "Esc goes back.", "Enter does nothing here.", "Press Esc to go back."),
    "question": ("Question", "A question that must be answered before anything is written. The recommended answer is marked.",
                 "Up/Down move, Enter answers, Esc cancels and writes nothing.", "Enter answers with the marked option.", "Press Esc to cancel; nothing is written."),
    "progress": ("Working", "The stages of a long step: done, now and next, with the time it has taken. No percentage is invented.",
                 "Ctrl+C stops safely; the step says what was and was not done.", "Enter does nothing here.", "Wait, or press Ctrl+C to stop safely."),
    "error": ("Problem", "What went wrong in plain words, the likely cause and the exact next action. The raw technical detail stays hidden until you ask for it.",
              "d shows or hides the technical detail, Esc goes back.", "Enter does nothing here.", "Press Esc to go back."),
}


def make_state(computers, *, coordinator_running, this_name, this_role, this_address, preview=False) -> AppState:
    st = AppState(computers=list(computers), coordinator_running=coordinator_running,
                  this_name=this_name, this_role=this_role, this_address=this_address, preview=preview)
    this = next((c for c in st.computers if c.is_this), None)
    if this is not None:
        st.target_id = this.node_id or this.name
    return st


def error_state(raw: str) -> AppState:
    st = AppState(screen="error", error=translate_error(raw))
    return st


def _wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(line)
    return [ln[:width] for ln in out]


def _trim_visible(line: str, width: int) -> str:
    """Cut a line at `width` visible characters. Escape sequences are copied
    whole and never counted or split: slicing raw bytes cut a colour escape
    in half and the terminal printed its suffix ('38;5;69m') as text."""
    out, visible, i = [], 0, 0
    while i < len(line) and visible < width:
        m = ANSI_RE.match(line, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(line[i])
        visible += 1
        i += 1
    return "".join(out)


def _frame(lines: list[str], width: int) -> str:
    return "\n".join(_trim_visible(ln, width) for ln in lines)


_TITLES = ("Home", "Computers", "Question", "Add a computer", "What to do next:")


def _colorize(text: str, level: str) -> str:
    """Token colors on words that already carry a symbol and a word, so
    NO_COLOR loses nothing. Colors come only from tokens.json."""
    if level == "none":
        return text

    def paint(word: str, hex_color: str) -> str:
        return f"{term.sgr(hex_color, level)}{word}{term.RESET}"

    teal = tokens.semantic("teal", dark=True)
    amber = tokens.semantic("amber", dark=True)
    blue = tokens.load()["brand"]["letters"]
    out = []
    for ln in text.split("\n"):
        if ln in _TITLES or ln.startswith(("Help:", "Computer:")):
            ln = paint(ln, blue)
        if ln.startswith("[PREVIEW"):
            ln = paint(ln, amber)
        if ln.startswith("[WARNING]"):
            ln = paint(ln, amber)
        if ln.startswith("This computer: "):
            ln = "This computer: " + paint(ln[len("This computer: "):], teal)
        ln = ln.replace("- Not running", "- " + paint("Not running", amber))
        ln = ln.replace("- Running", "- " + paint("Running", teal))
        ln = ln.replace(" Offline", " " + paint("Offline", amber))
        ln = ln.replace(" Online", " " + paint("Online", teal))
        ln = ln.replace(" done - ", " " + paint("done", teal) + " - ")
        ln = ln.replace(" now - ", " " + paint("now", blue) + " - ")
        out.append(ln)
    return "\n".join(out)


def render(state: AppState, *, width: int = 80, env: dict | None = None) -> str:
    width = max(20, width)
    env = {} if env is None else env
    L: list[str] = []
    L += term.render(columns=width, tty=True, env=env).split("\n")
    if state.preview:
        L.append("[PREVIEW - nothing will be changed]")
    screen = state.screen
    if screen == "home":
        run_word = "Running" if state.coordinator_running else "Not running"
        run_sym = sym("running") if state.coordinator_running else sym("warning")
        L.append(f"Home")
        L.append(f"Coordinator: {run_sym} - {run_word}")
        L.append(f"This computer: {state.this_name} - {state.this_role} - {state.this_address}")
        online = sum(1 for c in state.computers if c.online)
        L.append(f"Computers connected: {online} of {len(state.computers)} online")
        if not state.coordinator_running:
            L.append("Next: choose Setup steps to start it, or Computers to see who is paired.")
        if state.last_action:
            L += _wrap(state.last_action, width)
        L.append("What to do next:")
        for i, item in enumerate(HOME_ITEMS):
            L.append(f"{'>' if i == state.cursor else ' '} {item}")
        L.append("Keys: Up/Down move, Enter open, r refresh, ? help, q leave")
    elif screen == "computers":
        L.append("Computers")
        L.append("The target is chosen for the next action. There is no global switch.")
        for i, c in enumerate(state.computers):
            word = "Online" if c.online else "Offline"
            mark = " [TARGET] target for the next action" if (c.node_id or c.name) == state.target_id else ""
            here = " (this computer)" if c.is_this else ""
            row = f"{'>' if i == state.selected else ' '} {c.name}{here} - {c.role} - {c.address} - {word}"
            if mark and len(row) + len(mark) > width:
                L.append(row)
                L.append(f"  {mark.strip()}")
            else:
                L.append(row + mark)
        L.append(f"{'>' if state.selected == len(state.computers) else ' '} Add a computer")
        if state.last_action:
            L += _wrap(state.last_action, width)
        L.append("Keys: Up/Down move, Enter open, r refresh, Esc back, ? help")
    elif screen == "detail":
        c = state.computers[state.selected]
        word = "Online" if c.online else "Offline"
        L.append(f"Computer: {c.name}")
        L.append(f"Role: {c.role}   Address: {c.address}   Status: {word}")
        if state.show_details:
            L.append(f"System: {c.system or 'not reported yet'}")
            L.append(f"Fingerprint: {c.fingerprint or 'not shown'}")
            L.append("Forget removes trust in this computer. Pairing again undoes it.")
        for i, item in enumerate(DETAIL_ITEMS):
            L.append(f"{'>' if i == state.cursor else ' '} {item}")
        L.append("Keys: Up/Down move, Enter choose, Esc back, ? help")
    elif screen == "add":
        L.append("Add a computer")
        L.append("1. On the other computer, install Multi Auto Setup and run: mas join")
        L.append("2. On this computer, run: mas pair")
        L.append('3. In your chat, say exactly: "Add a computer to Multi Auto Setup."')
        L.append("The chat shows the plan. Nothing is paired until you approve it there and type the code.")
        L.append("Keys: Esc back, ? help")
    elif screen == "help":
        title, seeing, press, enter, back = HELP_TEXT.get(state.help_from, HELP_TEXT["home"])
        L.append(f"Help: {title}")
        L += _wrap(f"What you are seeing: {seeing}", width)
        L += _wrap(f"What you can press: {press}", width)
        L += _wrap(f"If you press Enter: {enter}", width)
        L += _wrap(f"How to go back: {back}", width)
        L.append("Keys: Esc back")
    elif screen == "question" and state.question:
        q = state.question
        L.append("Question")
        L += _wrap(q.question, width)
        L.append(f"It will do: {q.action}")
        for i, opt in enumerate(q.options):
            rec = " (recommended)" if i == q.default else ""
            L.append(f"{'>' if i == state.cursor else ' '} {opt}{rec}")
        L += _wrap(f"What happens: {q.consequence}", width)
        L += _wrap(f"Undo: {q.undo}", width)
        L.append("Keys: Up/Down move, Enter answer, Esc cancel (nothing is written)")
    elif screen == "progress" and state.progress:
        pr = state.progress
        L.append(pr.title)
        for i, stage in enumerate(pr.stages):
            if i < pr.current:
                L.append(f"{sym('done')} done - {stage}")
            elif i == pr.current:
                L.append(f"{sym('running')} now - {stage}")
            else:
                L.append(f"{sym('waiting')} next - {stage}")
        L.append(f"Time so far: {int(pr.elapsed_s)} s")
        L.append("Ctrl+C stops safely. Nothing is written before its step.")
    elif screen == "error" and state.error:
        e = state.error
        L.append(f"{sym('warning')} {e.headline}")
        L += _wrap(f"Likely cause: {e.cause}", width)
        L += _wrap(e.next_action, width)
        if state.show_details:
            L += _wrap(f"Technical detail: {e.detail}", width)
        L.append("Keys: d technical details, Esc back, ? help")
    return _colorize(_frame(L, width), term.color_level(env, tty=True))


# ---- keys --------------------------------------------------------------------


def feed_key(state: AppState, key: str) -> AppState:
    if key == "ctrl_c":
        state.last_action = "Stopped safely. Nothing was written by this screen."
        state.screen = "home"
        return state
    if key == "?" and state.screen != "help":
        state.help_from = state.screen
        state.screen = "help"
        return state
    if state.screen == "help":
        if key in ("esc", "enter", "q"):
            state.screen = state.help_from if state.help_from != "help" else "home"
        return state
    if state.screen == "error":
        if key == "d":
            state.show_details = not state.show_details
        elif key in ("esc", "enter", "q"):
            state.screen = "home"
        return state
    if key == "q":
        state.quit = True
        return state
    if key == "r":
        state.dirty = True
        state.refreshes += 1
        return state
    if state.screen == "home":
        if key == "up":
            state.cursor = max(0, state.cursor - 1)
        elif key == "down":
            state.cursor = min(len(HOME_ITEMS) - 1, state.cursor + 1)
        elif key == "enter":
            item = HOME_ITEMS[state.cursor]
            if item == "Computers":
                state.screen = "computers"
            elif item == "Setup steps":
                state.request_setup = True
            elif item == "Help":
                state.help_from = "home"
                state.screen = "help"
            elif item == "Leave":
                state.quit = True
    elif state.screen == "computers":
        rows = len(state.computers) + 1
        if key == "up":
            state.selected = max(0, state.selected - 1)
        elif key == "down":
            state.selected = min(rows - 1, state.selected + 1)
        elif key == "esc":
            state.screen = "home"
        elif key == "enter":
            if state.selected == len(state.computers):
                state.screen = "add"
            else:
                state.cursor = 0
                state.show_details = False
                state.screen = "detail"
    elif state.screen == "add":
        if key == "esc":
            state.screen = "computers"
    elif state.screen == "detail":
        c = state.computers[state.selected]
        if key == "up":
            state.cursor = max(0, state.cursor - 1)
        elif key == "down":
            state.cursor = min(len(DETAIL_ITEMS) - 1, state.cursor + 1)
        elif key == "esc":
            state.screen = "computers"
        elif key == "enter":
            item = DETAIL_ITEMS[state.cursor]
            if item == "Back":
                state.screen = "computers"
            elif item == "Details":
                state.show_details = not state.show_details
            elif item.startswith("Use this computer"):
                state.target_id = c.node_id or c.name
                state.last_action = f"{c.name} is now the target for the next action. Nothing has changed yet; each action still shows its plan and asks you first."
                state.screen = "computers"
            elif item.startswith("Forget") and c.is_this:
                state.last_action = "This is this computer, so there is nothing to forget here. To stop it starting by itself, use the setup steps and remove the service."
                state.screen = "computers"
            elif item.startswith("Forget"):
                state.question = Question(
                    question=f"Forget {c.name}?",
                    options=["No, keep it (recommended)", "Yes, forget it"],
                    default=0,
                    consequence="This computer will no longer be trusted and cannot connect until it is paired again.",
                    undo="Pair it again: on that computer run mas join, here run mas pair.",
                    action=f"Forget computer {c.name} ({c.address})",
                    argv=["unpair", c.node_id] if c.node_id and not c.is_this else ["forget"],
                )
                state.cursor = 0
                state.screen = "question"
    elif state.screen == "question" and state.question:
        q = state.question
        if key == "up":
            state.cursor = max(0, state.cursor - 1)
        elif key == "down":
            state.cursor = min(len(q.options) - 1, state.cursor + 1)
        elif key == "esc":
            state.last_action = "Cancelled. Nothing was changed."
            state.question = None
            state.screen = "detail" if state.computers else "home"
        elif key == "enter":
            if state.cursor == q.default:
                state.last_action = "Kept. Nothing was changed."
                state.question = None
                state.screen = "computers"
            else:
                state.pending_write = q
                state.question = None
    return state


def parse_key(data: str) -> str:
    return {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
            "\r": "enter", "\n": "enter", "\x03": "ctrl_c", "\x1b": "esc"}.get(data, data)


# ---- running on a real terminal ----------------------------------------------


@dataclass
class Ports:
    snapshot: object  # () -> AppState
    read_key: object  # () -> str
    write: object  # (str) -> None
    run_write: object  # (Question) -> (bool, str)
    open_setup: object  # () -> str (message)


def _emit(ports: Ports, text: str) -> None:
    # The terminal runs in raw mode, where a bare newline does not return
    # to column 0: every line break homes the cursor first.
    ports.write(text.replace("\n", "\r\n"))


def run_tui(ports: Ports, *, width: int = 80, env: dict | None = None) -> int:
    state: AppState = ports.snapshot()
    while not state.quit:
        _emit(ports, "\x1b[2J\x1b[H" + render(state, width=width, env=env) + "\n")
        key = ports.read_key()
        feed_key(state, key)
        if state.request_setup:
            state.request_setup = False
            state.last_action = ports.open_setup()
        if state.pending_write is not None:
            q, state.pending_write = state.pending_write, None
            if state.preview:
                state.last_action = f"Preview only: nothing was changed. (Would ask the existing command to run: mas {' '.join(q.argv)})"
                state.screen = "computers"
            else:
                state.progress = Progress("Working", ["Show the plan", "Wait for your answer", "Run the step", "Check the result"], current=2, elapsed_s=0.0)
                state.screen = "progress"
                _emit(ports, "\x1b[2J\x1b[H" + render(state, width=width, env=env) + "\n")
                t0 = time.monotonic()
                ok, msg = ports.run_write(q)
                state.progress.current = 3
                state.progress.elapsed_s = time.monotonic() - t0
                state.last_action = msg
                state.screen = "computers"
                state.dirty = True
        if state.dirty:
            state.dirty = False
            fresh: AppState = ports.snapshot()
            fresh.screen, fresh.cursor, fresh.selected, fresh.target_id = state.screen, state.cursor, state.selected, state.target_id
            fresh.last_action, fresh.refreshes = state.last_action, state.refreshes
            state = fresh
    _emit(ports, "\nLeft. Run `mas` again any time.\n")
    return 0


_LAYER = None


def _read_key(stream) -> str:
    global _LAYER
    import envrec_os
    if _LAYER is None:
        _LAYER = envrec_os.current()  # one layer, so a kept byte survives between keys
    return parse_key(_LAYER.read_key(stream))


def _terminal_ports(state_dir: Path, state_args: list[str], *, preview: bool) -> Ports:
    if preview:
        fake = [
            Computer("This Mac", "coordinator", "192.168.100.20", True, is_this=True, node_id="this", system="macOS"),
            Computer("Lenovo", "node", "192.168.100.31", True, node_id="lenovo1", system="Windows"),
            Computer("Old PC", "node", "192.168.100.32", False, node_id="oldpc1", system="Linux"),
        ]
        return Ports(
            snapshot=lambda: make_state(fake, coordinator_running=True, this_name="This Mac", this_role="coordinator", this_address="192.168.100.20", preview=True),
            read_key=lambda: _read_key(sys.stdin),
            write=lambda s: sys.stdout.write(s) or sys.stdout.flush(),
            run_write=lambda q: (False, "Preview only: nothing was changed."),
            open_setup=lambda: "Preview only: the setup steps would open here. Nothing was changed.",
        )
    from envrec_coordinator.registry import Registry
    from envrec_mas import menu as menu_mod
    import envrec_os

    base = menu_mod.real_ports(state_dir, state_args)

    def snapshot() -> AppState:
        computers: list[Computer] = []
        addresses = base.addresses()
        this_addr = addresses[0][1] if addresses else "no address found"
        role = base.detect_role() or "not set up yet"
        computers.append(Computer(socket.gethostname() or "This computer", role, this_addr, True, is_this=True, node_id="this"))
        running = False
        try:
            reg = Registry(state_dir / "coordinator", envrec_os.current())
            now = time.time()
            for n in reg.all():
                online = n.last_heartbeat is not None and (now - n.last_heartbeat) < ONLINE_AFTER_S
                running = running or online
                computers.append(Computer(n.name, "node", "", online, node_id=n.node_id, fingerprint=n.fingerprint, system=f"{n.os}/{n.arch}"))
        except Exception:
            pass
        try:
            from envrec_coordinator.control import control_dir, read_json
            running = running or read_json(control_dir(state_dir / "coordinator") / "runs.json") is not None
        except Exception:
            pass
        return make_state(computers, coordinator_running=running, this_name=computers[0].name, this_role=role, this_address=this_addr)

    def run_write(q: Question):
        code = base.run_cli(list(q.argv))
        return (code == 0, "Done. " + q.action + "." if code == 0 else f"That step did not finish (code {code}). Nothing else was changed.")

    def open_setup() -> str:
        code = menu_mod.run_menu(base)
        return "Setup steps closed." if code == 0 else "Setup steps closed."

    return Ports(snapshot=snapshot, read_key=lambda: _read_key(sys.stdin),
                 write=lambda s: sys.stdout.write(s) or sys.stdout.flush(), run_write=run_write, open_setup=open_setup)


def _run_on_terminal(state_dir: Path, state_args: list[str], *, preview: bool, stdin: TextIO, stdout: TextIO) -> int:
    if not (stdin.isatty() and stdout.isatty()):
        if preview:  # plain, scriptable snapshot of the fake screens; still no writes
            ports = _terminal_ports(state_dir, state_args, preview=True)
            stdout.write(render(ports.snapshot(), width=80, env={"NO_COLOR": "1"}) + "\n")
        return 2
    import envrec_os
    ports = _terminal_ports(state_dir, state_args, preview=preview)
    try:
        with envrec_os.current().raw_terminal(stdin.fileno()):
            return run_tui(ports, width=_columns(stdout), env=dict(os.environ))
    except (KeyboardInterrupt, EOFError):
        stdout.write("\nStopped safely. Nothing was written by this screen.\n")
        return 0
    except Exception:
        return 2  # no usable interactive terminal: the caller falls back


def _columns(stdout: TextIO) -> int:
    import shutil
    return shutil.get_terminal_size((80, 24)).columns


def main_for_terminal(state: Path, state_args: list[str], *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return _run_on_terminal(state, state_args, preview=False, stdin=stdin or sys.stdin, stdout=stdout or sys.stdout)


def main_preview(*, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return _run_on_terminal(Path("."), [], preview=True, stdin=stdin or sys.stdin, stdout=stdout or sys.stdout)
