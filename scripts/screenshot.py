"""生成界面截图（离屏渲染）。

用法：`.venv/Scripts/python.exe scripts/screenshot.py [输出路径]`

场景由 `ARCQUILL_SHOT_KIND` 选择，默认 `main`：

| kind | 画面 |
| --- | --- |
| `main` | 查词主页（含同义语、固定搭配、备注） |
| `phrases` | 词组收藏模式 |
| `incomplete` | 详情区顶部的缺项提示 |
| `review` | 复习卡片（已翻面） |
| `export` | 导出选项 |
| `import` | 导入前的核对框 |
| `refresh` | 批量重查的范围选择 |
| `settings` | 设置页 |
| `background` | 自定义背景图 |
| `fallback` | 接口失败后落到离线词库 |
| `onboarding` / `onboarding_tutorial` | 首次启动的欢迎页 / 教程页 |

配色由 `ARCQUILL_SHOT_THEME` 指定主题名（`ink` / `sage` / `clay` / `plum` /
`graphite` / `night`），不设时用配置里的主题。逐张批量出图见
`scripts/make_showcase.py`。
"""

from __future__ import annotations

import atexit
import gc
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SHOT_HOME = tempfile.mkdtemp(prefix="arcquill_shot_")
os.environ["ARCQUILL_HOME"] = _SHOT_HOME


def _clean_home() -> None:
    """退出时删掉本次截图用的临时数据目录。

    原先写的是 `atexit.register(shutil.rmtree, _SHOT_HOME, True)`，那个 `True`
    是 `ignore_errors`——**它把「删干净了」和「静默没删」变成了同一件事**。
    Qt 与 SQLite 的句柄未必在解释器退出前释放（目录里带 `arcquill.db-wal` /
    `-shm` 的残留就是这么来的），首次 `rmtree` 会被挡住，于是每跑一次截图
    就悄悄漏一个装着数据库的目录。本项目的铁律里有一条专门讲这个：曾经 6 处
    `mkdtemp` 不清理，累积到 3763 个目录 / 2.6 GB，把系统盘写满，症状是 Bash 报
    `ENOSPC`——**看起来像工具坏了**。

    所以这里重试 + `gc.collect()`，仍失败就**打印警告**，不装作已经删掉。
    （进程被外部强杀时 `atexit` 本就不会执行，那种情况由 `clean_temp.py`
    兜底——它已登记 `arcquill_shot_*` 前缀。）
    """
    for _ in range(3):
        gc.collect()
        try:
            shutil.rmtree(_SHOT_HOME)
            return
        except OSError:
            pass
    print(f"警告：截图临时目录未能清理：{_SHOT_HOME}", flush=True)


atexit.register(_clean_home)
# 不使用 offscreen 平台：该平台不加载系统字体，文字会渲染成方框。
# 改为在真实平台上创建「不显示但仍参与渲染」的窗口。

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.config import DB_PATH, DEFAULT_CONFIG, load_config  # noqa: E402
from app.core.db import connect, init_db  # noqa: E402
from app.core.dictionary import DictionaryService  # noqa: E402
from app.core.models import Entry, Phrase, Synonym  # noqa: E402
from app.core.repository import Repository  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.ui.main_window import MODE_PHRASES, MainWindow  # noqa: E402
from app.ui.settings import SettingsDialog  # noqa: E402
from app.ui.theme import stylesheet  # noqa: E402

# 截图窗口的逻辑尺寸。脚本自身不放大，分辨率由 `QT_SCALE_FACTOR` 决定：
# 设 2 时 `grab()` 取到的是 2 倍像素（2560×1600），文字按设备像素重新排版，
# 比事后拉伸图片清楚得多。
WINDOW_W, WINDOW_H = 1280, 800


