# Инвест-агент v1 (бумажная торговля крипто-сигналами) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram-агент, который дважды в день считает сигналы на покупку крипты на споте Binance, фильтрует и объясняет их через Claude, ведёт два бумажных портфеля и честную статистику — без реальных денег и без ключей биржи.

**Architecture:** Чистые функции (индикаторы, стратегии, модель исполнения, риск) общие для бэктеста и живого режима. Состояние живого режима — SQLite-журнал; итог каждой позиции пересчитывается детерминированно из часовых свечей. Оркестратор окна (`cycle.py`) вызывается планировщиком внутри процесса Telegram-бота (aiogram + APScheduler), всё в одном Docker-контейнере на VPS.

**Tech Stack:** Python 3.12, httpx, pandas, aiogram 3, APScheduler 3.x, anthropic (официальный SDK), pydantic-settings, SQLite (stdlib `sqlite3`), pytest + respx, ruff, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-20-invest-agent-design.md` — читать вместе с планом.

## Global Constraints

- Только покупка на споте, пары к USDT. Никаких ордеров на биржу, никаких ключей Binance в коде и `.env`.
- Комиссия 0,1% на каждую сторону, проскальзывание 0,05% на сторону — и в бэктесте, и в бумажной торговле (одна функция).
- Риск 1% счёта на сделку; 0,5% в состоянии REDUCED. Позиция ≤ 25% счёта; ≤ 4 открытых (включая ожидающие входа); суммарный риск открытых ≤ 4%; ≤ 1 позиции на монету; меньше минимального ордера Binance — не отправлять.
- Просадка от максимума: −10% → REDUCED (обратно при новом максимуме); −15% → HALTED (снимается только `/resume`, с новым бумажным этапом).
- Цель ≥ 1,5 × риск (target − entry_high ≥ 1,5 × (entry_high − stop)). Принудительный выход через 10 дней.
- Окна 08:00 и 20:00 UTC+5 (`TZ_OFFSET_HOURS=5`, окна в настройках). Недельная статистика — воскресенье 20:00.
- Стратегия видит только закрытые свечи (`close_time <= now`).
- Числа в сообщениях берутся только из сигнала и расчёта размера, никогда из текста модели.
- Модели: фильтр и сводки — `claude-haiku-5-5`; объяснение — `claude-sonnet-5-5`. Цены за 1M токенов: Haiku $0.10 / $0.50, Sonnet $2.00 / $10.00 (вход / выход).
- Лимит Claude API в месяц — `CLAUDE_MONTHLY_BUDGET_USD=10`: ≥ 80% → объяснение тоже на Haiku; ≥ 100% → Claude не вызывается, сигналы получают REVIEW; оба события — сообщение пользователю.
- Стартовый счёт обоих портфелей — 200 000 ₸ в USDT по курсу на момент старта этапа.
- Бот отвечает и пишет только `TELEGRAM_CHAT_ID`. Все тексты — на русском.
- Время в коде — только aware `datetime` в UTC; UTC+5 только при выводе и в расписании.

## Review Focus

1. **Binance недоступен или отвечает 429/451/5xx посреди окна** — окно не оставляет полузаписанное состояние, пользователь получает «сигналы не посчитаны, причина …». Тест: Task 12 `test_window_binance_down_reports_failure_and_writes_nothing`.
2. **Перезапуск бота / повторный вызов того же окна** — второй прогон ничего не дублирует. Тест: Task 12 `test_window_is_idempotent`.
3. **Пропуски в часовых свечах или монета снята с торгов при открытой позиции** — позиция остаётся открытой, без падения; при пропаже пары — предупреждение в сводке. Тесты: Task 5 `test_gap_in_candles_keeps_position_open`, Task 12 `test_delisted_symbol_with_open_position_warns`.
4. **Нулевой ATR, стоп ≥ входа, цена ниже стопа на момент сигнала** — кандидат отклоняется с причиной, без деления на ноль. Тесты: Task 6 `test_rejects_non_positive_risk`, Task 7 `test_strategy_returns_none_on_flat_market`.
5. **Текст Claude с HTML-символами, чужими числами или слишком длинный** — экранируется, числа сигнала не меняются, сообщение ≤ 4096 символов. Тест: Task 11 `test_signal_message_escapes_and_truncates_explanation`.

---

## Структура файлов

```
invest_agent/
  config.py          настройки (.env), константы риска и цен
  models.py          Candle, SignalCandidate, Outcome, Sizing и т. п.
  indicators.py      ema, atr, rsi, donchian_high
  data/binance.py    публичный REST Binance: свечи, фильтры пар, объёмы
  data/candles.py    кэш свечей в SQLite
  data/fx.py         курс USD→KZT
  universe.py        отбор монет
  execution.py       модель исполнения (общая для бэктеста и бумаги)
  risk.py            размер позиции, лимиты, ступени просадки
  strategies/        base.py, trend.py, meanrev.py, registry.py
  backtest/          engine.py, gate.py, holdout.py, __main__.py (CLI)
  journal.py         схема SQLite и операции журнала/портфелей
  llm/               client.py, filter.py, explain.py, budget.py
  messages.py        тексты Telegram
  stats.py           статистика, бенчмарк BTC, критерии перехода
  cycle.py           одно окно целиком
  bot.py             aiogram, команды, планировщик, бэкап
