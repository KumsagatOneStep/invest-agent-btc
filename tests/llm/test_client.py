from invest_agent.config import Settings
from invest_agent.llm.client import make_client


def test_client_has_bounded_timeout_and_retries():
    client = make_client(Settings(anthropic_api_key="test-key"))
    assert client.timeout == 60
    assert client.max_retries == 2