def _sample_background(path: Path) -> None:
    """生成一张示例背景图（渐变 + 三团柔光）。

    刻意**自己画**而不往仓库里塞一张照片：照片有版权，而仓库里多一个二进制
    大文件还会让人误以为它是程序的一部分。这张图只用来演示「有一张图铺在界面
    后面」这件事，低饱和是有意的——背景图的作用是氛围，不是把正文压下去。
    """
    width, height = 1600, 1000
    image = QImage(width, height, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, width, height)
    gradient.setColorAt(0.0, QColor("#8FA3BF"))
    gradient.setColorAt(0.5, QColor("#C9D2DC"))
    gradient.setColorAt(1.0, QColor("#E7E2D8"))
    painter.fillRect(0, 0, width, height, QBrush(gradient))
    for cx, cy, radius, color in (
        (300, 220, 460, QColor(84, 116, 170, 110)),
        (1230, 800, 560, QColor(198, 156, 112, 95)),
        (1160, 170, 320, QColor(112, 168, 156, 85)),
    ):
        radial = QRadialGradient(cx, cy, radius)
        radial.setColorAt(0.0, color)
        radial.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        painter.fillRect(0, 0, width, height, QBrush(radial))
    painter.end()
    image.save(str(path))


def _as_child(widget: QWidget, window: QWidget) -> None:
    """把对话框摆成主窗口的**子控件**再截图。

    让它作为顶层窗口 `show()` 的话，进程会在**退出阶段**卡死：`grab()` 正常、
    图正常存盘、`app.quit()` 返回、`main()` 也返回并打印「已保存」，但进程
    就此不退。卡点在 Windows 的 `ExitProcess` → DLL 分离环节，连
    `os._exit(0)` 都救不了（它同样走 `ExitProcess`），`faulthandler` 也 dump
    不出栈（C 层持着 GIL）。现象极具迷惑性——日志已经说「存好了」，看起来跑完
    了，实际整条截图链就此停住（实测卡死 6 分钟以上，只能外部强杀）。

    原因是最顶上多了一个已显示的顶层窗口。`setParent(window, Widget)` 让它
    成为主窗口的子控件——照样排版与 `grab()`，但不创建第二个原生顶层窗口。
    `WA_DontShowOnScreen` + `show()` 仍是必需的，少了 `show()` 会 `grab()` 到
    空 pixmap。
    """
    widget.setParent(window, Qt.WindowType.Widget)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    _fit(widget)


def _fit(widget: QWidget) -> None:
    """把控件撑到内容需要的尺寸。

    顶层窗口 `show()` 时 Qt 会按 `sizeHint` 自己定尺寸；作为子控件摆出来不会，
    它会保持 `setParent` 那一刻的尺寸（默认小得多），于是底部的按钮被切掉一半。
    截图上看不出是尺寸问题，只像是「对话框就这么高」。
    """
    hint = widget.sizeHint().expandedTo(widget.minimumSizeHint())
    widget.resize(hint)


def _save_later(widget: QWidget, out: Path, delay: int = 200) -> None:
    """等布局稳定后截图并存盘，然后退出事件循环。"""

    def _grab() -> None:
        widget.grab().save(str(out))
        QApplication.instance().quit()

    QTimer.singleShot(delay, _grab)


def _row_of(window: MainWindow, prefix: str) -> int:
    for i in range(window.list.count()):
        if window.list.item(i).text().startswith(prefix):
            return i
    return 0


