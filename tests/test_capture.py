"""Tasks 7.3 / 7.4: session-log adapters and the screen capture (design D12), on the task-3.x samples."""

import json
from pathlib import Path

import pytest

from fakeclient import append_jsonl, iso
from polyagent import kinds as K
from polyagent.capture import ClaudeLog, CodexLog, CopilotLog, Outcome, ScreenCapture, locate, prompt_digest

LOGS = Path(__file__).parent / "fixtures" / "logs"
SCREENS = Path(__file__).parent / "fixtures" / "screens"


def lines(rel):
    return (LOGS / rel).read_text().splitlines(keepends=True)


def copy(tmp_path, rel, upto=None):
    p = tmp_path / Path(rel).name
    p.write_text("".join(lines(rel)[:upto]))
    return p


def user_texts(rel, pred):
    out = []
    for i, line in enumerate(lines(rel)):
        e = json.loads(line)
        if pred(e):
            out.append(i)
    return out


# ---- copilot -----------------------------------------------------------------------


def copilot_prompt(rel, index):
    return json.loads(lines(rel)[index])["data"]["content"]


def test_copilot_single_and_multistep_turns(tmp_path):
    """Scenario: 多步工具调用的回合 — the final message, not the progress notes."""
    rel = "copilot/readonly-pong-read-denied.events.jsonl"
    log = CopilotLog(str(copy(tmp_path, rel)))
    j = log.judge(0, prompt_digest(copilot_prompt(rel, 2)))
    assert (j.outcome, j.text) == (Outcome.REPLY, "PONG")
    j = log.judge(8, prompt_digest(copilot_prompt(rel, 8)))
    assert j.outcome is Outcome.REPLY and j.text == "The secret word is **BANANA-42**."
    j = log.judge(20, prompt_digest(copilot_prompt(rel, 20)))
    assert j.text.startswith("1) shell denied")
    assert not log.turn_open()


def test_copilot_events_after_the_end_marker_and_the_mark(tmp_path):
    rel = "copilot/readonly-pong-read-denied.events.jsonl"
    log = CopilotLog(str(copy(tmp_path, rel)))
    assert log.judge(9, prompt_digest(copilot_prompt(rel, 8))).outcome is Outcome.NOT_SEEN  # before the mark
    partial = CopilotLog(str(copy(tmp_path, rel, upto=12)))
    assert partial.judge(8, prompt_digest(copilot_prompt(rel, 8))).outcome is Outcome.IN_PROGRESS
    assert partial.turn_open()


def test_copilot_agent_error(tmp_path):
    """Scenario: agent 级错误."""
    rel = "copilot/auth-error.events.jsonl"
    j = CopilotLog(str(copy(tmp_path, rel))).judge(0, prompt_digest(copilot_prompt(rel, 2)))
    assert j.outcome is Outcome.ERROR and "Authorization error" in j.text


def test_copilot_refusal_without_final_message(tmp_path):
    """Scenario: 拒绝导致回合中止 — an end marker without a final message."""
    rel = "copilot/permission-dialog-persisted.events.jsonl"
    j = CopilotLog(str(copy(tmp_path, rel))).judge(0, prompt_digest(copilot_prompt(rel, 2)))
    assert j.outcome is Outcome.ABORTED


def test_copilot_guide_line_prompt_matches_the_full_text(tmp_path):
    rel = "copilot/fullauto-guideline-tab-bang.events.jsonl"
    j = CopilotLog(str(copy(tmp_path, rel))).judge(0, prompt_digest(f"{K.GUIDE_LINE_TEXT}\n!touch fa-gl-bang.txt"))
    assert j.outcome is Outcome.REPLY


