from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

FEE = 0.001
SLIPPAGE = 0.0005
RISK_NORMAL = 0.01
RISK_REDUCED = 0.005
MAX_POSITION_FRAC = 0.25
MAX_OPEN = 4
MAX_TOTAL_RISK = 0.04
DD_REDUCED = 0.10
DD_HALT = 0.15
MIN_RR = 1.5
MAX_HOLD = timedelta(days=10)
PRICES_PER_MTOK = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}
DEFAULT_WINDOWS_UTC = [time(3, 0), time(15, 0)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    telegram_token: str = ""
    telegram_chat_id: int = 0
    anthropic_api_key: str = ""
    tz_offset_hours: int = 5
    windows: list[str] = ["08:00", "20:00"]
    start_capital_kzt: float = 200_000
    claude_monthly_budget_usd: float = 10
    server_cost_usd_month: float = 5
    cheap_model: str = "claude-haiku-5-5"
    strong_model: str = "claude-sonnet-5-5"
    db_path: Path = Path("data/journal.sqlite")
    healthcheck_url: str | None = None

    def window_times_utc(self) -> list[time]:
        """Локальные окна (UTC+tz_offset) -> время в UTC."""
        today = datetime.now(UTC).date()
        result = []
        for w in self.windows:
            hh, mm = map(int, w.split(":"))
            local = datetime.combine(today, time(hh, mm)) - timedelta(hours=self.tz_offset_hours)
            result.append(local.time())
        return result