tests/               зеркально модулям
```

---

### Task 1: Каркас, настройки, модели данных

**Files:**
- Create: `pyproject.toml`, `Makefile`, `.gitignore`, `.env.example`, `invest_agent/__init__.py`, `invest_agent/config.py`, `invest_agent/models.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Settings` (pydantic-settings, env-файл `.env`): `telegram_token: str`, `telegram_chat_id: int`, `anthropic_api_key: str`, `tz_offset_hours: int = 5`, `windows: list[str] = ["08:00","20:00"]`, `start_capital_kzt: float = 200_000`, `claude_monthly_budget_usd: float = 10`, `server_cost_usd_month: float = 5`, `cheap_model: str = "claude-haiku-5-5"`, `strong_model: str = "claude-sonnet-5-5"`, `db_path: Path = Path("data/journal.sqlite")`, `healthcheck_url: str | None = None`; метод `window_times_utc() -> list[time]`.
  - Константы в `config.py`: `FEE = 0.001`, `SLIPPAGE = 0.0005`, `RISK_NORMAL = 0.01`, `RISK_REDUCED = 0.005`, `MAX_POSITION_FRAC = 0.25`, `MAX_OPEN = 4`, `MAX_TOTAL_RISK = 0.04`, `DD_REDUCED = 0.10`, `DD_HALT = 0.15`, `MIN_RR = 1.5`, `MAX_HOLD = timedelta(days=10)`, `PRICES_PER_MTOK = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}`.
  - `models.py` (frozen dataclasses): `Candle(open_time, close_time: datetime, open, high, low, close, volume, quote_volume: float)`; `SignalCandidate(symbol, strategy: str, created_at: datetime, entry_low, entry_high, stop, target: float, features: dict[str, float])`; `Outcome(status: Literal["pending","expired","open","stop","target","time"], entry_time: datetime|None, entry_price: float|None, exit_time: datetime|None, exit_price: float|None)`; `Sizing(qty, notional, risk_usdt: float)`; `Rejection(reason: str)`; `SymbolRules(symbol: str, tick_size, step_size, min_notional: float)`.

- [ ] **Step 1:** `git init` в `~/invest-agent`; `.gitignore`: `.env`, `data/`, `.venv/`, `__pycache__/`. `.env.example` со всеми ключами `Settings` без значений. `Makefile`: `install` (venv + `pip install -e .[dev]`), `test` (`pytest -q`), `check` (`ruff check . && pytest -q`).
- [ ] **Step 2: Тест** `tests/test_config.py::test_window_times_utc` — при `windows=["08:00","20:00"]`, `tz_offset_hours=5` → `[time(3,0), time(15,0)]`; `test_defaults` — `claude_monthly_budget_usd == 10`, `cheap_model == "claude-haiku-5-5"`.
- [ ] **Step 3:** Запустить `pytest tests/test_config.py -v` — FAIL (нет модуля).
- [ ] **Step 4:** Реализовать `config.py`, `models.py`.
- [ ] **Step 5:** `make check` — PASS.
- [ ] **Step 6:** Commit `chore: каркас проекта, настройки и модели данных`.

---

### Task 2: Индикаторы

**Files:** Create `invest_agent/indicators.py`, `tests/test_indicators.py`

**Interfaces:**
- Produces (все на `pd.Series`, индекс сохраняется): `ema(s, n) -> Series`; `atr(df, n) -> Series` (Wilder, df с колонками high/low/close); `rsi(s, n) -> Series` (Wilder); `donchian_high(df, n) -> Series` (максимум high за n **предыдущих** свечей, без текущей).

- [ ] **Step 1: Тесты** на маленьких рядах с посчитанными вручную значениями: `test_ema_matches_manual`, `test_atr_constant_range_equals_range` (high−low=2 у всех свечей, без гэпов → ATR=2), `test_rsi_all_up_is_100`, `test_donchian_excludes_current_bar`.
- [ ] **Step 2:** `pytest tests/test_indicators.py -v` — FAIL.
- [ ] **Step 3:** Реализовать.
- [ ] **Step 4:** PASS. **Step 5:** Commit `feat: индикаторы`.

---

### Task 3: Данные Binance и кэш свечей

**Files:** Create `invest_agent/data/binance.py`, `invest_agent/data/candles.py`, `invest_agent/data/fx.py`, `tests/data/test_binance.py`, `tests/data/test_candles.py`, `tests/data/test_fx.py`

**Interfaces:**
- Consumes: `Candle`, `SymbolRules`.
- Produces:
  - `BinanceClient(base_url="https://data-api.binance.vision", http: httpx.Client | None)`: `klines(symbol, interval: Literal["1h","4h","1d"], start: datetime, end: datetime) -> list[Candle]` (постранично по 1000, только свечи с `close_time <= end`); `symbol_rules() -> dict[str, SymbolRules]` (из `/api/v3/exchangeInfo`: `PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize`, `NOTIONAL.minNotional`; только `status == "TRADING"`, `quoteAsset == "USDT"`, `isSpotTradingAllowed`); `BinanceError(Exception)` на любой не-200 и сетевую ошибку (после 3 попыток с паузой 1/2/4 с на 429 и 5xx).
  - `CandleStore(conn: sqlite3.Connection)`: `upsert(symbol, interval, candles)`, `get(symbol, interval, start, end) -> list[Candle]`, `last_close_time(symbol, interval) -> datetime | None`, `refresh(client, symbol, interval, now)` — докачивает от последней свечи (или от `now − 4 года`) до `now`.
  - `usd_kzt(now, conn, http) -> float | None` — `https://open.er-api.com/v6/latest/USD`, `rates.KZT`, кэш в таблице `fx` на сутки; при ошибке — последний известный курс; если его нет — `None` (тогда в сообщениях только $).
  - Функция `to_frame(candles) -> pd.DataFrame` (индекс `close_time`, колонки open/high/low/close/volume).