def test_copilot_sub_agent_events_and_notification_turns_are_not_replies(tmp_path):
    """Scenario: 无法禁用的后台命令 (and the background sub-agent): the reply is this turn's final
    message; the sub-agent's own user/assistant events and the notification turns belong to no prompt."""
    rel = "copilot/fullauto-background-task-async-shell-late-turns.events.jsonl"
    log = CopilotLog(str(copy(tmp_path, rel)))
    assert log.judge(0, prompt_digest(copilot_prompt(rel, 3))).text == "LAUNCHED"
    assert log.judge(0, prompt_digest("Reply with the word SUBDONE")).outcome is Outcome.NOT_SEEN  # sub-agent's
    assert log.judge(33, prompt_digest(copilot_prompt(rel, 34))).text == "STARTED"
    rel2 = "copilot/fullauto-excluded-tools-async-shell-late-turn.events.jsonl"
    assert CopilotLog(str(copy(tmp_path, rel2))).judge(8, prompt_digest(copilot_prompt(rel2, 9))).text == "STARTED"


def test_copilot_prompt_received_during_a_notification_turn_is_ambiguous(tmp_path):
    """Scenario: prompt 与自行开始的回合交错 (spliced log)."""
    rel = "copilot/fullauto-background-task-async-shell-late-turns.events.jsonl"
    p = copy(tmp_path, rel, upto=28)  # the notification turn (from line 25) has not ended
    append_jsonl(p,
                 {"type": "user.message", "id": "u9", "timestamp": iso(), "data": {"content": "Reply with PONG9"}},
                 {"type": "assistant.message", "id": "a9", "timestamp": iso(), "data": {"content": "PONG9", "toolRequests": []}},
                 {"type": "session.usage_checkpoint", "id": "c9", "timestamp": iso(), "data": {}})
    j = CopilotLog(str(p)).judge(28, prompt_digest("Reply with PONG9"))
    assert j.outcome is Outcome.AMBIGUOUS


def test_copilot_turn_open_for_notification_turns(tmp_path):
    """D11: an open notification turn counts as working; a turn-less notification only for 10 s."""
    rel = "copilot/fullauto-background-task-async-shell-late-turns.events.jsonl"
    assert CopilotLog(str(copy(tmp_path, rel, upto=28))).turn_open()  # activity after the notification
    p = copy(tmp_path, rel, upto=46)  # ends right after the second notification line (index 46)
    p.write_text("".join(lines(rel)[:46]))
    now = 1_900_000_000.0
    append_jsonl(p, {"type": "system.notification", "timestamp": iso(now), "data": {"content": "done"}})
    assert CopilotLog(str(p)).turn_open(now=now + 2)
    assert not CopilotLog(str(p)).turn_open(now=now + 11)
    assert not CopilotLog(str(copy(tmp_path, rel))).turn_open()


def test_copilot_missing_log_means_no_turn(tmp_path):
    log = CopilotLog(str(tmp_path / "nope" / "events.jsonl"))
    assert log.mark() == 0 and not log.turn_open()
    assert log.judge(0, prompt_digest("x")).outcome is Outcome.NOT_SEEN


def test_partial_last_line_is_not_parsed(tmp_path):
    rel = "copilot/readonly-pong-read-denied.events.jsonl"
    p = copy(tmp_path, rel, upto=6)
    with open(p, "a") as f:
        f.write(lines(rel)[6][:20])  # half-written line
    log = CopilotLog(str(p))
    assert log.mark() == 6
    with open(p, "a") as f:
        f.write(lines(rel)[6][20:] + lines(rel)[7])
    assert log.judge(0, prompt_digest(copilot_prompt(rel, 2))).text == "PONG"


# ---- codex -----------------------------------------------------------------------------


def test_codex_reply_abort_and_stripped_comparison(tmp_path):
    rel = "codex/fullauto-env-interrupt-aborted.rollout.jsonl"
    log = CodexLog(str(copy(tmp_path, rel)))
    assert log.judge(0, prompt_digest("  Reply with exactly PONG3.")).text == "PONG3"  # leading blanks stripped
    assert log.judge(0, prompt_digest("Run the shell command `sleep 30` and then reply DONE.")).outcome is Outcome.ABORTED
    assert not log.turn_open()
    rel2 = "codex/readonly-no-rule-localshell.rollout.jsonl"
    assert CodexLog(str(copy(tmp_path, rel2))).judge(0, prompt_digest(" !touch g2.txt")).outcome is Outcome.REPLY


