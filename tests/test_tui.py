"""Interactive terminal (mas with no arguments on a TTY, and `mas preview`).

SIMULATED-LOCAL: fake computers and a fake screen; nothing here touches
this computer, the network or any service.
Held-out case (written before the fix): the old-IP startup error must
end in a plain-language screen with a next action.
"""
import re

import pytest

from envrec_mas import tui

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(state, width=80, env=None):
    return ANSI.sub("", tui.render(state, width=width, env=env or {"NO_COLOR": "1"}))


COMPUTERS = [
    tui.Computer("This Mac", "coordinator", "192.168.100.20", True, is_this=True, node_id="this", system="macOS"),
    tui.Computer("Lenovo", "node", "192.168.100.31", True, node_id="lenovo1", system="Windows"),
    tui.Computer("Old PC", "node", "192.168.100.32", False, node_id="oldpc1", system="Linux"),
]


def state(**kw):
    return tui.make_state(COMPUTERS, coordinator_running=True, this_name="This Mac",
                          this_role="coordinator", this_address="192.168.100.20", **kw)


def keys(st, *ks):
    for k in ks:
        tui.feed_key(st, k)
    return st


def test_home_answers_running_this_computer_and_connected_within_one_screen():
    text = plain(state())
    assert "Coordinator:" in text and "Running" in text and "This computer:" in text
    assert "2 of 3 online" in text and "What to do next:" in text and "q leave" in text


def test_coordinator_down_is_a_word_not_just_a_color():
    st = tui.make_state(COMPUTERS, coordinator_running=False, this_name="This Mac",
                        this_role="coordinator", this_address="192.168.100.20")
    assert "Not running" in plain(st)


def test_switch_computer_takes_at_most_three_keys_and_back_is_obvious():
    st = keys(state(), "enter")  # open Computers (cursor starts there)
    assert st.screen == "computers"
    st = keys(st, "down", "enter")  # 2 more keys: mark Lenovo, open it
    assert st.screen == "detail"
    st = keys(st, "enter")  # use as target for the next action
    assert st.target_id == "lenovo1" and "target for the next action" in plain(st)
    assert "Esc back" in plain(st)


def test_refresh_keeps_the_selection():
    st = keys(state(), "enter", "down")
    keys(st, "r")
    assert st.selected == 1 and st.refreshes == 1


def test_help_anywhere_explains_screen_enter_and_back():
    st = keys(state(), "enter", "?")
    text = plain(st)
    assert "What you are seeing:" in text and "If you press Enter:" in text and "How to go back:" in text
    keys(st, "esc")
    assert st.screen == "computers"


def test_add_computer_screen_gives_plain_steps_and_the_exact_sentence():
    st = keys(state(), "enter", "down", "down", "down", "enter")
    assert st.screen == "add"
    text = plain(st)
    assert "mas join" in text and 'say exactly: "Add a computer to Multi Auto Setup."' in text


def _forget_question():
    st = keys(state(), "enter", "down", "enter", "down", "down", "enter")
    assert st.screen == "question"
    return st


def test_question_shows_options_recommended_consequence_and_undo_no_bare_yes_no():
    text = plain(_forget_question())
    assert "(recommended)" in text and "What happens:" in text and "Undo:" in text
    assert "It will do: Forget computer Lenovo" in text and "[y/N]" not in text


def test_rejecting_a_write_writes_nothing_and_accepting_runs_only_after_confirmation():
    st = _forget_question()
    keys(st, "enter")  # recommended answer: keep it
    assert st.pending_write is None and "Nothing was changed" in st.last_action
    st = _forget_question()
    keys(st, "down", "enter")  # yes, forget it
    assert st.pending_write is not None and st.pending_write.argv == ["unpair", "lenovo1"] and st.pending_write.action == "Forget computer Lenovo (192.168.100.31)"


def test_progress_shows_done_now_next_and_time_without_a_percentage():
    st = state()
    st.screen = "progress"
    st.progress = tui.Progress("Working", ["Show the plan", "Run the step", "Check the result"], current=1, elapsed_s=12)
    text = plain(st)
    assert "done - Show the plan" in text and "now - Run the step" in text and "next - Check the result" in text
    assert "12 s" in text and "%" not in text and "Ctrl+C stops safely" in text


