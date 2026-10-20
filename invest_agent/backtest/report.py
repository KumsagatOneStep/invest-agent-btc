import json
from pathlib import Path

SURVIVORSHIP = (
    "Оговорка о выживших: вселенная монет строится по сегодняшнему exchangeInfo, поэтому "
    "монеты, исчезнувшие с биржи за период, в бэктест не попали. Результаты оптимистичнее реальных."
)


def load_validation(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text()) if path.exists() else {}


def save_validation(path: Path, version: str, entry: dict) -> None:
    data = load_validation(path)
    data[version] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _row(label: str, s: dict) -> str:
    return (
        f"| {label} | {int(s['trades'])} | {s['win_pct']:.0f}% | {s['net_pnl']:.2f} | "
        f"{100 * s['max_dd']:.1f}% | {s['btc_hold_pct']:+.1f}% |"
    )


def build_report(validation: dict[str, dict], holdout: dict[str, dict]) -> str:
    lines = [
        "# Отчёт бэктеста",
        "",
        SURVIVORSHIP,
        "",
        f"Версий стратегий проверено на holdout: {len(holdout)}, "
        + f"из них прошли ворота: {sum(1 for h in holdout.values() if h['passed'])}.",
        "",
        "Суммы в USDT после комиссий 0,1% и проскальзывания 0,05% на сторону; "
        + "BTC buy&hold — за тот же период.",
    ]
    versions = list(dict.fromkeys([*validation, *holdout]))
    for v in versions:
        val = validation.get(v, {})
        hold = holdout.get(v)
        name = val.get("name") or (hold or {}).get("summary", {}).get("name", "?")
        params = val.get("params") or (hold or {}).get("summary", {}).get("params", {})
        lines += ["", f"## {name} `{v[:10]}`", "", f"Параметры: {params}", ""]
        lines += [
            "| Период | Сделок | Прибыльных | Net | Max DD | BTC buy&hold |",
            "|---|---|---|---|---|---|",
        ]
        for label, key in (("train", "train"), ("validation", "validation")):
            if key in val:
                lines.append(_row(label, val[key]))
        if hold:
            lines.append(_row("holdout", hold["summary"]))
            verdict = "прошла" if hold["passed"] else "не прошла"
            lines += ["", f"Ворота отбора: {verdict}."]
            lines += [f"- {r}" for r in hold["reasons"]]
        else:
            lines += ["", "На holdout не запускалась."]
    return "\n".join(lines) + "\n"