def test_codex_empty_final_message_is_not_a_reply(tmp_path):
    p = tmp_path / "r.jsonl"
    append_jsonl(p, {"type": "event_msg", "payload": {"type": "task_started"}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                        "content": [{"type": "input_text", "text": "hello"}]}})
    log = CodexLog(str(p))
    assert log.turn_open() and log.judge(0, prompt_digest("hello")).outcome is Outcome.IN_PROGRESS
    append_jsonl(p, {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": ""}})
    assert log.judge(0, prompt_digest("hello")).outcome is Outcome.ABORTED


def test_prompt_split_into_several_inputs_is_not_delivered(tmp_path):
    p = tmp_path / "r.jsonl"
    for part in ("first line", "second line"):
        append_jsonl(p, {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                               "content": [{"type": "input_text", "text": part}]}})
    append_jsonl(p, {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "ok"}})
    assert CodexLog(str(p)).judge(0, prompt_digest("first line\nsecond line")).outcome is Outcome.NOT_SEEN


# ---- claude ----------------------------------------------------------------------------


def test_claude_turns(tmp_path):
    rel = "claude/real-readonly-restricted-env.jsonl"
    log = ClaudeLog(str(copy(tmp_path, rel)))
    assert log.judge(0, prompt_digest("Reply with exactly PONG")).text == "PONG"
    j = log.judge(0, prompt_digest("Read the file data.txt and tell me the secret word it contains."))
    assert j.outcome is Outcome.REPLY and "BANANA-42" in j.text  # after a tool call
    assert log.judge(0, prompt_digest(f"{K.GUIDE_LINE_TEXT}\n!touch ro-gl.txt")).outcome is Outcome.REPLY
    assert log.judge(0, prompt_digest("Reply with OK only.\n/permissions\n!touch ro-ml.txt")).text == "OK"
    assert not log.turn_open()


def test_claude_interrupt_is_an_abort(tmp_path):
    rel = "claude/stub-interrupt.jsonl"
    log = ClaudeLog(str(copy(tmp_path, rel)))
    assert log.judge(0, prompt_digest("SLOW please")).outcome is Outcome.ABORTED
    assert log.judge(0, prompt_digest("Reply with PONG2")).outcome is Outcome.REPLY


def test_claude_refused_tool_use_is_an_abort(tmp_path):
    """Scenario: 拒绝导致回合中止 (default permission mode, Esc at the write dialog)."""
    rel = "claude/real-default-mode-write-dialog-rejected.jsonl"
    prompt = "Create a file named dm.txt containing hi using your file writing tool, then reply DONE."
    assert ClaudeLog(str(copy(tmp_path, rel))).judge(0, prompt_digest(prompt)).outcome is Outcome.ABORTED


def test_claude_api_error(tmp_path):
    """Scenario: agent 级错误."""
    rel = "claude/api-error-region.jsonl"
    j = ClaudeLog(str(copy(tmp_path, rel))).judge(0, prompt_digest("Reply with exactly the word PONG and nothing else."))
    assert j.outcome is Outcome.ERROR and "API Error" in j.text