- [ ] **Step 1: Тесты** (respx): `test_klines_paginates_and_drops_unclosed` (две страницы, последняя свеча с `close_time > end` отброшена); `test_klines_retries_429_then_raises` (4 ответа 429 → `BinanceError`); `test_451_raises_immediately`; `test_symbol_rules_filters_non_usdt_and_halted`; `test_store_refresh_is_incremental` (второй `refresh` запрашивает только с последней свечи); `test_fx_falls_back_to_cached_rate`.
- [ ] **Step 2:** FAIL. **Step 3:** Реализовать. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat: данные Binance, кэш свечей, курс тенге`.

---

### Task 4: Отбор монет

**Files:** Create `invest_agent/universe.py`, `tests/test_universe.py`

**Interfaces:**
- Consumes: `SymbolRules`, дневные `Candle` (с `quote_volume`).
- Produces: `select_universe(rules: dict[str, SymbolRules], daily: dict[str, list[Candle]], now: datetime, size: int = 10) -> list[str]`; константы `EXCLUDED_BASES` (стейблкоины: USDC, FDUSD, TUSD, DAI, USDP, EUR, AEUR, USDE; обёрнутые: WBTC, WBETH, BETH, STETH) и правило «база кончается на UP/DOWN/BULL/BEAR — исключить».

Правило: сумма `quote_volume` за последние 30 закрытых дней; история ≥ 365 дней (первая дневная свеча ≤ now − 365 дн.); BTCUSDT и ETHUSDT всегда включены; сортировка по объёму, всего `size` штук. Та же функция используется в бэктесте на исторические даты (раз в месяц).

- [ ] **Step 1: Тесты:** `test_always_includes_btc_eth_even_if_low_volume`, `test_excludes_stablecoins_wrapped_and_leveraged`, `test_excludes_history_shorter_than_year`, `test_picks_top_by_30d_quote_volume`, `test_uses_only_data_before_now` (огромный объём после `now` не влияет).
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: отбор монет`.

---

### Task 5: Модель исполнения

**Files:** Create `invest_agent/execution.py`, `tests/test_execution.py`

**Interfaces:**
- Consumes: `SignalCandidate`, `Candle`, `Outcome`, `FEE`, `SLIPPAGE`, `MAX_HOLD`.
- Produces: `simulate(sig: SignalCandidate, candles_1h: list[Candle], expires_at: datetime) -> Outcome`; `net_pnl(outcome: Outcome, qty: float) -> tuple[float, float]` → `(pnl_usdt, fees_usdt)`.

