import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path


def backup(
    db_path: Path | str,
    backups_dir: Path = Path("data/backups"),
    keep: int = 14,
    today: date | None = None,
) -> Path:
    """Копия БД через sqlite3 backup API в journal-YYYYMMDD.sqlite; старые сверх `keep` удаляются."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    day = today or datetime.now(UTC).date()
    dest = backups_dir / f"journal-{day:%Y%m%d}.sqlite"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(backups_dir.glob("journal-*.sqlite"))[:-keep]:
        old.unlink()
    return dest
