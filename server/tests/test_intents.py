import pytest
from app.intents import _regex_action


@pytest.mark.parametrize(
    "text, expected",
    [
        # False positives that used to hit the wrong tool.
        ("next week's weather", None),
        ("lower the thermostat", None),
        ("turn up the heating", None),
        ("play something by Queen", None),
        ("playlist please", None),
        ("never mind the weather, what time is it", None),
        ("what's the weather in Paris?", None),
        # Real commands.
        ("pause", ("media_control", {"action": "pause"})),
        ("pause the music", ("media_control", {"action": "pause"})),
        ("stop the music", ("media_control", {"action": "pause"})),
        ("play the music", ("media_control", {"action": "play"})),
        ("next track", ("media_control", {"action": "next"})),
        ("skip", ("media_control", {"action": "next"})),
        ("shuffle", ("media_control", {"action": "next"})),
        ("start the song over", ("media_control", {"action": "restart"})),
        ("mute", ("change_volume", {"direction": "mute"})),
        ("turn the volume up", ("change_volume", {"direction": "up", "steps": 3})),
        ("turn up the volume", ("change_volume", {"direction": "up", "steps": 3})),
        ("lower the volume", ("change_volume", {"direction": "down", "steps": 3})),
        ("stop talking", ("stop_listening", {})),
        ("stop listening", ("stop_listening", {})),
        ("that's all", ("stop_listening", {})),
        ("never mind", ("stop_listening", {})),
        ("set a timer for five minutes", ("set_timer", {"seconds": 300})),
        ("set an alarm for 7 am", ("set_alarm", {"hour": 7, "minute": 0})),
        ("set volume to 40", ("set_volume", {"level": 40})),
        ("keep listening", ("continue_listening", {})),
        ("what time is it", None),
        ("tell me a joke", None),
    ],
)
def test_regex_action(text, expected):
    assert _regex_action(text) == expected
