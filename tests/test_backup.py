import sqlite3
from datetime import date, timedelta

from invest_agent.backup import backup


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()


def test_backup_copies_data(tmp_path):
    db = tmp_path / "j.sqlite"
    make_db(db)
    out = backup(db, tmp_path / "b", today=date(2026, 10, 20))
    assert out.name == "journal-20261020.sqlite"
    assert sqlite3.connect(out).execute("SELECT x FROM t").fetchone() == (42,)


def test_backup_keeps_last_14(tmp_path):
    db = tmp_path / "j.sqlite"
    make_db(db)
    d = tmp_path / "b"
    start = date(2026, 10, 1)
    for i in range(20):
        backup(db, d, today=start + timedelta(days=i))
    names = sorted(p.name for p in d.iterdir())
    assert len(names) == 14
    assert names[0] == "journal-20261007.sqlite"
    assert names[-1] == "journal-20261020.sqlite"