Правила (детерминированно, только свечи с `open_time >= sig.created_at`):
1. Вход — первая свеча с `open_time < expires_at`, у которой `low <= entry_high` и `high >= entry_low`. Цена входа = `entry_high × (1 + SLIPPAGE)`. Нет такой свечи, а данные уже покрывают `expires_at` → `expired`; не покрывают → `pending`.
2. В свече входа: если `low <= stop` → выход `stop` в этой же свече. Цель в свече входа не засчитывается.
3. Дальше по свечам: если `low <= stop` → `stop` (даже если `high >= target` в той же свече); иначе если `high >= target` → `target`.
4. Цена выхода по стопу = `min(open, stop) × (1 − SLIPPAGE)`; по цели = `target × (1 − SLIPPAGE)`.
5. Первая свеча с `close_time >= entry_time + MAX_HOLD` без стопа/цели → `time`, цена `close × (1 − SLIPPAGE)`.
6. Свечи кончились раньше → `open`. Пропуски в свечах не ошибка.
7. `net_pnl`: `qty × (exit − entry) − FEE × qty × (entry + exit)`.

- [ ] **Step 1: Тесты** — по одному на каждое правило: `test_pending_before_expiry`, `test_expired_when_zone_never_touched`, `test_entry_at_worst_price_plus_slippage`, `test_stop_in_entry_candle`, `test_target_ignored_in_entry_candle`, `test_both_hit_counts_as_stop`, `test_gap_below_stop_fills_at_open`, `test_time_exit_after_10_days`, `test_gap_in_candles_keeps_position_open`, `test_net_pnl_includes_fees_both_sides` (entry 100, exit 110, qty 1 → pnl `10 − 0.21`).
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: модель исполнения`.

---

### Task 6: Риск, размер позиции, ступени просадки

**Files:** Create `invest_agent/risk.py`, `tests/test_risk.py`

**Interfaces:**
- Consumes: `SignalCandidate`, `Sizing`, `Rejection`, `SymbolRules`, константы риска.
- Produces:
  - `OpenRisk(symbol: str, risk_usdt: float, notional: float)`.
  - `size_position(sig, equity: float, risk_pct: float, open_: list[OpenRisk], rules: SymbolRules) -> Sizing | Rejection`. Порядок проверок и тексты причин: `"stop>=entry"` (entry_high − stop ≤ 0), `"rr<1.5"`, `"symbol already open"`, `"max open positions"`, `"total risk limit"` (сумма открытого риска + новый > 4% equity — сначала пробовать уменьшить qty до остатка лимита, отказ только если остаток 0), `"below min notional"`. qty = risk / (entry_high − stop), затем ограничение 25% equity по notional, затем округление вниз до `step_size`.
  - `round_prices(sig, rules) -> SignalCandidate` — к `tick_size`: entry_low вверх, entry_high вниз, stop вниз, target вниз.
  - `DDState = Literal["NORMAL","REDUCED","HALTED"]`; `step_drawdown(state: DDState, equity: float, peak: float) -> tuple[DDState, list[str]]` — события `"reduced"`, `"recovered"`, `"halted"`; HALTED не снимается этой функцией. `risk_pct_for(state) -> float`.

- [ ] **Step 1: Тесты:** `test_one_percent_risk_qty` (equity 400, entry 100, stop 95 → qty 0.8 до ограничений); `test_caps_at_25_percent_equity`; `test_rejects_non_positive_risk`; `test_rejects_bad_rr`; `test_one_position_per_symbol`; `test_max_four_open`; `test_shrinks_to_remaining_total_risk`; `test_rejects_below_min_notional`; `test_round_prices_directions`; `test_drawdown_ladder` (NORMAL→REDUCED на −10%, остаётся REDUCED на −5%, →NORMAL на новом максимуме, →HALTED на −15%, HALTED остаётся на −1%).
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: риск и просадка`.

---

### Task 7: Стратегии

**Files:** Create `invest_agent/strategies/base.py`, `trend.py`, `meanrev.py`, `registry.py`, `tests/strategies/test_strategies.py`

**Interfaces:**
- Consumes: индикаторы, `to_frame`, `SignalCandidate`, `MIN_RR`.
- Produces:
  - `class Strategy(Protocol)`: `name: str`, `params: dict[str, float]`, `version() -> str` (sha1 от `name` + отсортированных params + `CODE_VERSION`), `evaluate(symbol: str, h4: pd.DataFrame, d1: pd.DataFrame, now: datetime) -> SignalCandidate | None`.
  - `closed_only(df, now) -> DataFrame` в `base.py`; `evaluate` обязан вызывать его первым.
  - `TrendBreakout(params)` и `MeanReversion(params)`; не больше 4 параметров у каждой, значения по умолчанию в классе.
  - `registry.load_approved(path=Path("data/approved_strategies.json")) -> list[Strategy]` — читает имена+params+version, прошедшие holdout (Task 8); пустой список, если файла нет.

Логика — на усмотрение исполнителя в рамках спеки §7: тренд — пробой `donchian_high` на 4ч при `close_1d > ema(d1, N)`; возврат — `rsi(4ч) < порога` при `close_1d > ema(d1, N)`. Зона входа ниже текущего close на долю ATR(4ч); стоп `entry_high − k × ATR`; цель `entry_high + rr × (entry_high − stop)`, `rr ≥ 1.5`.

