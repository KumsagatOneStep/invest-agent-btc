import json
from datetime import UTC, datetime
from pathlib import Path

from invest_agent.backtest.engine import Period, run_backtest, summarize
from invest_agent.backtest.gate import GateVerdict, gate
from invest_agent.data.candles import CandleStore
from invest_agent.models import SymbolRules
from invest_agent.strategies.base import Strategy


class HoldoutAlreadyRun(Exception):
    pass


def _read(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(path)


class HoldoutLog:
    def __init__(
        self, path: Path = Path("data/holdout_log.json"), approved_path: Path | None = None
    ) -> None:
        self.path = path
        self.approved_path = approved_path or path.parent / "approved_strategies.json"

    def entries(self) -> dict[str, dict]:
        return _read(self.path, {})

    def already_run(self, version: str) -> bool:
        return version in self.entries()

    def record(self, version: str, verdict: GateVerdict, summary: dict) -> None:
        """summary должен содержать `name` и `params` (нужны для approved_strategies.json)."""
        entries = self.entries()
        entries[version] = {
            "passed": verdict.passed,
            "reasons": verdict.reasons,
            "summary": summary,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        _write(self.path, entries)
        if verdict.passed:
            approved = [a for a in self.approved() if a["version"] != version]
            approved.append(
                {"name": summary["name"], "params": summary["params"], "version": version}
            )
            _write(self.approved_path, approved)

    def approved(self) -> list[dict]:
        return _read(self.approved_path, [])


def run_holdout(
    strategy: Strategy,
    store: CandleStore,
    rules: dict[str, SymbolRules],
    period: Period,
    start_equity: float,
    log: HoldoutLog,
) -> tuple[GateVerdict, dict]:
    version = strategy.version()
    if log.already_run(version):
        raise HoldoutAlreadyRun(version)
    result = run_backtest([strategy], store, rules, period.start, period.end, start_equity)
    verdict = gate(result)
    summary = {
        **summarize(result),
        "name": strategy.name,
        "params": strategy.params,
        "period": [period.start.isoformat(), period.end.isoformat()],
    }
    log.record(version, verdict, summary)
    return verdict, summary
