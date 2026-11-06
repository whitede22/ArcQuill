"""验证 ~/.quill → ~/.arcquill 的迁移：成功路径与「校验不过就不删」两种。

用隔离的临时家目录跑，不碰真实数据。三个用例：

1. 正常库：迁移后新库可读、旧目录被删
2. 损坏的库：校验不通过，旧目录保留，新目录不留残骸（用户数据不能因为一次
   复制失败就没了，也不能让下次启动在坏库上直接崩）
3. 只有配置没有库：也能迁，且不因为「没有 entries 表」误判为失败
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


def run_case(label: str, build, *, expect_migrated: bool, expect_db: bool = True) -> bool:
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
        )
        out = subprocess.run(
            [PY, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
        )
        lines = dict(
            line.split(" ", 1) for line in out.stdout.strip().splitlines() if " " in line
        )
        migrated = lines.get("migrated") == "True"
        old_left = lines.get("old") == "True"
        ok = migrated == expect_migrated and (lines.get("db") == "True") == expect_db
        ok = ok and old_left is (not expect_migrated)
        print(
            f"{'OK ' if ok else 'BAD'} {label}: migrated={migrated} "
            f"db={lines.get('db')} 旧目录还在={old_left}"
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


def main() -> int:
    results = [
        run_case("正常库迁走并删旧目录", case_normal, expect_migrated=True),
        run_case("库坏了不删旧目录", case_broken, expect_migrated=False, expect_db=False),
        run_case("只有配置也能迁", case_config_only, expect_migrated=True, expect_db=False),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
