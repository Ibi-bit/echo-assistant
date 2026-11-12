"""Tool schemas exposed to the language model.

The model decides what the user wants; the tablet owns the actual alarm clock,
audio hardware and Rusty UI. Descriptions are intentionally concrete so a small
model does not have to guess units or ranges.
"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "set_alarm",
            "description": (
                "Set a one-time alarm in the tablet's clock app. Use 24-hour local time. "
                "The clock app rings even if the assistant is not running."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                    "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                    "label": {"type": "string", "description": "Optional short label."},
                },
                "required": ["hour", "minute"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Start a countdown timer in the tablet's clock app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                    "label": {"type": "string", "description": "Optional short label."},
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_alarm",
            "description": "Dismiss the next alarm, or every alarm, in the clock app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "all": {"type": "boolean", "description": "Dismiss all alarms, not just the next one."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_timer",
            "description": "Dismiss the current timer in the tablet's clock app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "all": {"type": "boolean", "description": "Reserved; the clock app dismisses the current timer."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": "Set the tablet's media volume to an absolute percentage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_volume",
            "description": "Turn media volume up or down, or mute/unmute it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down", "mute", "unmute"]},
                    "steps": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_listening",
            "description": (
                "Stop the conversation and go back to waiting for the wake word. "
                "Use when the user says stop listening, that's all, never mind, or shut up."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_volume",
            "description": "Read the tablet's current media volume percentage.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_status",
            "description": (
                "Read the latest live snapshot of the tablet: battery, screen, volume, "
                "Wi-Fi, Rusty state, now-playing music, and the next alarm."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": (
                "Control Spotify playback through Rusty. 'restart' seeks the current track "
                "back to the beginning. Use play/pause/toggle/next/previous for transport."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "toggle", "next", "previous", "restart"],
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "continue_listening",
            "description": (
                "Stay listening for the next request after answering. Call this whenever the "
                "user says 'keep listening', 'continue listening', 'stay on', or otherwise "
                "asks to continue without a wake word. Call it in the same response as any "
                "other tool you are calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Optional short reason, e.g. the user asked me to keep listening.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search the web for current or time-sensitive information — weather, news, sports, "
                "prices, recent events — or for any fact you are unsure of. The result is a few "
                "short snippets; answer from them in plain spoken English without citing URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A short search query, e.g. 'London weather today'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]