@pytest.mark.parametrize("raw,fragment", [
    ("could not listen on 192.168.1.106:7443: [Errno 49] Can't assign requested address", "address has changed"),
    ("listen tcp :7443: bind: address already in use", "already busy"),
    ("the coordinator service is not installed", "not installed yet"),
    ("The coordinator did not answer. Is it running?", "not answering"),
])
def test_error_translation_known_cases(raw, fragment):
    info = tui.translate_error(raw)
    assert fragment in info.headline and info.next_action.startswith("Next:")


def test_error_screen_hides_raw_detail_until_d_is_pressed():
    st = tui.error_state("could not listen on 192.168.1.106:7443: [Errno 49] Can't assign requested address")
    assert "Errno 49" not in plain(st)
    keys(st, "d")
    assert "Errno 49" in plain(st)


def test_no_color_and_80_columns_lose_zero_information():
    st = keys(state(), "enter")
    colored = tui.render(st, width=80, env={"TERM": "xterm-256color", "COLORTERM": "truecolor"})
    plain_text = plain(st, width=80)
    assert "\x1b" not in plain_text
    for word in ("Lenovo", "Online", "Offline", "target for the next action", "Esc back"):
        assert word in plain_text and word in ANSI.sub("", colored)
    assert all(len(ln) <= 80 for ln in plain_text.split("\n"))


def test_preview_forget_never_writes():
    written = []
    st = state(preview=True)
    ports = tui.Ports(snapshot=lambda: st, read_key=lambda: "q", write=lambda s: None,
                      run_write=lambda q: written.append(q) or (True, "x"), open_setup=lambda: "")
    keys(st, "enter", "down", "enter", "down", "down", "enter", "down", "enter")
    assert st.pending_write is not None and st.pending_write.argv == ["unpair", "lenovo1"]
    tui.run_tui(ports, width=80, env={"NO_COLOR": "1"})
    assert written == [] and st.last_action.startswith("Preview only")


def _line_start_columns(data: str):
    """Start column of every printed line, under raw-terminal rules (a bare
    newline moves down only; it does not return to column 0)."""
    starts, col, at_line_start = [], 0, True
    i = 0
    while i < len(data):
        if data[i] == "\x1b":
            m = ANSI.match(data, i)
            seq = m.group(0) if m else data[i]
            if seq.endswith("H"):
                col = 0
            i += len(seq)
            continue
        ch = data[i]
        if ch == "\r":
            col = 0
        elif ch == "\n":
            at_line_start = True
        else:
            if at_line_start:
                starts.append(col)
                at_line_start = False
            col += 1
        i += 1
    return starts


def test_frame_bytes_home_the_cursor_and_start_every_line_at_column_zero():
    """Regression (owner screenshot): frames joined with bare newlines in raw
    mode staircase to the right, each line starting where the last one ended."""
    st = state()
    frames = []
    ports = tui.Ports(snapshot=lambda: st, read_key=iter(["down", "q"]).__next__, write=frames.append,
                      run_write=lambda q: (True, "x"), open_setup=lambda: "")
    tui.run_tui(ports, width=80, env={"NO_COLOR": "1"})
    data = "".join(frames)
    plain_lines = [ln for ln in ANSI.sub("", data).split("\n") if ln.strip()]
    starts = _line_start_columns(data)
    assert len(starts) == len(plain_lines)
    assert starts == [0] * len(starts)  # every line's first byte lands at column 0


def test_tty_render_uses_token_colors_and_no_color_keeps_the_same_words():
    st = keys(state(), "enter")
    colored = tui.render(st, width=80, env={"TERM": "xterm-256color", "COLORTERM": "truecolor"})
    assert "\x1b[38;2;45;212;176m" in colored  # teal on dark (#2dd4b0) from tokens, on Online
    assert "\x1b[38;2;245;161;90m" in colored  # amber on dark (#f5a15a) from tokens, on Offline
    nocolor = tui.render(st, width=80, env={"NO_COLOR": "1"})
    assert "\x1b" not in nocolor

    def content(text):  # the screen below the banner (banner glyphs legitimately differ: color vs shade)
        lines = ANSI.sub("", text).split("\n")
        return lines[next(i for i, ln in enumerate(lines) if ln.strip() == "Computers"):]

    assert content(colored) == content(nocolor)
    assert "Your computers, working as a team." in ANSI.sub("", colored)