- [ ] **Step 1: Тесты:** `test_no_lookahead` (для каждой стратегии: результат на данных с добавленными будущими свечами == результат на обрезанных данных); `test_trend_fires_on_breakout_in_uptrend` и `test_trend_silent_in_downtrend` на синтетике; `test_meanrev_fires_on_oversold_in_uptrend`; `test_strategy_returns_none_on_flat_market` (ATR = 0 → None); `test_signal_respects_min_rr`; `test_version_changes_with_params`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: стратегии тренда и возврата к среднему`.

---

### Task 8: Бэктест, ворота отбора, однократный holdout

**Files:** Create `invest_agent/backtest/engine.py`, `gate.py`, `holdout.py`, `__main__.py`, `tests/backtest/test_engine.py`, `test_gate.py`, `test_holdout.py`

**Interfaces:**
- Consumes: `CandleStore`, `select_universe`, `Strategy`, `simulate`, `net_pnl`, `size_position`, `round_prices`, `step_drawdown`, `risk_pct_for`.
- Produces:
  - `Trade(symbol, strategy, created_at, entry_time, exit_time, status, pnl_usdt, fees_usdt)`; `BacktestResult(trades: list[Trade], equity: pd.Series, start, end)` с `net_pnl()`, `max_drawdown()`.
  - `run_backtest(strategies: list[Strategy], store: CandleStore, rules: dict[str, SymbolRules], start: datetime, end: datetime, start_equity: float) -> BacktestResult` — окна в `Settings.window_times_utc()` каждый день; вселенная пересчитывается 1-го числа каждого месяца по данным до этой даты; срок годности сигнала = следующее окно; те же лимиты и ступени просадки, что в живом режиме (HALTED в бэктесте = больше не открывать сделок до конца периода).
  - `split_periods(start, end) -> tuple[Period, Period, Period]` — holdout = последние 365 дней, validation = 365 дней перед ним, train = остальное.
  - `gate(result: BacktestResult) -> GateVerdict(passed: bool, reasons: list[str])`: `net_pnl > 0`, `≥ 30` сделок, в ≥ 2 из 3 равных под-периодов `net_pnl ≥ 0`.
  - `HoldoutLog(path=Path("data/holdout_log.json"))`: `already_run(version) -> bool`, `record(version, verdict, summary)`, `approved() -> list[dict]`; запись в `data/approved_strategies.json` для прошедших.
  - CLI: `python -m invest_agent.backtest fetch` (история 1h/4h/1d за 4 года для всех USDT-пар, прошедших фильтры Task 4, кроме объёма), `validate --strategy NAME [--param k=v ...]` (train + validation, печать отчёта), `holdout --strategy NAME [--param k=v ...]` (отказ с сообщением, если `version` уже запускалась; иначе запуск, gate, запись), `report` (Markdown-отчёт в `data/backtest_report.md`: по каждой версии — сделки, % прибыльных, net, max DD, сравнение с BTC buy&hold, сколько версий проверено на holdout).

- [ ] **Step 1: Тесты:** `test_engine_uses_same_execution_and_limits` (синтетическая монета, 5 сигналов подряд → не больше 4 открытых); `test_engine_no_lookahead_universe` (монета, появившаяся позже, не торгуется раньше); `test_split_last_365_days_is_holdout`; `test_gate_requires_30_trades`; `test_gate_requires_two_of_three_subperiods`; `test_holdout_refuses_second_run_same_version`; `test_holdout_writes_approved_only_when_passed`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: бэктест и отбор стратегий`.

---

### Task 9: Журнал и два бумажных портфеля

**Files:** Create `invest_agent/journal.py`, `tests/test_journal.py`