def _seed(service: DictionaryService, repo: Repository) -> None:
    """造一份像真实用过的词库。

    词的选取有实际用途，不是随手挑的：`ubiquitous` 是首页那一条（同义语三档
    差异说明、三条固定搭配、一条备注都齐），`garrulous` 只有英文释义与例句，
    用来展示缺项提示，`serendipity` 与 `eloquent` 的来源写成离线词库，重查
    对话框才有「需要更新」的条数可显示。
    """
    for word in ("ubiquitous", "ephemeral", "meticulous"):
        service.lookup(word)

    # 手工造的这两条也标成接口来源、提示词版本对齐当前版本：留在版本 0 会让
    # 重查对话框把它们算进「需要更新」，那条提示就成了假警报。
    from app.providers.prompts import PROMPT_VERSION

    # 带屈折归并的词条，用于展示「收录变体」的实际效果
    repo.upsert(
        Entry(
            lemma="run",
            phonetic="/rʌn/",
            pos="v.",
            definition_en="to move quickly on foot",
            definition_zh="跑；奔跑",
            example="She runs every morning before work.",
            example_zh="她每天上班前都去跑步。",
            source="openai-compat",
            prompt_version=PROMPT_VERSION,
            synonyms=[
                Synonym(word="sprint", gloss_zh="冲刺", diff_note="强调短距离全速"),
                Synonym(word="jog", gloss_zh="慢跑", diff_note="速度较慢，多为健身目的"),
                Synonym(word="dash", gloss_zh="猛冲", diff_note="强调突然且急促"),
            ],
        )
    )
    service.lookup("running")
    service.lookup("runs")

    # 给首页词条写一条备注，展示备注框与「重新查询不会覆盖」的语义
    shown = repo.get("ubiquitous")
    if shown is not None:
        shown.notes = "记忆法：ubi（哪里）+ quit（任何）→ 哪里都在"
        repo.upsert(shown)

    # 不完整词条：接口只给了英文释义与例句，中文释义/同义语/例句翻译都缺
    repo.upsert(
        Entry(
            lemma="garrulous",
            phonetic="/ˈɡærələs/",
            pos="adj.",
            definition_en="excessively talkative, especially about trivial matters",
            example="Our garrulous neighbour kept us on the doorstep for an hour.",
            source="openai-compat",
            prompt_version=PROMPT_VERSION,
        )
    )

    # 来源标成离线词库的两条：批量重查的对话框会把它们算进「需要更新」
    repo.upsert(
        Entry(
            lemma="serendipity",
            phonetic="/ˌserənˈdɪpəti/",
            pos="n.",
            definition_en="the luck of finding something good without looking for it",
            definition_zh="意外发现珍奇事物的运气",
            example="Finding that little bookshop was pure serendipity.",
            example_zh="发现那家小书店纯属意外之喜。",
            source="offline",
        )
    )
    repo.upsert(
        Entry(
            lemma="eloquent",
            phonetic="/ˈeləkwənt/",
            pos="adj.",
            definition_en="able to express ideas clearly and persuasively",
            definition_zh="雄辩的；有说服力的",
            example="Her silence was more eloquent than any speech.",
            example_zh="她的沉默比任何演说都更有说服力。",
            source="offline",
        )
    )

    for phrase, gloss, example, example_zh, source in (
        ("take off", "起飞；脱下；突然走红", "The plane took off twenty minutes late.",
         "飞机晚了二十分钟起飞。", "take"),
        ("look up", "查阅；抬头看", "Look the word up before you use it.",
         "用之前先查一下这个词。", "look"),
        ("in the long run", "从长远来看", "In the long run, reading pays off.",
         "从长远来看，读书是有回报的。", "run"),
        ("break down", "出故障；分解；（情绪）崩溃", "The car broke down on the motorway.",
         "车在高速上抛锚了。", "break"),
        ("catch up on", "补上；赶上", "I need a weekend to catch up on sleep.",
         "我需要一个周末补觉。", "catch"),
        ("make sense of", "理解；弄懂", "I could not make sense of the diagram.",
         "我看不懂那张图。", "sense"),
    ):
        repo.add_phrase(
            Phrase(
                phrase=phrase,
                gloss_zh=gloss,
                example=example,
                example_zh=example_zh,
                source_lemma=source,
            )
        )

    _relabel_as_api(repo)


def _relabel_as_api(repo: Repository) -> None:
    """把示例词条的来源改写成接口，让画面上的各处说法互相对得上。

    内容本身来自内置示例（截图不能联网），但右下角状态栏、详情区的来源标签、
    重查对话框里「需要更新」的条数都由同一份配置推出来。留着 `mock` 来源的话，
    画面会变成「已接入接口 · deepseek-chat」配「来源 内置示例」——两个元素各说
    各的，懂行的人一眼就能看出图是拼的。

    改完之后 `prompt_version` 也与当前版本对齐，重查对话框里只剩两条真正从
    离线词库来的词条需要更新，那正是接上接口后该做的事。
    """
    from app.providers.prompts import PROMPT_VERSION

    for entry in repo.list_entries():
        if (entry.source or "") != "mock":
            continue
        entry.source = "openai-compat"
        entry.prompt_version = PROMPT_VERSION
        repo.upsert(entry)