def test_claude_ignores_side_chain_entries(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text((LOGS / "claude/real-readonly-async-subagent.agent.jsonl").read_text())
    first = json.loads(p.read_text().splitlines()[0])["message"]["content"]
    assert ClaudeLog(str(p)).judge(0, prompt_digest(first)).outcome is Outcome.NOT_SEEN
    assert not ClaudeLog(str(p)).turn_open()


def test_claude_turn_open(tmp_path):
    rel = "claude/real-readonly-restricted-env.jsonl"
    assert ClaudeLog(str(copy(tmp_path, rel, upto=5))).turn_open()


# ---- locating the logs -------------------------------------------------------------------


def test_config_dir_from_the_agent_environment(tmp_path):
    environ = b"PATH=/bin\0COPILOT_HOME=/iso/copilot\0HOME=/h\0"
    assert locate.config_dir(environ, "COPILOT_HOME", tmp_path, ".copilot") == Path("/iso/copilot")
    assert locate.config_dir(b"PATH=/bin\0", "COPILOT_HOME", tmp_path, ".copilot") == tmp_path / ".copilot"
    assert locate.config_dir(None, "CLAUDE_CONFIG_DIR", tmp_path, ".claude") == tmp_path / ".claude"
    assert locate.env_value(b"CLAUDE_CONFIG_DIR_X=1\0CLAUDE_CONFIG_DIR=/c\0", "CLAUDE_CONFIG_DIR") == "/c"


def test_log_paths():
    sid = "11111111-2222-3333-4444-555555555555"
    assert locate.copilot_log_path(Path("/c"), sid) == Path(f"/c/session-state/{sid}/events.jsonl")
    assert not hasattr(locate, "claude_log_path") and not hasattr(locate, "claude_project_key")  # D12: never computed


def test_codex_rollout_from_open_file_descriptors(tmp_path):
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    rollout = tmp_path / "sessions" / "rollout-2026-11-16T10-00-00-abc.jsonl"
    rollout.parent.mkdir()
    rollout.write_text("")
    (proc / "42" / "fd" / "3").symlink_to("/dev/null")
    (proc / "42" / "fd" / "7").symlink_to(rollout)
    assert locate.open_rollouts([41, 42], root=str(proc)) == [str(rollout)]


# ---- screen capture (7.4) --------------------------------------------------------------------


CURSOR = K.adapter("cursor")


def test_reply_from_the_screen():
    """Scenario: 退回屏幕截取 (cursor sample)."""
    screen = (SCREENS / "cursor/websearch-rejected-after-esc.txt").read_text()
    before = screen.split("  Do these steps in order")[0]  # the screen before the second prompt was sent
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    baseline = cap.baseline(before)
    assert baseline[-1].strip() == "The secret word is BANANA-42."
    prompt = ("Do these steps in order and report each result in one short line: 1) run the shell command "
              "`touch preset.txt` 2) create a new file w.txt containing hi 3) run `curl -s http://127.0.0.1:43831/probe` "
              "4) search the web for 'polyagent probe'. Do not ask me questions.")
    text = cap.extract(screen, baseline, [prompt], count=cap.count(before, baseline))
    assert text.startswith("Ask mode blocks file changes")
    assert text.rstrip().endswith("4. Web search for polyagent probe was rejected.")
    assert "Add a follow-up" not in text and "shift+tab" not in text


def test_first_prompt_on_a_fresh_screen():
    screen = (SCREENS / "cursor/websearch-rejected-after-esc.txt").read_text()
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    text = cap.extract(screen, [], ["Read the file data.txt and tell me the secret word it contains."], count=0)
    assert text.startswith("I'll open data.txt") and "BANANA-42" in text


def test_multiline_prompt_continuation_and_prefixes_are_skipped():
    cap = ScreenCapture(K.adapter("claude").footer, K.adapter("claude").footer_block)
    screen = "old reply\n> Message from the user follows:\n  /help me\n\n⏺ Here is the answer.\n  Second line.\n" \
             "  Worked for 3s • 2:47 PM\n" + "─" * 40 + "\n❯ \n"
    text = cap.extract(screen, ["old reply"], [K.GUIDE_LINE_TEXT, "/help me"], count=1)
    assert text == "Here is the answer.\nSecond line."


def test_no_echo_after_the_baseline_gives_none():
    """Scenario: 无法分离回复 — the agent raises ReplyNotFoundError from this (see test_ask)."""
    screen = (SCREENS / "cursor/websearch-rejected-after-esc.txt").read_text()
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    assert cap.extract(screen, cap.baseline(screen), ["Read the file data.txt"], count=1) is None
    assert cap.extract(screen, ["a line that is not on screen"], ["Read the file data.txt"], count=1) is None


def test_baseline_ignores_the_footer():
    screen = (SCREENS / "cursor/websearch-rejected-after-esc.txt").read_text()
    assert all("Add a follow-up" not in line for line in ScreenCapture(CURSOR.footer, CURSOR.footer_block).baseline(screen))


def test_codex_screen_footer():
    screen = (SCREENS / "codex/status-after-raw-slash-status.txt").read_text()
    cap = ScreenCapture(K.adapter("codex").footer, K.adapter("codex").footer_block)
    text = cap.extract(screen, [], ["/status"], count=0)
    assert "OpenAI Codex" in text and "Ask Codex to do anything" not in text


def test_copilot_turnless_notification_long_before_the_prompt_is_not_ambiguous(tmp_path):
    p = tmp_path / "e.jsonl"
    append_jsonl(p, {"type": "system.notification", "timestamp": iso(1_900_000_000.0), "data": {}},
                 {"type": "user.message", "timestamp": iso(1_900_000_030.0), "data": {"content": "q"}},
                 {"type": "assistant.message", "timestamp": iso(1_900_000_031.0), "data": {"content": "a", "toolRequests": []}},
                 {"type": "session.usage_checkpoint", "timestamp": iso(1_900_000_031.0), "data": {}})
    assert CopilotLog(str(p)).judge(0, prompt_digest("q")).text == "a"
    q = tmp_path / "f.jsonl"
    append_jsonl(q, {"type": "system.notification", "timestamp": iso(1_900_000_000.0), "data": {}},
                 {"type": "user.message", "timestamp": iso(1_900_000_003.0), "data": {"content": "q"}})
    assert CopilotLog(str(q)).judge(0, prompt_digest("q")).outcome is Outcome.AMBIGUOUS


# ---- phase-4 review regressions -------------------------------------------------------------------


def test_find_claude_log_by_session_id(tmp_path):
    sid = "11111111-2222-3333-4444-555555555555"
    assert locate.find_claude_log(tmp_path, sid) is None
    d = tmp_path / "projects" / "whatever-name"
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("")
    assert locate.find_claude_log(tmp_path, sid) == str(d / f"{sid}.jsonl")
    assert locate.find_claude_log(tmp_path, "../x") is None
    # Scenario: 工作目录含特殊字符 — whatever claude made of a space, `@` or non-ASCII characters.
    odd = tmp_path / "projects" / "-srv-my project-app@2-ü"
    odd.mkdir()
    sid2 = "21111111-2222-3333-4444-555555555555"
    (odd / f"{sid2}.jsonl").write_text("")
    assert locate.find_claude_log(tmp_path, sid2) == str(odd / f"{sid2}.jsonl")


def test_jsonl_resolve_and_truncation(tmp_path):
    late = tmp_path / "late.jsonl"
    log = ClaudeLog(str(tmp_path / "guess.jsonl"), resolve=lambda: str(late) if late.exists() else None)
    assert log.mark() == 0 and not log.exists()
    append_jsonl(late, {"type": "user", "message": {"role": "user", "content": "q"}})
    assert log.mark() == 1 and log.path == str(late)
    late.write_text("")  # replaced by a shorter file: nothing new, the marks stay valid
    assert log.mark() == 1


def test_identical_questions_and_answers_on_screen():
    """Review C11: the baseline block also ends the new turn; the right occurrence is the last one
    that is followed by this prompt's echo."""
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    foot = "→ Add a follow-up\n  Auto Balance   Run Everything"
    head = ["Cursor Agent", "~/repo"]
    turn = ["> ping", "PONG"]
    one, two, three = ("\n".join(head + turn * n + [foot]) for n in (1, 2, 3))

    def after(before, now):
        base = cap.baseline(before)
        return cap.extract(now, base, ["ping"], count=cap.count(before, base))

    assert after(one, two) == "PONG"
    assert after(two, three) == "PONG"
    bare = "\n".join(turn + [foot])
    base = cap.baseline(bare)
    assert cap.extract("\n".join(turn * 2 + [foot]), base, ["ping"], count=cap.count(bare, base)) == "PONG"
    assert cap.echo_seen("\n".join(turn * 2 + [foot]), base, ["ping"], count=cap.count(bare, base))
    assert not cap.echo_seen("\n".join(turn * 2), base, ["ping"], count=cap.count(bare, base))  # no input area
    assert not cap.echo_seen(bare, base, ["ping"], count=cap.count(bare, base))


def test_identical_turns_before_an_undelivered_prompt_are_not_its_reply():
    """Review N1: three identical ping/PONG turns on screen, the fourth prompt never arrives: no echo, no
    reply. Delivered, the fourth turn's reply is found."""
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    foot = "→ Add a follow-up\n  Auto Balance   Run Everything"
    turn = ["> ping", "PONG"]
    three = "\n".join(["Cursor Agent"] + turn * 3 + [foot])
    four = "\n".join(["Cursor Agent"] + turn * 3 + ["> ping", "PONG 4"] + [foot])
    base = cap.baseline(three)
    n = cap.count(three, base)
    assert n == 2  # the three-line block "PONG, > ping, PONG" occurs twice in three turns
    assert cap.extract(three, base, ["ping"], count=n) is None
    assert not cap.echo_seen(three, base, ["ping"], count=n)
    assert cap.extract(four, base, ["ping"], count=n) == "PONG 4"
    assert cap.echo_seen(four, base, ["ping"], count=n)


def test_a_scrolled_screen_never_points_before_the_pre_submit_position():
    """Review N1: earlier occurrences scrolled out of a later read: the n-th occurrence is then at or after
    the pre-submit position, so the result is "not found" or the right reply, never an old one."""
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    turn = ["> ping", "PONG"]
    before = turn * 5
    base = cap.baseline("\n".join(before))
    n = cap.count("\n".join(before), base)
    scrolled = "\n".join(before[4:])  # three old turns left in the window, prompt not delivered
    assert cap.extract(scrolled, base, ["ping"], count=n) is None
    delivered = "\n".join(before[4:] + ["> ping", "PONG 6"])
    assert cap.extract(delivered, base, ["ping"], count=n) in (None, "PONG 6")


def test_one_word_reply_that_occurs_in_the_prompt_is_kept():
    """Review S5 (phase 4 spot check): continuation lines are matched in order, not as substrings."""
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    screen = "Cursor Agent\n> Reply with exactly one word: PONG\n\nPONG\n→ Add a follow-up\n  Auto Balance   Run Everything"
    assert cap.extract(screen, ["Cursor Agent"], ["Reply with exactly one word: PONG"], count=1) == "PONG"
    prompt = "Reply with exactly one word, nothing else, no punctuation at all; the word is PONG"
    wrapped = ("Cursor Agent\n> Reply with exactly one word, nothing else, no punctuation at all;\n"
               "the word is PONG\n\nPONG\n→ Add a follow-up\n  Auto Balance   Run Everything")
    assert cap.extract(wrapped, ["Cursor Agent"], [prompt], count=1) == "PONG"


def test_cursor_first_prompt_on_a_real_screen():
    """Phase 5: before the first prompt cursor's input line is `→ Plan, search, build anything`; it is a
    footer marker too, otherwise the baseline is taken from lines that disappear with the first prompt."""
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    before = (SCREENS / "cursor/first-prompt-before.txt").read_text()
    after = (SCREENS / "cursor/first-prompt-pong-after.txt").read_text()
    base = cap.baseline(before)
    assert base and all("Plan, search" not in line for line in base)
    assert cap.extract(after, base, ["Reply with exactly one word: PONG"], count=cap.count(before, base)) == "PONG"


def test_copilot_two_prompts_in_one_turn_are_ambiguous(tmp_path):
    """Phase 5: a prompt submitted while the previous turn was running joins that turn (copilot queues it);
    the final message cannot be attributed to either prompt."""
    p = tmp_path / "e.jsonl"
    append_jsonl(p, {"type": "user.message", "timestamp": iso(), "data": {"content": "first"}},
                 {"type": "assistant.message", "timestamp": iso(), "data": {"content": "", "toolRequests": [{"name": "view"}]}},
                 {"type": "user.message", "timestamp": iso(), "data": {"content": "second"}},
                 {"type": "assistant.message", "timestamp": iso(), "data": {"content": "SECOND", "toolRequests": []}},
                 {"type": "session.usage_checkpoint", "timestamp": iso(), "data": {}})
    log = CopilotLog(str(p))
    assert log.judge(0, prompt_digest("first")).outcome is Outcome.AMBIGUOUS
    assert log.judge(0, prompt_digest("second")).outcome is Outcome.AMBIGUOUS


def test_claude_drops_format_characters_from_the_recorded_prompt(tmp_path):
    """Phase 5 (real claude 2.1.322): a BOM after the guide line is not in the transcript; the prompt still
    matches its digest."""
    sent = f"{K.GUIDE_LINE_TEXT}\n\ufeff!touch pwned\nReply OK"
    p = tmp_path / "c.jsonl"
    append_jsonl(p, {"type": "user", "message": {"role": "user", "content": sent.replace("\ufeff", "")}},
                 {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn",
                                                   "content": [{"type": "text", "text": "OK"}]}},
                 {"type": "system", "subtype": "turn_duration", "durationMs": 5})
    assert ClaudeLog(str(p)).judge(0, prompt_digest(sent)).text == "OK"