**Interfaces:**
- Consumes: `SignalCandidate`, `Outcome`, `Sizing`, `OpenRisk`, `DDState`, `net_pnl`.
- Produces:
  - `connect(path) -> sqlite3.Connection` (WAL, `foreign_keys=ON`) и `migrate(conn)`: таблицы
    - `signals(id, created_at, symbol, strategy, strategy_version, entry_low, entry_high, stop, target, features_json, claude_status TEXT /* approved|rejected|review|skipped */, claude_reason, explanation, sent INTEGER)`;
    - `positions(id, signal_id, book TEXT /* main|research */, stage INTEGER, qty, risk_usdt, notional, status, entry_time, entry_price, exit_time, exit_price, pnl_usdt, fees_usdt, reject_reason)`;
    - `books(book PRIMARY KEY, stage, stage_started_at, start_equity, peak, dd_state, paused)`;
    - `runs(window_at PRIMARY KEY, started_at, finished_at, status, error)`;
    - `api_usage(ts, model, purpose, input_tokens, output_tokens, cost_usd)`;
    - `coins(month PRIMARY KEY, symbols_json)`; `fx(day PRIMARY KEY, usd_kzt)`; `candles(...)` (из Task 3).
  - `Journal(conn)`: `add_signal(sig, version) -> int`; `set_claude(signal_id, status, reason, explanation)`; `add_position(signal_id, book, stage, sizing | rejection)`; `active_positions(book) -> list[PositionRow]` (status `pending`/`open`); `apply_outcome(position_id, outcome)` (для финальных статусов записывает pnl/fees через `net_pnl`); `open_risk(book) -> list[OpenRisk]`; `equity(book, marks: dict[str, float]) -> float` = `start_equity` + сумма закрытых pnl текущего этапа + нереализованный pnl открытых текущего этапа по `marks`; `book(book) -> BookRow`; `update_book(book, **fields)`; `start_new_stage(book, start_equity, now)`; `begin_run(window_at) -> bool` (False, если окно уже было завершено); `finish_run(window_at, status, error=None)`.
  - Все записи одного окна — в одной транзакции (`with conn:`), выполняется вызывающим (Task 12).

- [ ] **Step 1: Тесты:** `test_books_independent` (позиция в research не занимает слот в main); `test_equity_counts_only_current_stage`; `test_new_stage_resets_peak_and_equity`; `test_apply_outcome_records_net_pnl`; `test_begin_run_twice_returns_false`; `test_rejection_is_recorded_with_reason`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: журнал и бумажные портфели`.

---

### Task 10: Claude — фильтр, объяснение, бюджет

**Files:** Create `invest_agent/llm/client.py`, `filter.py`, `explain.py`, `budget.py`, `tests/llm/test_filter.py`, `test_explain.py`, `test_budget.py`

**Interfaces:**
- Consumes: `Settings`, `Journal` (таблица `api_usage`), `PRICES_PER_MTOK`.
- Produces:
  - `FilterContext(signal: SignalCandidate, indicators: dict[str, float], btc_trend: str, open_positions: list[str], recent_results: list[str])`.
  - `FilterVerdict(BaseModel)`: `decision: Literal["approve","reject"]`, `reason: str`, `pros: list[str]`, `cons: list[str]`.
  - `FilterResult(status: Literal["approved","rejected","review"], verdict: FilterVerdict | None, error: str | None)`.
  - `filter_signal(ctx, client: anthropic.Anthropic, budget: Budget) -> FilterResult` — `client.messages.parse(model=budget.model_for("filter"), max_tokens=2048, output_format=FilterVerdict, system=..., messages=[...])`; `stop_reason == "refusal"`, любое исключение SDK, `parsed_output is None` → `review`. Системный промпт: роль скептичного риск-аналитика, разобрать «за» и «против» перед решением, не предлагать и не менять числа.
  - `explain_signal(ctx, verdict, client, budget) -> str` — `client.beta.messages.create(model=budget.model_for("explain"), max_tokens=1024, betas=["server-side-fallback-2026-07-01"], fallbacks="default", ...)` (если модель — Haiku, без `betas`/`fallbacks`); 2–3 строки «почему» и «что против», без чисел цен. Ошибка или отказ → текст, собранный из `verdict.pros`/`verdict.cons`.
  - `Budget(journal, settings, now)`: `spent_this_month() -> float`; `model_for(purpose: Literal["filter","explain","summary"]) -> str | None` (`None` при ≥ 100%; explain → cheap при ≥ 80%); `record(model, purpose, usage)` — `cost = in × p_in/1e6 + out × p_out/1e6`, неизвестная модель → цена Sonnet; `threshold_events() -> list[str]` (`"budget_80"`, `"budget_100"` — каждое один раз в месяц).
  - `filter_signal` при `budget.model_for("filter") is None` → `review` с `error="budget"` без вызова API.

- [ ] **Step 1: Тесты** (клиент — подставной объект с теми же методами): `test_approve_parsed`; `test_refusal_becomes_review`; `test_sdk_error_becomes_review`; `test_budget_exhausted_skips_api`; `test_explain_falls_back_to_verdict_text`; `test_explain_uses_cheap_model_after_80_percent`; `test_cost_calculation_haiku` (1 000 000 вход + 1 000 000 выход → $0.60); `test_threshold_event_once_per_month`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: фильтр и объяснение Claude с лимитом бюджета`.

---

### Task 11: Тексты сообщений и статистика

**Files:** Create `invest_agent/messages.py`, `invest_agent/stats.py`, `tests/test_messages.py`, `tests/test_stats.py`

