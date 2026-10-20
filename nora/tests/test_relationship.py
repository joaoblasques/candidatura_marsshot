from app.relationship import MAX_FACT_LEN, parse_relationship_directive


def test_remember_directive():
    action, fact, remaining = parse_relationship_directive("remember: prefers morning meetings")
    assert action == "remember" and fact == "prefers morning meetings" and remaining == ""


def test_remember_keeps_other_instruction():
    action, fact, remaining = parse_relationship_directive("warmly remember: assistant is Sarah")
    assert action == "remember" and fact == "assistant is Sarah" and remaining == "warmly"


def test_forget_directive():
    assert parse_relationship_directive("forget") == ("forget", None, "")
    assert parse_relationship_directive("forget contact") == ("forget", None, "")


def test_plain_instruction_is_not_a_directive():
    assert parse_relationship_directive("keep it short") == (None, None, "keep it short")
    assert parse_relationship_directive(None) == (None, None, "")


def test_fact_is_length_capped():
    action, fact, _ = parse_relationship_directive("remember: " + "x" * 1000)
    assert action == "remember" and len(fact) <= MAX_FACT_LEN


def test_multi_bracket_remember_does_not_absorb_sibling_note():
    # Parser puts each [bracket] on its own line; the fact must stop at the line end.
    action, fact, remaining = parse_relationship_directive("remember: prefers mornings\nwarmly")
    assert action == "remember" and fact == "prefers mornings" and remaining == "warmly"
    # order-independent
    action, fact, remaining = parse_relationship_directive("warmly\nremember: assistant is Sarah")
    assert action == "remember" and fact == "assistant is Sarah" and remaining == "warmly"


def test_multi_bracket_forget_keeps_sibling_note():
    action, fact, remaining = parse_relationship_directive("forget\nkeep it short")
    assert action == "forget" and fact is None and remaining == "keep it short"


def test_parser_keeps_bracket_notes_separate():
    from app.email.parser import parse_command_line
    from app.relationship import parse_relationship_directive
    _cp, instruction = parse_command_line("NB-RE [remember: prefers mornings] [warmly]")
    action, fact, remaining = parse_relationship_directive(instruction)
    assert fact == "prefers mornings" and "warmly" in remaining and "warmly" not in fact