def test_cursor_input_line_showing_the_previous_prompt_is_a_footer():
    """Phase 5 (real cursor run): right after a turn the input line can read `→ <previous prompt>`; the
    baseline must not be taken from it, or the next reply is never found."""
    cap = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
    before = ("  Cursor Agent\n  > Reply with exactly one word: PONG\n\n  PONG\n\n"
              "  → Reply with exactly one word: PONG\n\n  Auto Balance   Run Everything\n  /tmp/pa-work\n")
    after = ("  Cursor Agent\n  > Reply with exactly one word: PONG\n\n  PONG\n\n  > Read both files\n\n"
             "  - **alpha.txt**: 41\n  - **beta.txt**: 17\n\n  → Add a follow-up\n\n  Auto Balance   Run Everything\n")
    base = cap.baseline(before)
    assert base and all("→" not in line for line in base)
    assert cap.extract(after, base, ["Read both files"], count=cap.count(before, base)) == \
        "- **alpha.txt**: 41\n- **beta.txt**: 17"


# ---- task 13.21: content area and footer (D12 "内容区和页脚") ------------------------------------------

CURSOR_CAP = ScreenCapture(CURSOR.footer, CURSOR.footer_block)
CODEX = K.adapter("codex")
CODEX_CAP = ScreenCapture(CODEX.footer, CODEX.footer_block)
STATUS = "  Auto Balance   Run Everything"