**Interfaces:**
- Consumes: `SignalCandidate`, `Sizing`, `Journal`, курс `usd_kzt`.
- Produces:
  - `messages.py` (HTML parse mode, всё пользовательское и модельное через `html.escape`, итог ≤ 4096 символов — урезается объяснение): `signal_message(sig, sizing, equity, risk_pct, explanation, expires_at, usd_kzt, tz) -> str` (монета, «покупка (спот)», зона, стоп, цель, сумма $ и ₸, риск ₸ и %, «почему / что против», срок годности в UTC+5, напоминание про OCO-ордер); `window_header(now, n_signals, notes: list[str]) -> str` («Сигналов нет» при 0); `evening_summary(...)`; `weekly_message(stats_main, stats_research, criteria, costs) -> str`; `halt_message(analysis) -> str`; `reduced_message()`, `recovered_message()`; `failure_message(window_at, reason) -> str` («Сигналы не посчитаны, причина: …»); `budget_message(event) -> str`.
  - `stats.py`: `BookStats(n_signals, n_entered, n_expired, win_rate, avg_win, avg_loss, profit_factor, net_usdt, max_dd)`; `book_stats(journal, book, since, until) -> BookStats`; `costs(journal, settings, since, until) -> float` (сервер пропорционально дням + `api_usage`); `btc_hold_return(store, since, until) -> float`; `golive_check(journal, store, now) -> Criteria(days_ok, trades_ok, profit_ok, beats_btc_ok, dd_ok: bool, details: dict)` — по основному портфелю, текущему этапу: ≥ 60 дней, ≥ 100 закрытых, net > 0, доходность > BTC hold за этап, max DD ≤ 15%; `halt_analysis(journal, book) -> dict` (убытки по стратегиям, монетам, 5 худших сделок).

- [ ] **Step 1: Тесты:** `test_signal_message_contains_all_fields`; `test_signal_message_escapes_and_truncates_explanation` (объяснение с `<b>`, `&` и числом «стоп 1.0», 10 000 символов → экранировано, ≤ 4096, стоп в сообщении — из `sig.stop`); `test_signal_message_without_fx_shows_usd_only`; `test_no_signals_header`; `test_profit_factor_and_win_rate`; `test_costs_include_server_and_api`; `test_golive_requires_all_five`; `test_halt_analysis_groups_losses`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: сообщения и статистика`.

---

### Task 12: Окно целиком (оркестратор)

**Files:** Create `invest_agent/cycle.py`, `tests/test_cycle.py`

**Interfaces:**
- Consumes: всё выше.
- Produces: `Deps(settings, conn, journal, store, binance, anthropic_client, now_fn)`; `run_window(window_at: datetime, deps: Deps) -> list[str]` — возвращает тексты сообщений по порядку (отправка — Task 13).

Порядок (спека §6–§13):
1. `journal.begin_run(window_at)` → False ⇒ вернуть `[]`.
2. Сетевая часть **до** транзакции: правила пар, обновление свечей (1h/4h/1d) для вселенной + BTCUSDT + всех монет с активными позициями; вселенная пересчитывается, если для текущего месяца нет записи в `coins` (кто добавился и кто выбыл — заметкой в шапку окна); курс. Любая `BinanceError` ⇒ `finish_run(..., "failed", reason)` и вернуть `[failure_message]`.
3. Транзакция: для каждой активной позиции обоих портфелей — `simulate` (expires_at = следующее окно после `created_at`) и `apply_outcome`. Монета пропала из `symbol_rules` при открытой позиции ⇒ заметка в шапку окна.
4. Equity и peak по каждому портфелю (marks = последний close 1h); для main — `step_drawdown`; события → сообщения; `halted` ⇒ `halt_message(halt_analysis(...))`.
5. Кандидаты: `load_approved()`; пустой ⇒ заметка «нет одобренных стратегий — см. отчёт бэктеста», дальше шаг 8. Для каждой монеты вселенной × стратегии — `evaluate`, затем `round_prices`.
6. Для каждого кандидата: `add_signal`; research — `size_position` и `add_position`. Main — если не `paused` и не `HALTED`: `size_position`; при `Sizing` — `filter_signal`; `approved` ⇒ `explain_signal`, `add_position(main)`, `sent=1`, `signal_message`; иначе `set_claude` с причиной, в main позиция не создаётся. При paused/HALTED — `claude_status="skipped"`.
7. События бюджета ⇒ `budget_message`.
8. Вечернее окно ⇒ `evening_summary`; воскресенье вечер ⇒ `weekly_message` с `golive_check`.
9. `window_header` первым сообщением; `finish_run(..., "ok")`; пинг `healthcheck_url`, если задан (ошибка пинга не влияет на результат).
Любое неожиданное исключение в шаге 3–9 ⇒ откат транзакции, `finish_run("failed")`, `[failure_message]`.

- [ ] **Step 1: Тесты** (respx для Binance, подставной клиент Claude, SQLite в памяти, синтетические свечи): `test_window_sends_approved_signal_and_records_both_books`; `test_rejected_signal_only_in_research`; `test_review_not_sent`; `test_window_is_idempotent`; `test_window_binance_down_reports_failure_and_writes_nothing`; `test_halt_stops_signals_but_research_continues`; `test_paused_skips_claude_calls`; `test_no_approved_strategies_note`; `test_delisted_symbol_with_open_position_warns`; `test_sunday_evening_has_weekly_stats`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: оркестратор окна`.

