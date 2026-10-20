import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from invest_agent.backtest.engine import (
    D1_LOOKBACK,
    H4_LOOKBACK,
    Period,
    btc_hold_pct,
    run_backtest,
    split_periods,
    summarize,
    universe_anchors,
)
from invest_agent.backtest.holdout import HoldoutAlreadyRun, HoldoutLog, run_holdout
from invest_agent.backtest.report import build_report, load_validation, save_validation
from invest_agent.config import MIN_RR, Settings
from invest_agent.data.binance import BinanceClient
from invest_agent.data.candles import CandleStore
from invest_agent.models import SymbolRules
from invest_agent.strategies.base import Strategy
from invest_agent.strategies.registry import CLASSES
from invest_agent.universe import candidate_symbols, select_universe

BACKTEST_YEARS = 3
START_EQUITY_USDT = 400.0  # ~200 000 ₸
DATA_DIR = Path("data")
RULES_FILE = "symbol_rules.json"
VALIDATION_FILE = "validation_log.json"
REPORT_FILE = "backtest_report.md"
BENCH = "BTCUSDT"
# Окна индикаторов не длиннее истории, которую видит стратегия (иначе сигнала не будет никогда)
LOOKBACK_LIMITS = {"donchian_n": H4_LOOKBACK, "rsi_n": H4_LOOKBACK, "trend_ema": D1_LOOKBACK}


class CliError(Exception):
    pass


def make_strategy(name: str, raw_params: list[str]) -> Strategy:
    cls = CLASSES[name]
    params: dict[str, float] = {}
    for item in raw_params:
        key, sep, value = item.partition("=")
        if not sep or key not in cls.DEFAULTS:
            raise CliError(f"неизвестный параметр {key!r}; допустимые: {', '.join(cls.DEFAULTS)}")
        try:
            number = float(value)
        except ValueError:
            raise CliError(f"значение параметра {key} не число: {value!r}") from None
        if isinstance(cls.DEFAULTS[key], int):
            if number != int(number):
                raise CliError(f"параметр {key} должен быть целым: {value!r}")
            params[key] = int(number)
        else:
            params[key] = number
        _check_param(key, params[key])
    return cls(params)


def _check_param(key: str, value: float) -> None:
    """Значения, которые стратегия молча исправила бы или с которыми никогда не сработает."""
    if value <= 0:
        raise CliError(f"параметр {key} должен быть больше нуля: {value:g}")
    if key == "rr" and value < MIN_RR:
        raise CliError(f"rr={value:g} меньше минимума {MIN_RR:g} (стратегия всё равно взяла бы {MIN_RR:g})")
    limit = LOOKBACK_LIMITS.get(key)
    if limit is not None and value >= limit:
        raise CliError(f"{key}={value:g}: окно должно быть меньше {limit} свечей истории")


def save_rules(path: Path, rules: dict[str, SymbolRules]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([vars(r) for r in rules.values()]))


def load_rules(path: Path) -> dict[str, SymbolRules]:
    if not path.exists():
        raise CliError("нет data/symbol_rules.json — сначала выполните `fetch`")
    return {r["symbol"]: SymbolRules(**r) for r in json.loads(path.read_text())}


def data_periods(store: CandleStore) -> tuple[Period, Period, Period]:
    end = store.last_close_time(BENCH, "1h")
    if end is None:
        raise CliError("в базе нет свечей BTCUSDT 1h — сначала выполните `fetch`")
    return split_periods(end - timedelta(days=365 * BACKTEST_YEARS), end)


