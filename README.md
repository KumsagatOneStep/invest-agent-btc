# invest-agent

Бумажный агент-аналитик по криптовалютам. Дважды в день (окна 08:00 и 20:00
по UTC+5) он смотрит закрытые свечи Binance по парам к USDT, ищет сигналы на
**покупку на споте** по одобренным стратегиям, проверяет их фильтром Claude и
присылает в Telegram: вход, стоп, цель, размер позиции. Параллельно ведёт
бумажный портфель (стартовый счёт 200 000 ₸ в USDT), журнал сделок и недельную
статистику (воскресенье 20:00).

> **Не финансовый совет. Агент не торгует:** он не отправляет ордера на биржу,
> ключей Binance в нём нет. Все сделки — виртуальные (бумажные). Любые решения
> о реальных деньгах вы принимаете сами и на свой риск; прошлые результаты не
> гарантируют будущих.

## Локальный запуск

Нужен Python 3.12.

```bash
make install   # venv в .venv + зависимости
make check     # ruff + pytest
cp .env.example .env   # и заполните значения (см. ниже)
```

Секреты задаёт только пользователь, в git `.env` не попадает:

| Переменная | Что это |
|---|---|
| `TELEGRAM_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | ваш chat id; бот отвечает и пишет только в этот чат |
| `ANTHROPIC_API_KEY` | ключ Claude API |

Необязательные (значения по умолчанию в `.env.example`, закомментированы):
`TZ_OFFSET_HOURS`, `WINDOWS`, `START_CAPITAL_KZT`, `CLAUDE_MONTHLY_BUDGET_USD`
(лимит Claude в месяц, по умолчанию $10), `SERVER_COST_USD_MONTH`,
`CHEAP_MODEL`, `STRONG_MODEL`, `DB_PATH`, `HEALTHCHECK_URL`.

## Первый запуск: от истории к боту

Локально (из `.venv`) команды бэктеста работают без `.env`: токены им не нужны.
Через `docker compose` файл `.env` должен существовать (его требует `env_file`),
поэтому на VPS создайте его до любой команды compose. Все файлы пишутся в `data/`.

1. **Загрузка истории** (долго, ходит в публичный API Binance, ключи не нужны):
   `python -m invest_agent.backtest fetch`
   Создаёт `data/journal.sqlite` (свечи) и `data/symbol_rules.json`.
2. **Валидация** на обучающем и проверочном периодах, для каждой стратегии
   (`trend`, `meanrev`), параметры можно менять через `--param k=v`:
   `python -m invest_agent.backtest validate --strategy trend`
   Пишет `data/validation_log.json`. Последний период (holdout) здесь не
   читается.
3. **Holdout** — финальная проверка на данных, которых стратегия не видела.
   Одна версия стратегии запускается на нём **только один раз**, повтор
   запрещён:
   `python -m invest_agent.backtest holdout --strategy trend`
   Если ворота отбора пройдены, стратегия попадает в
   `data/approved_strategies.json`; без этого файла бот сигналов не даёт.
   Если код стратегии изменился после holdout, её версия не совпадёт и она
   будет пропущена.
4. **Отчёт**: `python -m invest_agent.backtest report` -> `data/backtest_report.md`
   (учтите смещение выжившего: список монет взят по сегодняшнему exchangeInfo).
5. **Старт бота**: `python -m invest_agent.bot` (локально) или `docker compose up -d`
   (см. ниже).

Локально команды бэктеста запускаются как `.venv/bin/python -m invest_agent.backtest ...`.

## Деплой на VPS в Европе

Нужен небольшой VPS в Европе (1-2 vCPU, 2 ГБ RAM, около 4-5 EUR в месяц):
Binance блокирует IP из США. Отдельный от других ботов.

```bash
# 1. Docker (Debian/Ubuntu)
curl -fsSL https://get.docker.com | sh

# 2. Код
git clone <адрес-репозитория> invest-agent && cd invest-agent

# 3. Секреты: создайте и заполните .env сами (не коммитьте его)
cp .env.example .env && nano .env

# 4. Каталог данных принадлежит пользователю контейнера (uid 10001)
mkdir -p data && sudo chown 10001 data

# 5. Сборка и запуск
docker compose up -d --build
docker compose logs -f agent
```

Историю и бэктест в контейнере выполняйте так (том `./data` общий с ботом):

```bash
docker compose run --rm agent python -m invest_agent.backtest fetch
docker compose run --rm agent python -m invest_agent.backtest validate --strategy trend
docker compose run --rm agent python -m invest_agent.backtest holdout --strategy trend
docker compose run --rm agent python -m invest_agent.backtest report
docker compose up -d --build   # затем запуск/перезапуск бота
```

Либо выполните шаги 1-4 локально и скопируйте каталог `data/` на сервер
(`rsync -a data/ vps:invest-agent/data/`, затем на сервере `sudo chown -R 10001 data`,
иначе контейнер не сможет писать в журнал).
`restart: unless-stopped` поднимает бота после перезагрузки сервера; логи
ротируются (10 МБ x 3 файла). Обновление: `git pull && docker compose up -d --build`.

## Команды бота

| Команда | Что делает |
|---|---|
| `/status` | текущее состояние портфеля и открытые позиции |
| `/stats` | статистика по сделкам и критерии перехода |
| `/pause` | приостановить выдачу сигналов |
| `/resume` | возобновить; единственный способ снять остановку HALTED (просадка -15%), начинается новый бумажный этап |
| `/budget` | расход на Claude API за месяц против лимита |
| `/coins` | текущий список монет (вселенная) |

Риск-правила: риск 1% счёта на сделку (0,5% в состоянии REDUCED после просадки
-10%), позиция не больше 25% счёта, не больше 4 открытых позиций.

## Журнал и бэкапы

- Журнал сделок, портфели и свечи: SQLite `data/journal.sqlite` (путь задаётся
  `DB_PATH`), на хосте это `./data/journal.sqlite`.
- Ежедневные бэкапы: `data/backups/journal-YYYYMMDD.sqlite`, хранятся 14 последних.
  Восстановление: остановите бота и положите нужный файл на место
  `data/journal.sqlite`.
- Логи бота: `docker compose logs agent`.
- Каталог `data/` не в git; копируйте его на другую машину, чтобы не потерять
  историю.