def _sanitize_settings_paths(dialog) -> None:
    """把设置页里的本机绝对路径换成示例路径。

    设置页有两处直接显示绝对路径。窗口背景的输入框指向本次渲染用的临时目录
    （`%TEMP%\\arcquill_shot_xxx\\bg.png`），「本地词库」那行的位置指向
    开发机上的仓库。截图要进文档，这两处等于把本机的目录结构一并公开。

    只改显示文字，不改取值：背景输入框在截图那一刻才换字，预览条已经按真实
    文件算好，因此图仍是真图。
    """
    from PySide6.QtWidgets import QLabel

    dialog.background.blockSignals(True)
    dialog.background.setText(r"C:\Users\you\Pictures\bg.png")
    # 光标留在末尾的话，输入框会横向滚到只露出路径的后半截，画面开头是个
    # `\Users`，看着像路径被吃掉了几个字符。
    dialog.background.setCursorPosition(0)
    dialog.background.blockSignals(False)

    # 下拉框停在占位项上时，画面会出现「背景已填、最近使用却写着没用过」的
    # 自相矛盾。选中那条记录，文案才与输入框对得上。
    if dialog.bg_recent.count() > 1:
        dialog.bg_recent.blockSignals(True)
        dialog.bg_recent.setCurrentIndex(1)
        dialog.bg_recent.blockSignals(False)

    for label in dialog.findChildren(QLabel):
        text = label.text()
        if not text.startswith("本地词库："):
            continue
        head = text.split("\n", 1)[0]
        label.setText(
            head + "\n位置：" + r"C:\Program Files\ArcQuill\assets\offline_dict.db"
        )


# ---------- 场景 ----------


def _scene_lookup(window: MainWindow, out: Path, kind: str) -> None:
    window.list.setCurrentRow(
        _row_of(window, "garrulous" if kind == "incomplete" else "ubiquitous")
    )
    _save_later(window, out, 180)


def _scene_phrases(window: MainWindow, out: Path) -> None:
    window._switch_mode(MODE_PHRASES)
    window.list.setCurrentRow(_row_of(window, "take off"))
    _save_later(window, out, 220)


def _scene_background(window: MainWindow, config: dict, wallpaper: Path, out: Path) -> None:
    # 不模糊的那张用来直观对比「模糊蒙版」这个开关到底做了什么
    config["background_image"] = str(wallpaper)
    config["background_blur"] = os.environ.get("ARCQUILL_SHOT_BLUR", "1") != "0"
    # 面板让位档位：soft / medium / bold，用于出三档对比图
    if os.environ.get("ARCQUILL_SHOT_VEIL"):
        config["background_veil"] = os.environ["ARCQUILL_SHOT_VEIL"]
    window._apply_background()
    # 选中一条词条再截图：留着空详情区的话，画面里看不出「半透明面板让背景
    # 透上来」这件事。
    window.list.setCurrentRow(_row_of(window, "ubiquitous"))
    _save_later(window, out, 260)


def _scene_fallback(window: MainWindow, config: dict, service: DictionaryService, out: Path) -> None:
    """兜底预览：真实走一遍「接口失败 → 离线词库 → 界面标注」。

    刻意不手工构造 Entry 直接渲染——那样截出来的是渲染函数的样子，不是用户
    会看到的样子（曾经正因为少了这段链路，界面上那句提示被列表重载覆盖掉而
    无人察觉）。
    """
    from app.config import KEY_PATH
    from app.providers.base import ProviderError
    from app.providers.fallback import FallbackProvider
    from app.providers.offline import OfflineDictProvider
    from app.security.keystore import save_api_key

    class _Down:
        name = "openai-compat"

        def lookup(self, word: str) -> Entry:
            raise ProviderError("请求超时（30 秒）。可能是网络不稳定，或接口响应过慢。")

    config["provider"] = "openai-compat"
    config["model"] = config.get("model") or DEFAULT_CONFIG["model"]
    # 让常驻状态与画面自洽：窗口构造时 config 还是 mock，右下角会停在「离线
    # 示例模式」，与画面里的兜底内容自相矛盾。写一个假密钥到临时数据目录
    # （本脚本用独立临时 HOME，不会碰到真实配置）。
    save_api_key(KEY_PATH, "sk-preview")
    window._refresh_mode_label()
    window.detail.set_networked(window._is_networked())
    service.set_provider(FallbackProvider(_Down(), OfflineDictProvider()))
    # 用 apple 而不是别的词：它的释义含真换行（源语料把换行存成字面量 `\n`，
    # 构建层解码后才是多行）。截图顺带就是「解码生效」的证据——若解码被改坏，
    # 这张图上会明晃晃地出现 `\n` 这坨字符。
    window._lookup_word("apple")
    _save_later(window, out, 1200)


