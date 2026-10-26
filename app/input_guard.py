"""P19 deterministic inbound-message boundary guard.

Pure, dependency-free checks run before any intent detection, model call, or
tool action. Empty / whitespace-only messages are answered with a friendly
prompt; oversized messages are rejected instead of being sent to the model
(this bounds token cost and resource use without making a network call).
"""

MAX_INBOUND_CHARS = 4000


def sanitize_inbound(message: str | None) -> tuple[str, str | None]:
    """Return ``(cleaned, error_reply)``.

    ``error_reply`` is ``None`` when the message is acceptable; otherwise it
    carries the user-facing reply and ``cleaned`` may be empty.
    """
    if message is None:
        return "", "我没有收到内容，请再说一次。"
    cleaned = message.strip()
    if not cleaned:
        return "", "我没有收到内容，请说点什么，我才能帮你。"
    if len(cleaned) > MAX_INBOUND_CHARS:
        return cleaned, (
            f"内容过长（超过 {MAX_INBOUND_CHARS} 字），请精简后再发给我；"
            "一次说太多容易遗漏关键信息，也请把要做的事分步骤告诉我。"
        )
    return cleaned, None