@pytest.mark.parametrize("input_line", ["→ Plan, search, build anything", "→ Add a follow-up",
                                        "→ Reply with exactly one word: PONG"])
def test_cursor_three_input_line_forms(input_line):
    before = f"Cursor Agent\n  old answer\n\n  {input_line}\n\n{STATUS}\n  /tmp/pa-work\n"
    assert CURSOR_CAP.recognized(before) and CURSOR_CAP.baseline(before) == ["Cursor Agent", "  old answer"]
    after = before.replace(f"  {input_line}", f"  What is it?\n\n  It is 42.\n\n  {input_line}")
    base = CURSOR_CAP.baseline(before)
    assert CURSOR_CAP.extract(after, base, ["What is it?"], count=CURSOR_CAP.count(before, base)) == "It is 42."


def test_cursor_reply_lines_starting_with_the_input_prefix_are_kept():
    """Scenario: 回复正文里有与输入行相同开头的行 (the known defect before 13.21)."""
    before = f"Cursor Agent\n  → Add a follow-up\n{STATUS}\n"
    after = (f"Cursor Agent\n  List the steps\n  Steps:\n  → first step\n  → second step\n  done.\n"
             f"  → Add a follow-up\n{STATUS}\n")
    base = CURSOR_CAP.baseline(before)
    text = CURSOR_CAP.extract(after, base, ["List the steps"], count=CURSOR_CAP.count(before, base))
    assert text == "Steps:\n→ first step\n→ second step\ndone."