def _scene_import(window: MainWindow, out: Path) -> None:
    """导入前的核对框。

    文案取自**真实实现**——先打桩 QMessageBox 把 `_confirm_import` 拦下来收走
    那段文本，再摆成对话框截图。手工另写一份文案的话，截出来的是「我以为的
    文案」，不是用户会看到的那一句，图就成了摆设。
    """
    import app.ui.main_window as mw
    from app.core.importer import parse_text

    src = Path(_SHOT_HOME) / "生词表.txt"
    src.write_text(
        "1. ubiquitous  /juːˈbɪkwɪtəs/  adj. 无处不在的\n"
        "2. meticulous  /məˈtɪkjələs/  adj. 一丝不苟的\n"
        "3. ephemeral   /ɪˈfemərəl/    adj. 短暂的\n"
        "perseverance, tenacity\n"
        "She went to the store to buy some milk\n",
        encoding="utf-8",
    )
    captured: list[str] = []
    real_box = mw.QMessageBox

    class _Box:
        StandardButton = real_box.StandardButton

        @staticmethod
        def question(*a, **_k):
            captured.append(str(a[2]))
            return real_box.StandardButton.No

        warning = staticmethod(lambda *a, **k: None)
        information = staticmethod(lambda *a, **k: None)

    mw.QMessageBox = _Box
    try:
        window._confirm_import(src, parse_text(src.read_text(encoding="utf-8")))
    finally:
        mw.QMessageBox = real_box

    box = real_box(
        real_box.Icon.Information,
        "导入生词表",
        captured[0] if captured else "(没有取到文案)",
        real_box.StandardButton.Yes | real_box.StandardButton.No,
        window,
    )
    # 程序没装 Qt 的中文翻译，标准按钮会显示 Yes / No（已记进待办）。截图里
    # 先写成中文，笔触与界面上其余部分一致。
    box.setButtonText(real_box.StandardButton.Yes, "开始收录")
    box.setButtonText(real_box.StandardButton.No, "取消")
    _as_child(box, window)
    QTimer.singleShot(
        200, lambda: (box.grab().save(str(out)), box.close(), QApplication.instance().quit())
    )


def _scene_onboarding(window: MainWindow, config: dict, out: Path, tutorial: bool) -> None:
    from app.ui.onboarding import OnboardingDialog

    dialog = OnboardingDialog(config.get("theme"), start_at_tutorial=tutorial)
    _as_child(dialog, window)
    _save_later(dialog, out, 220)


def _scene_settings(window: MainWindow, config: dict, wallpaper: Path, out: Path) -> None:
    # 设置页填一份能直接用的配置：空地址会当场报「请填写接口地址」，而截图
    # 要展示的是设置项本身，不是一次校验失败。模型名与地址互相匹配，避免
    # 「地址是 DeepSeek、模型是 gpt-4o-mini」这种自相矛盾的组合。
    dialog = SettingsDialog(
        {
            "provider": "openai-compat",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            # 这一屏顺带展示主题选择与「背景预览条」。带上背景图与一条最近
            # 使用记录，否则预览条只会显示「（没有背景图）」，「最近使用」
            # 也永远是空的那句占位。
            "theme": config.get("theme"),
            "background_image": str(wallpaper),
            "background_blur": True,
            "background_veil": "medium",
            "background_recent": [str(wallpaper)],
        },
        "sk-demo-key-not-a-real-secret",
        None,
        on_theme_preview=window._apply_theme,
    )
    _as_child(dialog, window)
    _sanitize_settings_paths(dialog)
    _save_later(dialog, out, 240)


def _scene_review(window: MainWindow, repo: Repository, config: dict, out: Path) -> None:
    from app.ui.review_dialog import ReviewDialog

    dialog = ReviewDialog(repo, config.get("theme"))
    # 翻到背面再摆出来：正面只有词与一句提示，展示不出「中文释义 + 例句 + 四档
    # 评分」。**先翻面再 show()**，尺寸在第一次显示时按完整内容算；摆出来之后再
    # 翻面的话，新长出来的那几行会挤在旧尺寸里，标题被压成一条黑边。
    dialog._reveal()
    _as_child(dialog, window)
    _save_later(dialog, out, 320)


