import argparse
import sys
from datetime import timedelta

from invest_agent.backtest.__main__ import (
    BACKTEST_YEARS,
    BENCH,
    DATA_DIR,
    RULES_FILE,
    CliError,
    load_rules,
)
from invest_agent.config import Settings
from invest_agent.data.candles import CandleStore
from invest_agent.journal import connect
from invest_agent.research.events import breakout, crash_volume, momentum_top
from invest_agent.research.panel import load_panel
from invest_agent.research.study import ROUND_TRIP_COST, StudyResult, event_study


def pct(x: float) -> str:
    return f"{100 * x:+.2f}%"


def print_result(title: str, r: StudyResult, top_symbols: int = 5) -> None:
    print(f"\n{title}")
    print(f"Дат с событием: {r.n_dates}, событий: {r.n_events}")
    print(f"Доходность после события:       {pct(r.mean_event)}")
    print(f"  после комиссий ({100 * ROUND_TRIP_COST:.1f}% за круг): {pct(r.mean_net)}")
    print(f"Избыток к среднему по вселенной: {pct(r.mean_excess)}  (t = {r.t_excess:.2f})")
    print(f"Избыток к BTC:                   {pct(r.mean_vs_bench)}")
    print("По годам (дат, избыток):")
    for year, (n, ex) in sorted(r.by_year.items()):
        print(f"  {year}: {n:4d}  {pct(ex)}")
    total = r.by_symbol["contrib"].abs().sum()
    print(f"Монет с событиями: {len(r.by_symbol)}; больше всего вклада:")
    for sym, row in r.by_symbol.head(top_symbols).iterrows():
        share = abs(row["contrib"]) / total if total else 0.0
        print(f"  {sym:12s} событий {int(row['events']):4d}  средн. {pct(row['mean_fwd'])}"
              f"  доля вклада {100 * share:.0f}%")  # fmt: skip
    print("Оговорка: |t| < 2 — от шума не отличить; при многих проверках порог строже.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m invest_agent.research")
    parser.add_argument("--size", type=int, default=30, help="монет во вселенной (топ по объёму)")
    parser.add_argument("--horizon", type=int, default=7, help="дней удержания")
    parser.add_argument("--step", type=int, help="шаг дат, по умолчанию = horizon")
    sub = parser.add_subparsers(dest="event", required=True)
    m = sub.add_parser("momentum", help="топ монет по доходности за прошлые дни")
    m.add_argument("--lookback", type=int, default=28)
    m.add_argument("--top", type=int, default=3)
    m.add_argument("--bottom", action="store_true", help="худшие монеты вместо лучших")
    b = sub.add_parser("breakout", help="закрытие выше максимума N дней")
    b.add_argument("--n", type=int, default=40)
    c = sub.add_parser("crash", help="резкое падение на большом объёме")
    c.add_argument("--drop", type=float, default=0.08)
    c.add_argument("--vol-k", type=float, default=2.0)
    args = parser.parse_args(argv)

    conn = connect(Settings().db_path)
    store = CandleStore(conn)
    try:
        rules = load_rules(DATA_DIR / RULES_FILE)
        end = store.last_close_time(BENCH, "1d")
        if end is None:
            raise CliError("в базе нет дневных свечей BTCUSDT — сначала `backtest fetch`")
        panel = load_panel(store, rules, end - timedelta(days=365 * BACKTEST_YEARS), end, args.size)
    except CliError as e:
        print(e, file=sys.stderr)
        return 2

    if args.event == "momentum":
        events = momentum_top(panel.closes, panel.member, args.lookback, args.top, args.bottom)
        side = "худшие" if args.bottom else "лучшие"
        title = f"Моментум: {side} {args.top} за {args.lookback} д"
    elif args.event == "breakout":
        events = breakout(panel.closes, panel.member, args.n)
        title = f"Пробой максимума {args.n} д"
    else:
        events = crash_volume(panel.closes, panel.volumes, panel.member, args.drop, args.vol_k)
        title = f"Падение ≥{100 * args.drop:.0f}% при объёме ≥{args.vol_k:g}× среднего"
    title += f", удержание {args.horizon} д, вселенная топ-{args.size}"
    r = event_study(events, panel.closes, panel.member, args.horizon, args.step or args.horizon, BENCH)
    print_result(title, r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
