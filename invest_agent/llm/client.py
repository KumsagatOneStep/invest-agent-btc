import anthropic

from invest_agent.config import Settings


def make_client(settings: Settings) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)
