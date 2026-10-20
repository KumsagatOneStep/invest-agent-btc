import json
import logging
from pathlib import Path

from invest_agent.strategies.base import Strategy
from invest_agent.strategies.meanrev import MeanReversion
from invest_agent.strategies.trend import TrendBreakout

log = logging.getLogger(__name__)

CLASSES = {cls.name: cls for cls in (TrendBreakout, MeanReversion)}


def load_approved(path: Path = Path("data/approved_strategies.json")) -> list[Strategy]:
    if not path.exists():
        return []
    result: list[Strategy] = []
    for entry in json.loads(path.read_text()):
        cls = CLASSES.get(entry["name"])
        if cls is None:
            log.warning("approved strategy %r: unknown name, skipped", entry["name"])
            continue
        strategy = cls(entry["params"])
        if strategy.version() != entry["version"]:
            log.warning("approved strategy %r: version mismatch, skipped", entry["name"])
            continue
        result.append(strategy)
    return result
