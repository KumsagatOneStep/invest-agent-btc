import anthropic

from invest_agent.config import Settings


def make_client(settings: Settings) -> anthropic.Anthropic:
    # окно держит транзакцию и lock бота, пока ждёт Claude: не дольше минуты на попытку
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60, max_retries=2)
