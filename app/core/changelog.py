"""发布以来每个版本的几句话说明。

## 这东西为什么和 CHANGELOG.md 并存

`CHANGELOG.md` 是给维护者和想深挖的人看的：一处改动的来龙去脉、为什么不那么做、
放弃了哪条路。它适合读，不适合在欢迎页上给人扫一眼。

这里只留用户**第二天会注意到**的差别：「太简单」之后词去哪了、导出多了哪一档。
一条一句话，一个版本一到五条。不写内部名词，不写「重构」「优化」这类看完仍然
不知道发生了什么的话。

## 维护规则

`RELEASES` 按版本倒序手写，**第一条必须等于 `APP_VERSION`**。selfcheck 有一条
断言盯着这件事：发版时改了版本号却忘了写说明，那条会红。这比「记得写」可靠。

文案长度是量过的。欢迎页可排区约 448px、一个全角宽 13px，条目在对话框里
换行显示（`wordWrap`），所以不受「必须排成一行」的约束，但一条超过两行就
不再是「几句话」了。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..version import APP_VERSION
from .update import is_newer


@dataclass(frozen=True)
class Release:
    """一个版本的三五句话。`date` 只用于展示，不参与任何比较。"""

    version: str
    date: str
    points: tuple[str, ...]

    def display_version(self) -> str:
        """带 v 前缀，与 Release 页面一致。"""
        return f"v{self.version.lstrip('vV')}"


# 倒序。新增版本时把新的一条插到最前面，同时把 `APP_VERSION` 改掉。
RELEASES: tuple[Release, ...] = (
    Release(
        version="1.8.0",
        date="2026-11-15",
        points=(
            "朗读修好了。上一版点喇叭一个音都没有，这一版真的会念出来。",
            "版本号从左下角挪到了顶栏字标旁边，点一下还是看更新日志。",
            "左栏顶上多了三个分页：生词本、复习、统计。复习不再是弹窗，切过去就开始。",
            "统计页有四个数字、近七天新增的柱形图，还有使用报告的入口。",
        ),
    ),
    Release(
        version="1.7.0",
        date="2026-11-11",
        points=(
            "更新后第一次打开，会先告诉你这一版改了什么。",
            "以后想回看，点窗口右下角的版本号。",
            "不想每次都被提醒，可以在那一页勾选不再显示。",
        ),
    ),
    Release(
        version="1.6.0",
        date="2026-11-11",
        points=(
            "复习时点「太简单」，这个词会离开生词本，不再出现在待复习里。",
            "移走的词没有删，状态筛选里能翻到，也能放回去。",
            "导出可以按添加日期取范围，也可以只取上次导出之后新加的。",
            "修好了复习结束后统计不记、角标不刷新、引导走不完。",
        ),
    ),
    Release(
        version="1.5.0",
        date="2026-11-11",
        points=(
            "查词时可以贴一段英文，释义按这段话给。",
            "音标旁边多了「朗读」，用系统语音念，不用装东西。",
            "左栏能按学习状态和词性筛选，列表也不再停在一千条。",
            "导出挪到后台，几千条也不会卡住窗口。",
            "启动时静默查一次新版本，有的话在状态栏提示。",
        ),
    ),
    Release(
        version="1.4.1",
        date="2026-11-08",
        points=(
            "密钥填错时不再假装查到了，直接报错并指到设置页。",
            "查完词，窗口下沿会冒一条「已保存」，两秒多自己消失。",
            "写库失败时这次编辑不再丢，留在原处等下次触发。",
            "日志写到本机，只记事件不记词条内容。",
        ),
    ),
    Release(
        version="1.4.0",
        date="2026-11-06",
        points=(
            "程序改名为 ArcQuill，数据目录迁到 ~/.arcquill，旧的会自动搬。",
            "图标和窗口左上角的字标换成新的。",
            "以前导出的文件仍然认得。",
        ),
    ),
    Release(
        version="1.3.5",
        date="2026-10-29",
        points=(
            "配了背景图之后，正文能读清了。",
            "托盘菜单在深色系统下不再是黑字。",
        ),
    ),
    Release(
        version="1.3.4",
        date="2026-10-29",
        points=("设置页里提交报告那一块换成强调色，提交前要留一个称呼。",),
    ),
    Release(
        version="1.3.3",
        date="2026-10-29",
        points=(
            "设置页改成左右两栏，高度矮了一半。",
            "提交报告单独成块，看得出它是唯一把数据送到别处的动作。",
        ),
    ),
    Release(
        version="1.3.2",
        date="2026-10-29",
        points=("报告可以直接提交，不用自己去 GitHub 建 Issue。",),
    ),
    Release(
        version="1.3.1",
        date="2026-10-29",
        points=(
            "拆分过长的函数，最长的一个从 228 行降到 21 行。",
            "依赖版本锁定，加了持续集成。",
        ),
    ),
    Release(
        version="1.3.0",
        date="2026-10-28",
        points=(
            "本地使用统计，默认关闭，收什么不收什么逐条列清。",
            "设置里能从备份恢复词库。",
            "复习多了第四档，一轮结束后给一段摘要。",
            "后台任务不再共用数据库连接。",
        ),
    ),
    Release(
        version="1.2.0",
        date="2026-10-27",
        points=(
            "离线词库换成全量档，词条从四万九千涨到七十六万。",
            "窗口收进托盘后按 Alt+Q 唤出，输入框预填剪贴板里的词。",
        ),
    ),
    Release(
        version="1.1.1",
        date="2026-10-27",
        points=("导出多了「单词表」，两列，换设备时整份导回。",),
    ),
    Release(
        version="1.1.0",
        date="2026-10-27",
        points=(
            "词组可以直接查，take off 这种。",
            "导入时能认出两到四个词组成的词组。",
        ),
    ),
    Release(
        version="1.0.0",
        date="2026-10-25",
        points=(
            "「重查全部」按当前接口批量刷新生词本。",
            "修好了导入完成后进度条关不掉。",
        ),
    ),
)

# 升级后最多在欢迎页摊开几个版本的说明。跨了好几个版本的用户，多给几屏就是
# 在欢迎页上做阅读任务；剩下的让他自己去更新日志里翻。
WELCOME_MAX_RELEASES = 3


def latest() -> Release:
    """当前版本的说明。`RELEASES` 保证非空且倒序（selfcheck 盯着）。"""
    return RELEASES[0]


def releases_since(version: str) -> tuple[Release, ...]:
    """比 `version` 新的那些版本，新到旧。

    `version` 为空串表示「没有锚点」：此时只给最新一个，而不是把全部历史
    摊开——老用户第一次带上这个字段升级时走的就是这条路。
    """
    if not (version or "").strip():
        return (latest(),)
    return tuple(item for item in RELEASES if is_newer(item.version, version))


def welcome_releases(last_seen: str) -> tuple[Release, ...]:
    """欢迎页上要摊开的说明，最多 `WELCOME_MAX_RELEASES` 条。"""
    return releases_since(last_seen)[:WELCOME_MAX_RELEASES]


def audit(releases: tuple[Release, ...] | None = None) -> str:
    """检查这组说明的形态，返回第一处问题的描述；全部合格返回空串。

    存在的理由：发版时改了 `APP_VERSION` 却忘了在这里加一条，程序不会报错，
    用户看到的是欢迎页第一屏写着上一个版本号。这类错只靠「记得写」防不住。

    `releases` 默认取当前的 `RELEASES`，而**不写成默认参数**——默认参数在函数
    定义时求值一次，之后替换模块级元组就影响不到它，自检里那条「改坏了要能
    发现」的自证会因此恒真。
    """
    items = RELEASES if releases is None else tuple(releases)
    if not items:
        return "没有任何版本说明"
    if items[0].version != APP_VERSION:
        return f"首条是 {items[0].version}，版本常量是 {APP_VERSION}"
    for index, item in enumerate(items):
        if not item.points:
            return f"{item.version} 没有一条说明"
        if any(not point.strip() for point in item.points):
            return f"{item.version} 里有空条目"
        if index and not is_newer(items[index - 1].version, item.version):
            return f"{item.version} 没有严格早于 {items[index - 1].version}"
    return ""
