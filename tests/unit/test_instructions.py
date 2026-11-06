from coincoin.realtime.instructions import build_instructions


def test_language_auto_and_explicit_switching_keep_tool_behavior() -> None:
    instructions = build_instructions({"skills": "roulade"})
    assert "witty young adult man" in instructions
    assert "Reply in the user's language" in instructions
    assert "Honor explicit requests to change language" in instructions
    assert "announcements before and after tools" in instructions
    assert "not requests to change language" in instructions
    assert "Use available tools to act" in instructions
    assert "Call sleep" in instructions
    assert "Available skills: roulade." in instructions
    assert "French" not in instructions


def test_response_language_is_configurable_without_changing_the_prompt() -> None:
    for language in ("English", "French", "Spanish"):
        instructions = build_instructions(language=language)
        assert f"Use {language} as the default response language" in instructions
        assert "including after waking or reconnecting" in instructions
        assert "Honor explicit requests to change language" in instructions
    assert "Reply in the user's language" in build_instructions(language=" AUTO ")