---

### Task 13: Telegram-бот, планировщик, бэкап

**Files:** Create `invest_agent/bot.py`, `invest_agent/backup.py`, `tests/test_bot.py`, `tests/test_backup.py`

**Interfaces:**
- Consumes: `run_window`, `Deps`, `Journal`, `stats`, `messages`, `Budget`.
- Produces:
  - `build_dispatcher(deps) -> aiogram.Dispatcher` с фильтром «только `telegram_chat_id`» на все хендлеры; команды: `/status` (режим main: NORMAL/REDUCED/HALTED/paused, просадка, открытые и ожидающие позиции со стопами и целями), `/stats` (`book_stats` обоих портфелей с начала этапа + `golive_check`), `/pause`, `/resume` (снимает паузу; если main в HALTED — `start_new_stage(main, ...)` по текущему курсу, `dd_state="NORMAL"`, и ответ, что начат новый бумажный этап), `/budget`, `/coins`.
  - `schedule(scheduler: AsyncIOScheduler, deps, send)` — cron на каждое время из `window_times_utc()` (UTC), `misfire_grace_time=3600`; `run_window` в `asyncio.to_thread`; ежедневный бэкап в 23:00 UTC.
  - `backup(db_path, backups_dir=Path("data/backups"), keep=14)` — `sqlite3` backup API в `journal-YYYYMMDD.sqlite`, удаление старых сверх `keep`.
  - `main()` — точка входа `python -m invest_agent.bot`; при старте: `migrate`, инициализация книг (если их нет — `start_new_stage` для обеих с `start_capital_kzt / usd_kzt`; курса нет — не стартовать и написать причину в лог и в Telegram), сообщение «агент запущен, окна 08:00 и 20:00».

- [ ] **Step 1: Тесты:** `test_foreign_chat_ignored`; `test_resume_from_halt_starts_new_stage`; `test_resume_from_pause_keeps_stage`; `test_status_shows_mode_and_positions`; `test_schedule_registers_utc_windows` (03:00 и 15:00 UTC); `test_backup_keeps_last_14`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS. **Step 5:** Commit `feat: Telegram-бот, расписание, бэкапы`.

---

### Task 14: Docker и инструкция деплоя

**Files:** Create `Dockerfile`, `docker-compose.yml`, `README.md`

- [ ] **Step 1:** `Dockerfile` на `python:3.12-slim`, пользователь без root (uid 10001), `CMD ["python","-m","invest_agent.bot"]`. `docker-compose.yml`: сервис `agent`, `env_file: .env`, том `./data:/app/data`, `restart: unless-stopped`, ротация логов (`json-file`, `max-size: 10m`, `max-file: 3`).
- [ ] **Step 2:** README: что делает агент; локальный запуск (`make install`, `make check`); порядок первого запуска — `fetch` → `validate` → `holdout` → `report` → старт бота; деплой на VPS в Европе (установка Docker, `git clone`, `.env` заполняет пользователь сам, `chown 10001 data`, `docker compose up -d --build`); команды бота; где журнал и бэкапы; предупреждение «не финансовый совет, агент не торгует».
- [ ] **Step 3:** Проверка: `docker compose build` — успешно; `docker compose run --rm agent python -c "import invest_agent.bot"` — без ошибок.
- [ ] **Step 4:** Commit `chore: Docker и README`.

---

### Task 15: Бэктест на реальных данных и отчёт пользователю

Не код, а исследование; выполняет основной агент сессии, не субагент.

- [ ] **Step 1:** `python -m invest_agent.backtest fetch` — история скачана (размер БД и число пар — в отчёт).
- [ ] **Step 2:** `validate` для `TrendBreakout` и `MeanReversion` с параметрами по умолчанию; при необходимости — правки параметров только на train/validation. Каждая попытка — строкой в отчёте.
- [ ] **Step 3:** `holdout` — по одному разу для финальной версии каждой стратегии.
- [ ] **Step 4:** `report` → `data/backtest_report.md`; показать пользователю: прошли ли стратегии ворота, сделки, net после комиссий, max DD, сравнение с BTC, сколько версий проверено. Если ни одна не прошла — так и сказать; бота не запускать без решения пользователя.
- [ ] **Step 5:** Деплой (Task 14 README) — только после решения пользователя и после того, как он сам заполнит `.env` на сервере.
