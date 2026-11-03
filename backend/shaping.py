"""Build the upstream vLLM request from the browser's request.

The browser's body is never forwarded. Only the role and text of user and
assistant messages survive; everything else (model, sampling, max_tokens,
tools, system prompts) is set here from config.
"""

from __future__ import annotations

from typing import Any

from backend import config

ROLES = ("user", "assistant")


class ShapingError(ValueError):
    status = 400

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_messages(body: Any) -> list[dict[str, str]]:
    """Validate the browser's `{"messages": [...]}` and keep only role + text."""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise ShapingError("invalid_request", 'Body must be {"messages": [...]}.')
    raw = body["messages"]
    if len(raw) > config.MAX_BODY_MESSAGES:
        raise ShapingError("invalid_request", "Too many messages.")
    messages = []
    for m in raw:
        if not isinstance(m, dict) or not isinstance(m.get("content"), str):
            raise ShapingError("invalid_request", "Each message needs a role and text content.")
        role = m.get("role")
        if role == "system":
            continue  # the server sets its own system prompt
        if role not in ROLES:
            raise ShapingError("invalid_request", "Message role must be user or assistant.")
        messages.append({"role": role, "content": m["content"]})
    return messages


def shape(body: Any) -> tuple[dict[str, Any], int]:
    """Return (upstream request body, prompt character count)."""
    messages = parse_messages(body)
    if not messages or messages[-1]["role"] != "user":
        raise ShapingError("invalid_request", "The last message must be from the user.")
    last = messages[-1]["content"]
    if not last.strip():
        raise ShapingError("invalid_request", "Message is empty.")
    if len(last) > config.MAX_MESSAGE_CHARS:
        raise ShapingError(
            "message_too_long", f"Messages are limited to {config.MAX_MESSAGE_CHARS} characters."
        )

    history = [
        {"role": m["role"], "content": m["content"][: config.MAX_MESSAGE_CHARS]}
        for m in messages[-config.MAX_TURNS :]
    ]
    # Drop the oldest messages until the total fits (the newest always fits),
    # then start the window on a user message.
    while sum(len(m["content"]) for m in history) > config.MAX_TOTAL_INPUT_CHARS:
        history.pop(0)
    while history[0]["role"] != "user":
        history.pop(0)

    upstream = {
        "model": config.SERVED_MODEL_NAME,
        "messages": [{"role": "system", "content": config.SYSTEM_PROMPT}, *history],
        "max_tokens": config.MAX_TOKENS,
        "temperature": config.TEMPERATURE,
        "top_p": config.TOP_P,
        "top_k": config.TOP_K,
        "presence_penalty": config.PRESENCE_PENALTY,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    return upstream, sum(len(m["content"]) for m in history)