def _scene_export(window: MainWindow, out: Path) -> None:
    from app.ui.export_dialog import ExportDialog

    # 带一个真实的筛选在生效，否则「跟随当前筛选」那条会置灰成
    # 「（当前没有筛选）」，画面上看着像坏掉的一项。
    window.filter.setText("e")
    total = len(window._current_entries(unlimited=True))
    dialog = ExportDialog(total, filtered=True)
    _as_child(dialog, window)
    _save_later(dialog, out, 220)


def _scene_refresh(window: MainWindow, out: Path) -> None:
    from app.ui.refresh_dialog import RefreshDialog

    rows = window._refresh_candidates()
    dialog = RefreshDialog(
        total=len(rows),
        outdated=sum(1 for row in rows if row[2]),
        scope=window._refresh_scope(),
    )
    _as_child(dialog, window)
    _save_later(dialog, out, 220)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parents[1] / "preview.png"
    )

    config = load_config()
    # 截的是「接上接口之后」的样子：状态栏、来源标签、重查条数都由这一组
    # 配置推出来，见 `_relabel_as_api`。密钥是假的，写进的是本脚本的临时
    # 数据目录，不会碰到真实配置。
    from app.config import KEY_PATH
    from app.security.keystore import save_api_key

    config["provider"] = "openai-compat"
    config["model"] = "deepseek-chat"
    save_api_key(KEY_PATH, "sk-demo-key-not-a-real-secret")
    # 主题换色：不设时沿用配置里的主题，不改变原有产物。
    if os.environ.get("ARCQUILL_SHOT_THEME"):
        config["theme"] = os.environ["ARCQUILL_SHOT_THEME"]
    # 首启的两个模态框（欢迎页、统计告知）在这里一律跳过：脚本自己会另外
    # 构造欢迎页，而让它们按启动流程弹出来的话，截图期间会有一个看不见的
    # 模态框压在事件循环里，靠抢在它前面截图来蒙混过去。
    from app.ui.onboarding import CURRENT_ONBOARD_VERSION

    config["onboard_version"] = CURRENT_ONBOARD_VERSION
    config["analytics_consent_decided"] = True

    # 演示用的壁纸。**只在这里定一次**：背景图场景与设置界面场景都要用它，
    # 各生成一次的话，两个场景里的图可能不是同一张，对比起来就没有意义。
    # 默认自绘（低饱和渐变，避免往仓库塞照片）；判读效果时用
    # `ARCQUILL_SHOT_WALLPAPER` 指向一张真实照片——自绘样张没有中间调与结构，
    # 任何不透明度都只呈现一层淡色偏，会把人误导成「透明没生效」。
    wallpaper = Path(_SHOT_HOME) / "bg.png"
    _real_wallpaper = os.environ.get("ARCQUILL_SHOT_WALLPAPER")
    if _real_wallpaper:
        wallpaper = Path(_real_wallpaper)
    else:
        _sample_background(wallpaper)

    conn = connect(DB_PATH)
    init_db(conn)
    repo = Repository(conn)
    service = DictionaryService(repo, MockProvider())
    _seed(service, repo)

    app = QApplication(sys.argv)
    app.setStyleSheet(stylesheet(config.get("theme")))
    window = MainWindow(repo, service, config)
    window.resize(WINDOW_W, WINDOW_H)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()

    kind = os.environ.get("ARCQUILL_SHOT_KIND", "main")
    if kind == "settings":
        _scene_settings(window, config, wallpaper, out)
    elif kind == "background":
        _scene_background(window, config, wallpaper, out)
    elif kind == "fallback":
        _scene_fallback(window, config, service, out)
    elif kind == "import":
        _scene_import(window, out)
    elif kind in ("onboarding", "onboarding_tutorial"):
        _scene_onboarding(window, config, out, kind == "onboarding_tutorial")
    elif kind == "phrases":
        _scene_phrases(window, out)
    elif kind == "review":
        _scene_review(window, repo, config, out)
    elif kind == "export":
        _scene_export(window, out)
    elif kind == "refresh":
        _scene_refresh(window, out)
    else:
        _scene_lookup(window, out, kind)

    app.exec()
    conn.close()
    print(f"已保存截图：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
