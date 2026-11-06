"""删除系统临时目录里 ArcQuill 测试留下的临时目录。

分**小批次**删除并逐批打印结果：本环境对批量删除有保护，一次性删几百个目录
会被拦下（SIGTERM）。批大小可通过命令行参数调整。

用法：
    python scripts/clean_temp.py          # 每批 8 个
    python scripts/clean_temp.py 20       # 每批 20 个
    python scripts/clean_temp.py --dry    # 只统计不删
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
import tempfile

PREFIXES = (
    "arcquill_check_*",
    "arcquill_legacy_*",
    "arcquill_oldfts_*",
    "arcquill_like_*",
    "arcquill_cfg_*",
    "arcquill_fb_*",
    "arcquill_v1_*",
    "arcquill_userdict_*",
    "arcquill_ablate_*",
    "arcquill_probe_*",
    "arcquill_lock_*",
    "arcquill_gui_*",
    "arcquill_shot_*",
    "arcquill_export_*",
    # 下面三个是**事后补登记**的：`selfcheck` 的 7b（标签）/ 7e（撤销）/ 7f（词组）
    # 各自建了一个临时库，而这三行当时都没写。全套检查跑了几十轮，没有任何一条
    # 断言提到它们——`_selftest_leftover_prefixes` 那条自证只验「表确实取自本文件」，
    # 验不出「本文件自己漏了前缀」。**又被「判据恰好漏掉被漏掉的那一类」咬了一次。**
    "arcquill_tags_*",
    "arcquill_undo_*",
    "arcquill_phrase_*",
    # `run.py --selfcheck` 的临时数据目录。它现在有 `atexit` 登记清理，可被强杀
    # （或打包产物在退出阶段被结束）时那一步不会执行——这里是兜底。
    #
    # 这个前缀**曾经两张表都没有**：`run.py` 建了它却从不删，而 `selfcheck` 与
    # 本脚本各自的表里都没登记它。结果是 10 月 11 日留下的一个目录在系统临时目录里
    # 躺了一整天、跑了几十轮检查都没有任何一条断言提到它——**「判据覆盖的目标
    # 恰好是被漏掉的那一类」**。所以前缀表只留本文件这一份定义。
    "arcquill_selfcheck_*",
    # `scripts/check_migration.py` 的隔离家目录。脚本自己会在 finally 里删，
    # 这里登记是为了那次 `rmtree` 被强杀时还有一道兜底。
    "arcquill_mig_*",
    # 改名（Quill → ArcQuill）之前那批前缀。程序不再生成它们，但历史残留还要清：
    # 少这些行不会让检查变红，只会在系统临时目录里留下几个旧名字的文件夹，而这
    # 正是用户会在「为什么还有 quill」里问起的东西。
    "quill_check_*",
    "quill_legacy_*",
    "quill_oldfts_*",
    "quill_like_*",
    "quill_cfg_*",
    "quill_fb_*",
    "quill_v1_*",
    "quill_userdict_*",
    "quill_ablate_*",
    "quill_probe_*",
    "quill_lock_*",
    "quill_gui_*",
    "quill_shot_*",
    "quill_export_*",
    "quill_tags_*",
    "quill_undo_*",
    "quill_phrase_*",
    "quill_selfcheck_*",
)
BATCH = 8


def _targets() -> list[str]:
    os.chdir(tempfile.gettempdir())
    found: list[str] = []
    for pat in PREFIXES:
        found.extend(glob.glob(pat))
    return sorted(found)


def main(argv: list[str]) -> int:
    batch = BATCH
    dry = False
    for arg in argv:
        if arg == "--dry":
            dry = True
        elif arg.isdigit():
            batch = int(arg)

    targets = _targets()
    print(f"临时目录: {tempfile.gettempdir()}")
    print(f"匹配到 {len(targets)} 个 arcquill_* 目录（每批删 {batch} 个）")
    if dry:
        return 0

    removed = 0
    failed: list[tuple[str, str]] = []
    for i, d in enumerate(targets, start=1):
        try:
            shutil.rmtree(d)
            removed += 1
        except OSError as exc:
            failed.append((d, str(exc)))
        if i % batch == 0:
            print(f"  已处理 {i}/{len(targets)}（删除成功 {removed}）", flush=True)

    print(f"完成：删除 {removed} 个，失败 {len(failed)} 个")
    for name, why in failed[:10]:
        print(f"  {name} — {why}")
    print(f"剩余 arcquill_* 目录：{len(_targets())} 个")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
