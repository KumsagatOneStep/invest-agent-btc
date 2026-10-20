from datetime import time

from invest_agent.config import DEFAULT_WINDOWS_UTC, Settings


def test_window_times_utc():
    s = Settings(_env_file=None, windows=["08:00", "20:00"], tz_offset_hours=5)
    assert s.window_times_utc() == [time(3, 0), time(15, 0)]


def test_defaults():
    s = Settings(_env_file=None)
    assert s.claude_monthly_budget_usd == 10
    assert s.cheap_model == "claude-haiku-5-5"
    assert s.telegram_chat_id == 0
    assert DEFAULT_WINDOWS_UTC == [time(3, 0), time(15, 0)]
