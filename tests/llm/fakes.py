from datetime import UTC, datetime
from types import SimpleNamespace

NOW = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)


def usage(i=1000, o=500):
    return SimpleNamespace(input_tokens=i, output_tokens=o)


class FakeClient:
    """Подставной клиент: те же методы, что у anthropic.Anthropic, без сети."""

    def __init__(self, parse=None, create=None):
        self.calls: list[tuple[str, dict]] = []
        self._parse, self._create = parse, create
        self.messages = SimpleNamespace(parse=self._do_parse)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._do_create))

    def _do_parse(self, **kw):
        self.calls.append(("parse", kw))
        if isinstance(self._parse, Exception):
            raise self._parse
        return self._parse

    def _do_create(self, **kw):
        self.calls.append(("create", kw))
        if isinstance(self._create, Exception):
            raise self._create
        return self._create


def spend(journal, usd: float, ts: datetime = NOW) -> None:
    journal.conn.execute(
        "INSERT INTO api_usage VALUES (?, 'claude-haiku-5-5', 'filter', 0, 0, ?)",
        (ts.isoformat(), usd),
    )
