"""验证旧数据目录 → ~/.arcquill 的迁移。

用隔离的临时家目录跑，不碰真实数据。四个用例：

1. 正常库：迁移后新库可读，**旧目录保留**，并留下 `migrated_from.txt`
2. 损坏的库（没有 entries 表）：校验不通过，旧目录保留，新目录不留残骸
3. 只有配置没有库：也能迁，且不因为「没有 entries 表」误判为失败
4. 页级损坏（entries 表在、内容半截）：校验不通过，旧目录保留

第 1 例的判据在 2026-11 改过一次：此前要求「旧目录被删」，现在要求「旧目录
还在」。删除是不可逆的，而它换来的只是少一个目录；判据一旦误判，用户两头空。
第 4 例是为这次改动补的——只查「有没有 entries 表」的旧判据会放它过去。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def make_db(path: Path, *, broken: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if not broken:
        conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, lemma TEXT)")
        conn.execute("INSERT INTO entries (lemma) VALUES ('ubiquitous')")
        conn.commit()
    else:
        conn.execute("CREATE TABLE something_else (x INTEGER)")
        conn.commit()
    conn.close()


def make_truncated_db(path: Path) -> None:
    """造一个「表还在、页已坏」的库：写满几页再砍掉后一半。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, lemma TEXT)")
    conn.executemany(
        "INSERT INTO entries (lemma) VALUES (?)",
        [(f"word{index:04d}",) for index in range(800)],
    )
    conn.commit()
    conn.close()
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        handle.truncate(int(size * 0.55))


def run_case(
    label: str,
    build,
    *,
    expect_migrated: bool,
    expect_db: bool = True,
) -> bool:
    home = Path(tempfile.mkdtemp(prefix="arcquill_mig_"))
    try:
        build(home)
        env = dict(os.environ)
        for key in ("ARCQUILL_HOME", "QUILL_HOME", "LEXICON_HOME"):
            env.pop(key, None)
        env["USERPROFILE"] = str(home)
        env["HOME"] = str(home)
        code = (
            "from app import config\n"
            "print('dir', config.APP_DIR)\n"
            "print('migrated', config.migrate_legacy_home())\n"
            "print('db', config.DB_PATH.exists())\n"
            "print('old', (config.APP_DIR.parent / '.quill').exists())\n"
            "print('mark', config.MIGRATION_MARK.exists())\n"
        )
        out = subprocess.run(
            [PY, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
        )
        lines = dict(
            line.split(" ", 1) for line in out.stdout.strip().splitlines() if " " in line
        )
        migrated = lines.get("migrated") == "True"
        old_left = lines.get("old") == "True"
        mark = lines.get("mark") == "True"
        ok = migrated == expect_migrated and (lines.get("db") == "True") == expect_db
        # 旧目录**永远**保留：它是回退的唯一凭据，删了就没有第二次机会。
        ok = ok and old_left
        # 只有迁移真正成功过才该有留痕：失败时写一行「从哪搬」只会误导。
        ok = ok and mark == expect_migrated
        print(
            f"{'OK ' if ok else 'BAD'} {label}: migrated={migrated} "
            f"db={lines.get('db')} 旧目录还在={old_left} 留痕={mark}"
        )
        if not ok:
            print("   stderr:", out.stderr.strip()[-300:])
        return ok
    finally:
        shutil.rmtree(home, ignore_errors=True)


def case_normal(home: Path) -> None:
    make_db(home / ".quill" / "quill.db")


def case_broken(home: Path) -> None:
    make_db(home / ".quill" / "quill.db", broken=True)


def case_config_only(home: Path) -> None:
    (home / ".quill").mkdir(parents=True)
    (home / ".quill" / "config.json").write_text('{"theme": "night"}', encoding="utf-8")


def case_truncated(home: Path) -> None:
    make_truncated_db(home / ".quill" / "quill.db")


def main() -> int:
    results = [
        run_case("正常库迁走，旧目录保留", case_normal, expect_migrated=True),
        run_case("库坏了不迁移、不删旧目录", case_broken, expect_migrated=False, expect_db=False),
        run_case("只有配置也能迁", case_config_only, expect_migrated=True, expect_db=False),
        run_case(
            "页级损坏同样拦下", case_truncated, expect_migrated=False, expect_db=False
        ),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
