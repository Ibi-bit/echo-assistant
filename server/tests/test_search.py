from app.search import _extract_location, parse_ddg_results


def test_location_explicit_place():
    assert _extract_location("what's the weather in Paris tomorrow") == "paris"


def test_location_stops_at_trailing_preposition():
    assert _extract_location("what's the weather in Paris for tomorrow") == "paris"
    assert _extract_location("weather near London on Friday") == "london"


def test_location_ignores_time_of_day():
    assert _extract_location("what's the weather in the morning") is None


def test_location_weather_without_place():
    assert _extract_location("what's the weather like?") is None


def test_search_parse_strips_markup():
    page = (
        '<a class="result__a">Hello <b>world</b></a>'
        '<a class="result__snippet">A &amp; B</a>'
    )
    assert parse_ddg_results(page) == [("Hello world", "A & B")]