def test_banner_bytes_never_leak_a_color_suffix_and_fit_80_columns():
    """Regression (owner screenshot): _frame sliced lines at 80 raw bytes,
    cutting colour escapes in half; the terminal printed '38;5;69m' as text
    and the logo lost most of its width."""
    out = tui.render(state(), width=80, env={"TERM": "xterm-256color"})
    for m in re.finditer(r"[0-9]+;[0-9;]*m", out):
        assert out[m.start() - 2:m.start()] == "\x1b[", "colour suffix without its ESC[ prefix"
    stripped = ANSI.sub("", out)
    assert "38;5;" not in stripped and "48;5;" not in stripped
    assert all(len(ln) <= 80 for ln in stripped.split("\n"))
    assert "M U L T I" in stripped and "Your computers, working as a team." in stripped


def test_banner_decodes_to_the_mas_grid_without_color():
    from envrec_style import tokens
    out = tui.render(state(), width=80, env={"NO_COLOR": "1"})
    lines = out.split("\n")[:(5 * 2 + 1 + 1) // 2]
    sub = set()
    for tr, ln in enumerate(lines):
        for c, ch in enumerate(ln[2:]):
            if ch in "█▀":
                sub.add((2 * tr, c))
            if ch in "█▄":
                sub.add((2 * tr + 1, c))
    letters = {(r, c) for r in range(5) for c in range(tokens.word_columns())
               if all((r * 2 + i, c * 2 + j) in sub for i in range(2) for j in range(2))}
    assert letters == tokens.letter_pixels()


def test_home_banner_and_coordinator_down_hold_outs():
    down = tui.make_state(COMPUTERS, coordinator_running=False, this_name="This Mac",
                          this_role="coordinator", this_address="192.168.100.20")
    text = plain(down, width=80)
    assert "Your computers, working as a team." in text and "Not running" in text
    assert all(len(ln) <= 80 for ln in text.split("\n"))


def test_queued_escape_keys_are_read_one_at_a_time():
    """Regression (owner pty journeys): when keys queue under real PTY timing,
    the old read ate the bytes after an Esc as part of one bogus key, so
    Esc Esc q vanished and the app sat waiting for input already sent."""
    import os as _os

    from envrec_os.posix import PosixLayer
    layer = PosixLayer("darwin")
    for payload, want in [(b"\x1b\x1bq", ["esc", "esc", "q"]), (b"\x1b[Bq", ["down", "q"]), (b"\x1bq", ["esc", "q"])]:
        r, w = _os.pipe()
        _os.write(w, payload)
        _os.close(w)
        with _os.fdopen(r, "r") as stream:
            got = [tui.parse_key(layer.read_key(stream)) for _ in want]
        assert got == want, payload


def test_read_key_reports_an_arrow_as_one_key_not_escape_plus_noise():
    """Regression (owner pty journeys): a buffered text read slurps the arrow's
    bytes, the fd then looks empty, and Down was reported as Esc."""
    import os as _os

    from envrec_os.posix import PosixLayer
    layer = PosixLayer("darwin")
    r, w = _os.pipe()
    _os.write(w, b"\x1b[B")
    with _os.fdopen(r, "r") as stream:
        assert layer.read_key(stream) == "\x1b[B"
    r, w = _os.pipe()
    _os.write(w, b"\x1b")
    _os.close(w)
    with _os.fdopen(r, "r") as stream:
        assert layer.read_key(stream) == "\x1b"


def test_coordinator_down_home_names_the_next_step():
    st = tui.make_state(COMPUTERS, coordinator_running=False, this_name="This Mac",
                        this_role="coordinator", this_address="192.168.100.20")
    assert "Next: choose Setup steps" in plain(st)


def test_forgetting_this_computer_is_explained_not_offered():
    st = keys(state(), "enter", "enter", "down", "down", "enter")
    assert st.screen == "computers" and st.question is None
    assert "nothing to forget here" in st.last_action


def test_coordinator_start_with_a_bad_address_is_plain_language(tmp_path, capsys):
    from envrec_mas import cli
    code = cli.main(["--state-dir", str(tmp_path), "coordinator", "start", "--bind", "999.1.1.1"])
    err = capsys.readouterr().err
    assert code == 2 and "That address cannot be used" in err and "Next:" in err and "Technical detail:" in err


def test_held_out_old_ip_error_is_plain_language_with_a_next_action():
    raw = "could not listen on 192.168.1.106:7443: [Errno 49] Can't assign requested address"
    state = tui.error_state(raw)
    text = plain(state)
    assert "address" in text.lower()
    assert "192.168.1.106" not in text.split("Next:")[0]  # headline is not the raw error
    assert "Next:" in text
    assert "d" in text and "details" in text.lower()  # raw detail hidden under a key
    assert tui.translate_error(raw).headline != raw