def fetch(client: BinanceClient, store: CandleStore, rules_path: Path, now: datetime) -> None:
    rules = client.symbol_rules()
    save_rules(rules_path, rules)
    symbols = candidate_symbols(rules)
    for i, symbol in enumerate(symbols, 1):
        print(f"1d {i}/{len(symbols)} {symbol}", flush=True)
        store.refresh(client, symbol, "1d", now)
    # 1h/4h нужны только монетам, которые хоть раз попадали во вселенную (фильтр объёма)
    start = now - timedelta(days=365 * BACKTEST_YEARS)
    daily = {s: store.get(s, "1d", datetime(2000, 1, 1, tzinfo=UTC), now) for s in symbols}
    anchors = set(universe_anchors(start, now))
    anchors.add(now)
    anchors.update(p.start for p in split_periods(start, now))
    needed = sorted({s for a in sorted(anchors) for s in select_universe(rules, daily, a)})
    for i, symbol in enumerate(needed, 1):
        for interval in ("4h", "1h"):
            print(f"{interval} {i}/{len(needed)} {symbol}", flush=True)
            store.refresh(client, symbol, interval, now)


def _print_summary(label: str, s: dict) -> None:
    print(
        f"{label}: сделок {int(s['trades'])}, прибыльных {s['win_pct']:.0f}%, "
        f"net {s['net_pnl']:.2f} USDT, max DD {100 * s['max_dd']:.1f}%, "
        f"BTC buy&hold {s['btc_hold_pct']:+.1f}%"
    )


def validate(
    strategy: Strategy,
    store: CandleStore,
    rules: dict[str, SymbolRules],
    periods: tuple[Period, Period, Period],
    log_path: Path,
    windows_utc: list[time],
) -> dict:
    train, val, _holdout = periods  # свечи holdout не читаются: end каждого прогона <= его начала
    entry: dict = {"name": strategy.name, "params": strategy.params}
    for label, period in (("train", train), ("validation", val)):
        result = run_backtest(
            [strategy], store, rules, period.start, period.end, START_EQUITY_USDT,
            windows_utc=windows_utc,
        )  # fmt: skip
        entry[label] = {**summarize(result), "btc_hold_pct": btc_hold_pct(store, period)}
        _print_summary(label, entry[label])
    save_validation(log_path, strategy.version(), entry)
    return entry


def holdout_cmd(
    strategy: Strategy,
    store: CandleStore,
    rules: dict[str, SymbolRules],
    periods: tuple[Period, Period, Period],
    log: HoldoutLog,
    windows_utc: list[time],
) -> bool:
    hold = periods[2]
    try:
        verdict, summary = run_holdout(
            strategy, store, rules, hold, START_EQUITY_USDT, log, windows_utc
        )
    except HoldoutAlreadyRun:
        print(f"Версия {strategy.version()} уже запускалась на holdout — повтор запрещён.")
        return False
    _print_summary("holdout", summary)
    print("Ворота отбора:", "пройдены" if verdict.passed else "не пройдены")
    for reason in verdict.reasons:
        print(" -", reason)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m invest_agent.backtest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("report")
    for cmd in ("validate", "holdout"):
        p = sub.add_parser(cmd)
        p.add_argument("--strategy", required=True, choices=sorted(CLASSES))
        p.add_argument("--param", action="append", default=[], metavar="k=v")
    args = parser.parse_args(argv)

    settings = Settings()  # без .env — значения по умолчанию (окна как у бота)
    db = settings.db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    CandleStore.ensure_schema(conn)
    store = CandleStore(conn)
    try:
        if args.cmd == "fetch":
            fetch(BinanceClient(), store, DATA_DIR / RULES_FILE, datetime.now(UTC))
        elif args.cmd == "report":
            log = HoldoutLog(DATA_DIR / "holdout_log.json")
            text = build_report(load_validation(DATA_DIR / VALIDATION_FILE), log.entries())
            (DATA_DIR / REPORT_FILE).write_text(text)
            print(f"Отчёт записан в {DATA_DIR / REPORT_FILE}")
        else:
            strategy = make_strategy(args.strategy, args.param)
            rules = load_rules(DATA_DIR / RULES_FILE)
            periods = data_periods(store)
            windows = settings.window_times_utc()
            if args.cmd == "validate":
                validate(strategy, store, rules, periods, DATA_DIR / VALIDATION_FILE, windows)
            else:
                log = HoldoutLog(DATA_DIR / "holdout_log.json")
                if not holdout_cmd(strategy, store, rules, periods, log, windows):
                    return 1
    except CliError as e:
        print(e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