def test_cursor_arrow_line_right_above_the_footer_block_stays_in_the_reply():
    screen = f"Cursor Agent\n  ask\n  → the last line of the reply\n  → Add a follow-up\n{STATUS}\n  /tmp/x\n"
    assert CURSOR_CAP.extract(screen, [], ["ask"], count=0) == "→ the last line of the reply"


@pytest.mark.parametrize("tail", [
    "  → Add a follow-up\n  Auto Balance\n  /tmp/x\n",  # an arrow line near the end, no status marker
    "  → Add a follow-up\n  a\n  b\n  c\n  d\n" + STATUS + "\n",  # five non-blank lines after it
    "  no input line at all\n" + STATUS + "\n",
])
def test_cursor_screen_without_a_recognized_footer_block(tail):
    screen = "Cursor Agent\n  ask\n  It is 42.\n" + tail
    assert not CURSOR_CAP.recognized(screen)
    assert CURSOR_CAP.extract(screen, [], ["ask"], count=0) is None
    assert not CURSOR_CAP.echo_seen(screen, [], ["ask"], count=0)


def test_codex_echo_in_the_transcript_carries_the_input_prefix():
    """Phase-2 real codex screen: submitted prompts appear as `› <prompt>` in the transcript."""
    screen = ("  >_ OpenAI Codex (v0.167.0)\n\n› Reply with exactly PONG.\n\n• PONG\n\n"
              "› Ask Codex to do anything\n\n  gpt default · /tmp/repo · ← for agents\n")
    assert CODEX_CAP.extract(screen, [], ["Reply with exactly PONG."], count=0) == "PONG"


