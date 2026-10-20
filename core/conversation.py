"""
core/conversation.py — the conversation layer in front of the agent.

One small model call decides whether a message is pure conversation (answer
it now: no screenshots, vision, planning or tools) or anything else (hand it
to the agent unchanged). It fails closed: every error, timeout, or malformed
or doubtful reply means "task", so the agent path behaves exactly as before.
"""

import json
import time
from datetime import datetime

from . import planner
from .config import CHAT_TIMEOUT, DEFAULT_MODEL

_PROMPT = """You are Mist, a desktop assistant inspired by JARVIS: quick, confident, dry and a little playful, natural when spoken aloud, never robotic, and you don't narrate yourself.{address}

CURRENT DATE/TIME (system clock): {now}
{history}
USER MESSAGE: {message}

Decide how to handle the message. Reply ONLY with JSON:
{{"route": "chat" or "task", "reply": "your spoken answer, empty for task"}}

Use "chat" ONLY for pure conversation: small talk, thanks, jokes, opinions, general knowledge, definitions, or the current date/time given above. Keep chat replies to 1-3 sentences unless the user asks for more. The reply is spoken aloud: plain sentences only, no markdown, lists, emoji or stage directions.
Use "task" for everything else: anything about this computer, its screen, files, apps, or the web; live or changing facts (weather, news, prices, scores, whether an app is open); anything that needs an action; a message mixing chat with a request; an indirect request ("it's too loud"); and whenever you are unsure.
In a chat reply, never claim you did, opened, checked, saw or changed anything on the computer."""


def build_prompt(message, now, history="", title=""):
    """The routing prompt. ``now`` is the system clock string, ``history`` the
    agent's recent-conversation block, ``title`` "sir"/"ma'am" or ""."""
    return _PROMPT.format(
        address=f" Occasionally address the user as {title}, not in every reply."
        if title else "",
        now=now,
        history=history,
        message=json.dumps(str(message)),
    )


def parse_route(raw):
    """The chat reply, or None when the call says task or can't be trusted."""
    try:
        cleaned = (raw or "").strip()
        for fence in ("```json", "```"):
            if fence in cleaned:
                cleaned = cleaned.split(fence)[1].split("```")[0]
        data = json.loads(cleaned.strip())
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("route") != "chat":
        return None
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None
    return reply.strip()


def _model_for(api, model):
    """The request's model, else the user's choice (api.current_model()),
    else the default -- bare test objects have no current_model."""
    if model:
        return model
    current = getattr(api, "current_model", None)
    return current() if callable(current) else DEFAULT_MODEL


def try_chat(api, message, gender="none", model=None):
    """Answer ``message`` as conversation, or return None to hand it to the
    agent. Never raises. ``model`` is the request's model (ask_mistai)."""
    t0 = time.monotonic()
    try:
        try:
            history = api.agent._recent_conversation_block(message)
        except Exception:
            history = ""
        title = "sir" if gender == "male" else "ma'am" if gender == "female" else ""
        now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        raw = planner._llm(
            api,
            build_prompt(message, now, history, title),
            _model_for(api, model),
            timeout=CHAT_TIMEOUT,
            label="Conversation",
            fallback=False,
        )
        reply = parse_route(raw)
    except Exception as e:
        print(f"[Conversation] Router failed ({e}) -- routing to agent")
        reply = None
    print(
        f"[Conversation] route={'chat' if reply else 'task'} "
        f"in {time.monotonic() - t0:.2f}s"
    )
    return reply
