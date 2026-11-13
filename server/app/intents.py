"""High-precision deterministic intent routing.

Short imperatives ("pause", "mute", "timer for five minutes") are run
server-side before the model sees them because small models skip tool calls on
them and then hallucinate success. Only unambiguous phrases fire; everything
else falls through to the model.
"""
from __future__ import annotations

import re

_NUMBER_WORD_VALUES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

_NUMBER_ONES = "one|two|three|four|five|six|seven|eight|nine"
_NUMBER_TEENS = (
    "ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
)
_NUM = (
    r"(?:\d+|one hundred|a hundred|hundred|"
    r"twenty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"thirty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"forty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"fifty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"sixty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"seventy(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"eighty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"ninety(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    + _NUMBER_TEENS + r"|" + _NUMBER_ONES + r"|a|an)"
)

# Words that make a phrase a question rather than a command.
_QUESTION_START = (
    "what ", "what s ", "how ", "when ", "where ", "which ", "who ", "why ",
    "whose ", "is ", "are ", "was ", "were ", "did ", "do ", "does ",
    "can you tell", "tell me", "how much", "how loud",
)

# Objects an imperative can safely act on. Anything else ("play something by
# Queen", "lower the thermostat") must reach the model instead.
_MEDIA_OBJECT = r"(?:the\s+)?(?:music|song|track|playback|spotify|it|this|that)"
_VOLUME_OBJECT = r"(?:the\s+)?(?:volume|music|song|track|playback|it|this|that)"

_PAUSE = re.compile(
    rf"(?:pause|stop|pause\s+{_MEDIA_OBJECT}|stop\s+{_MEDIA_OBJECT})"
)
_PLAY = re.compile(
    rf"(?:play|resume|play\s+{_MEDIA_OBJECT}|keep\s+playing)"
)
_NEXT = re.compile(
    r"(?:next|skip|shuffle|next\s+(?:track|song|one)|skip\s+(?:this|it|the\s+(?:song|track))|"
    r"shuffle\s+(?:the\s+)?(?:music|playlist|songs))"
)
_PREVIOUS = re.compile(
    r"(?:previous|back|previous\s+(?:track|song)|go\s+back)"
)
_RESTART = re.compile(
    r"(?:restart|replay|restart\s+(?:the\s+)?(?:song|track|music)|"
    r"start\s+(?:it|the\s+(?:song|track|music))\s+(?:over|again)|"
    r"start\s+(?:over|again)|from\s+the\s+(?:start|beginning))"
)
_VOLUME_UP = re.compile(
    rf"(?:volume\s+up|louder|crank(?:\s+{_VOLUME_OBJECT})?(?:\s+up)?|"
    rf"turn\s+(?:it|the\s+volume)\s+up|turn\s+up\s+(?:it|the\s+volume))"
)
_VOLUME_DOWN = re.compile(
    rf"(?:volume\s+down|quieter|softer|lower(?:\s+{_VOLUME_OBJECT})?|"
    rf"turn\s+(?:it|the\s+volume)\s+down|turn\s+down\s+(?:it|the\s+volume))"
)
_CONTINUE = re.compile(r"(?:keep|continue|stay)\s+listening")
_STOP_LISTENING = re.compile(
    r"(?:stop|quit|exit|end)\s+(?:talking|listening|the\s+conversation|"
    r"this\s+conversation|our\s+conversation)"
)
_STOP_PHRASES = re.compile(
    r"(?:shut\s+up|be\s+quiet|that\s+s\s+all|that\s+is\s+all|never\s+mind)"
)


def _number_from_words(raw: str) -> int | None:
    """Parse digits or English number words (incl. 'twenty five', 'one hundred')."""
    s = raw.strip().lower().replace("-", " ")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    tokens = [tok for tok in s.split() if tok != "and"]
    if not tokens:
        return None
    current = 0
    seen = False
    for tok in tokens:
        if tok in ("a", "an"):
            value = 1
        elif tok == "hundred":
            current = (current or 1) * 100
            seen = True
            continue
        elif tok in _NUMBER_WORD_VALUES:
            value = _NUMBER_WORD_VALUES[tok]
        else:
            return None
        current += value
        seen = True
    return current if seen else None


def _normalise(text: str) -> str:
    core = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9: ]", " ", text.lower())).strip()
    return re.sub(r"\s+(?:please|thanks|thank\s+you)$", "", core).strip()


def _strip_fillers(text: str) -> str:
    imp = text
    for _ in range(4):
        shorter = re.sub(
            r"^(?:hey\s+jarvis|ok|okay|so|and|well|please|just|now|then|um|uh)\b[ ,]*",
            "",
            imp,
        )
        if shorter == imp:
            break
        imp = shorter
    return re.sub(r"^(?:can you|could you|would you|will you)\s+", "", imp).strip()


def _regex_action(text: str) -> tuple[str, dict] | None:
    core = _normalise(text)
    if not core:
        return None
    interrogative = core.startswith(_QUESTION_START)
    imp = _strip_fillers(core)
    if not imp:
        return None

    if _STOP_LISTENING.fullmatch(imp) or _STOP_PHRASES.fullmatch(imp):
        return "stop_listening", {}

    if re.search(r"\b(?:cancel|dismiss|stop|kill|remove)\b[^.]*\btimer\b", core):
        return "cancel_timer", {}
    if re.search(r"\b(?:cancel|dismiss|turn off|stop|kill|remove)\b[^.]*\balarm\b", core):
        return "cancel_alarm", {}

    if re.search(r"\btimer\b", core) and not interrogative:
        m = re.search(rf"({_NUM})\s*(seconds?|minutes?|hours?)\b", core)
        if m:
            n = _number_from_words(m.group(1))
            if n is not None and n > 0:
                unit = m.group(2)
                secs = n if unit.startswith("s") else n * 60 if unit.startswith("m") else n * 3600
                return "set_timer", {"seconds": max(1, min(86400, secs))}

    if re.search(r"\balarm\b|\bwake me\b", core) and not interrogative:
        hour: int | None = None
        minute = 0
        ampm = None
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", core)
        if m and (m.group(2) or m.group(3) or re.search(r"\b(?:at|for|to)\s+\d", core)):
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = m.group(3)
        if hour is None:
            m2 = re.search(rf"\b({_NUM})\s*(am|pm)\b", core)
            if m2:
                hour = _number_from_words(m2.group(1))
                ampm = m2.group(2)
        if hour is None:
            m3 = re.search(rf"\balarm\s+(?:for\s+|at\s+)?({_NUM})\b", core)
            if m3 and not re.search(r"\b(?:minutes?|seconds?|hours?)\b", core):
                hour = _number_from_words(m3.group(1))
        if hour is not None and 0 <= hour <= 23:
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return "set_alarm", {"hour": hour, "minute": minute}

    if not interrogative:
        m = re.search(rf"\bvolume\b[^.]*?\bto\s+({_NUM})\b", core)
        if m:
            level = _number_from_words(m.group(1))
            if level is not None and 0 <= level <= 100:
                return "set_volume", {"level": level}
        m = re.search(
            rf"\b(?:set|turn|put|change)\b[^.]*?\bvolume\b[^.]*?\b({_NUM})"
            r"\s*(%|percent|per\s*cent)?\b",
            core,
        )
        if m:
            level = _number_from_words(m.group(1))
            if level is not None and 0 <= level <= 100 and (
                m.group(2) or re.search(r"\b(?:set|put)\b", core)
            ):
                return "set_volume", {"level": level}
        if re.search(r"\bunmute\b", imp):
            return "change_volume", {"direction": "unmute"}
        if re.search(r"\bmute\b", imp):
            return "change_volume", {"direction": "mute"}
        if _VOLUME_UP.fullmatch(imp):
            return "change_volume", {"direction": "up", "steps": 3}
        if _VOLUME_DOWN.fullmatch(imp):
            return "change_volume", {"direction": "down", "steps": 3}

        if _PAUSE.fullmatch(imp):
            return "media_control", {"action": "pause"}
        if _PLAY.fullmatch(imp):
            return "media_control", {"action": "play"}
        if _NEXT.fullmatch(imp):
            return "media_control", {"action": "next"}
        if _PREVIOUS.fullmatch(imp):
            return "media_control", {"action": "previous"}
        if _RESTART.fullmatch(imp):
            return "media_control", {"action": "restart"}

    if _CONTINUE.fullmatch(imp):
        return "continue_listening", {}

    return None