def test_codex_prompt_left_in_the_input_box_is_not_an_echo():
    """Scenario: prompt 留在输入框里没有提交 — the status line below it is never returned."""
    before = "  >_ OpenAI Codex (v0.167.0)\n\n  old reply\n\n› Ask Codex to do anything\n\n  gpt default · /tmp/repo\n"
    stuck = "  >_ OpenAI Codex (v0.167.0)\n\n  old reply\n\n› What is the answer?\n\n  gpt default · /tmp/repo\n"
    base = CODEX_CAP.baseline(before)
    count = CODEX_CAP.count(before, base)
    assert not CODEX_CAP.echo_seen(stuck, base, ["What is the answer?"], count=count)
    assert CODEX_CAP.extract(stuck, base, ["What is the answer?"], count=count) is None


def test_cursor_prompt_left_in_the_input_box_is_not_an_echo():
    before = f"Cursor Agent\n  old\n  → Add a follow-up\n{STATUS}\n"
    stuck = f"Cursor Agent\n  old\n  → What is the answer?\n{STATUS}\n"
    base = CURSOR_CAP.baseline(before)
    assert CURSOR_CAP.extract(stuck, base, ["What is the answer?"], count=CURSOR_CAP.count(before, base)) is None


@pytest.mark.parametrize("rel", ["cursor/first-prompt-before.txt", "cursor/first-prompt-pong-after.txt",
                                 "cursor/websearch-rejected-after-esc.txt", "codex/status-after-raw-slash-status.txt"])
def test_samples_show_a_recognized_input_area(rel):
    kind = rel.split("/")[0]
    a = K.adapter(kind)
    assert ScreenCapture(a.footer, a.footer_block).recognized((SCREENS / rel).read_text())
