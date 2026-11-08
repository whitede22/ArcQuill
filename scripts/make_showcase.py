"""批量生成产品展示用的界面截图。

用法：`.venv/Scripts/python.exe scripts/make_showcase.py [输出目录]`

每张图在独立子进程里渲染：Qt 只允许一个 `QApplication`，而每个场景都以
`app.quit()` 收尾，同进程里连跑会把上一个窗口的状态带进下一张。
`QT_SCALE_FACTOR` 默认 2，1280×800 的窗口出 2560×1600 的图。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "docs" / "showcase"

# (文件名, 场景, 主题)。主题为空表示用配置里的默认主题。
SCENES: tuple[tuple[str, str, str], ...] = (
    ("01-lookup", "main", ""),
    ("02-phrases", "phrases", ""),
    ("03-review", "review", ""),
    ("04-export", "export", ""),
    ("05-import", "import", ""),
    ("06-settings", "settings", ""),
    ("07-refresh", "refresh", ""),
    ("08-background", "background", ""),
    ("09-onboarding", "onboarding", ""),
    ("10-fallback", "fallback", ""),
    ("11-night-lookup", "main", "night"),
    ("12-night-settings", "settings", "night"),
)


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    scale = os.environ.get("QT_SCALE_FACTOR", "2")

    failed: list[str] = []
    for name, kind, theme in SCENES:
        target = out_dir / f"{name}.png"
        env = dict(os.environ)
        env["QT_SCALE_FACTOR"] = scale
        env["ARCQUILL_SHOT_KIND"] = kind
        if theme:
            env["ARCQUILL_SHOT_THEME"] = theme
        else:
            env.pop("ARCQUILL_SHOT_THEME", None)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "screenshot.py"), str(target)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if proc.returncode != 0 or not target.exists():
            failed.append(f"{name}（退出码 {proc.returncode}）")
            print(proc.stdout.strip(), flush=True)
            print(proc.stderr.strip(), flush=True)
            continue
        print(f"{target.name}  {target.stat().st_size:,} 字节", flush=True)

    if failed:
        print("以下场景失败：" + "、".join(failed), flush=True)
        return 1
    print(f"共 {len(SCENES)} 张，输出目录：{out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
