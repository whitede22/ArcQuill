"""首次启动的欢迎页、升级时的更新说明、可重看的教程。

**触发**：主窗口启动时比两个值。`onboard_version` 小于 `CURRENT_ONBOARD_VERSION`
说明教程大改过，要弹；`last_seen_version` 比当前程序版本旧说明刚从旧版升上来，
第一屏换成更新说明。两条判据分开，任意一条成立就弹一次。

版本号递增（而不是 bool 的「看过没有」），所以教程以后大改，老用户也能看到新版。

**首装不显示更新说明**：判据是 `onboard_version == 0`。那意味着欢迎页本身都没
走过，这个人没有「上一次」，讲「这次改了什么」对他没有意义。

**样式**：不写任何字面颜色——颜色全部来自 `theme.palette()` 或复用 QSS 里已有的
objectName（`#primary` / `#bare` / `#muted` / `#sectionTitle`）。这一页自己的字体
规则（`#welcomeTitle` / `#welcomeSub` / `#featureTitle` / `#featureBody`）**住在
`theme.py` 的模板里**，与主界面的规则同源：早先它们是在这里内联拼出来的，
同一个语义就此有了两处定义。selfcheck 那条「不许在界面代码里出现字面颜色」的
扫描仍然盯着这里，另有一条盯着「这份文件里不许再出现内联的样式串」。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.changelog import Release
from ..version import APP_VERSION
from .theme import stylesheet
from .widgets import with_accent_bar

# 教程版本号。每次大改教程内容就 +1。文案措辞的小改不算，整页重写才算。
#
# v4：加了第三页「跟着做一遍」。前两页仍只是说明——用户看完还是不知道自己
# 该点哪里，而「查一个词」是这个产品唯一必须学会的动作。v3 及更早看过教程的
# 人也会再看到一次这一页，这是有意的。
CURRENT_ONBOARD_VERSION = 4

_WELCOME_TITLE = "欢迎来到 ArcQuill"
_WELCOME_SUBTITLE = "回车查词，顺手收藏。"
_MADE_BY = "made by wtd22"

# 欢迎页的功能点：(标题, 说明)。五条都是**已经能用**的功能。
#
# 第 5 条原先是个预告位，写着「学习统计排在后面」。复习模式上线后它先改成了
# 「复习模式正在路上」，等第四档评分与学习摘要都做完，这句也成了假话。现在
# 五条全部兑现，预告位取消——`CURRENT_ONBOARD_VERSION` 因此从 2 加到 3，
# 老用户下次启动会重新看到这一版。
#
# 文案长度是量过的：可排区 448px、一个全角宽 13.0px，预算 34 个全角宽，最长一句
# 32（功能点 2 与教程第 2 步，各 416px）。中文换行能从任意两字之间断开，
#「点「收 / 藏」进词组」这种断法比多占一行难看得多；`selfcheck` 里那条「每句说明
# 都排得下一行」的断言盯着这件事。
_FEATURES: list[tuple[str, str]] = [
    ("查词即收藏", "按回车就查，查到的自己进生词本，不用点保存"),
    (
        "搭配与例句",
        "be in charge of 和 take charge of 差在哪，看例句；"
        "顺眼的收进词组",
    ),
    ("七种导出格式", "纯文本、表格、Anki 卡片都在里面，换工具不丢词"),
    ("主题与背景", "六套主题挑一套顺眼的，或者铺自己的图"),
    ("复习与归档", "忘了、模糊、记住了，太简单就移出生词本"),
]

# 教程的四步，与欢迎页前四个功能点一一对应（预告位不进教程）。
# 标题不带「第一步」——序号由调用处的 `f"{i}　{head}"` 加。
_TUTORIAL: list[tuple[str, str]] = [
    (
        "查词与收藏",
        "按回车查词，查到的词进生词本，不用点保存。左栏词表，右栏详情。",
    ),
    (
        "搭配和例句",
        "查词时右栏给出固定搭配，每条配一句例句；点「收藏」收进词组收藏。",
    ),
    (
        "想带走就导出",
        "左上角「导出」有七种，比如 Markdown、PDF、表格和 Anki 卡片。",
    ),
    (
        "界面随你",
        "设置里挑一套主题，也可以铺自己的背景图，再调背景透出来多少。",
    ),
]

# 第三页：跟着做一遍。三行文字对应主窗口里真实发生的三件事，
# 关掉这一页之后主窗口会进入同样的三步，用户不必自己去找入口。
_PRACTICE_TITLE = "跟着做一遍"
_PRACTICE_SUBTITLE = "三个动作，一分钟。做完你就知道这个软件怎么用了。"
_PRACTICE_STEPS: list[tuple[str, str]] = [
    ("查一个词", "在上方输入框里打一个英文单词，按回车。查到的词会自动进生词本。"),
    ("看一眼详情", "右侧会给出释义、搭配和例句；点「朗读」可以听发音。"),
    ("评一次分", "点左栏「待复习」，按「忘了 / 模糊 / 记住了 / 太简单」评一次。"),
]


class OnboardingDialog(QDialog):
    """欢迎页 + 教程 + 跟着做一遍，三页用 `QStackedWidget` 切。

    `practice` 回答「用户点了跟着做没有」：关掉对话框之后，主窗口据此决定要不要
    进入三步引导（聚焦输入框、在状态栏一步步提示）。把「用户想不想被带着做」
    交给这里，主窗口就不必猜。
    """

    def __init__(
        self,
        theme_id: str,
        parent: QWidget | None = None,
        start_at_tutorial: bool = False,
        update_notes: tuple[Release, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_WELCOME_TITLE)
        self.setModal(True)
        self.setObjectName("onboarding")
        # 样式自持：不依赖 QApplication 的全局样式表，这样 gui_smoke 直接构造
        # MainWindow 时弹出来的欢迎页也能拿到正确配色。本页自己的字体规则在
        # `theme.py` 的模板里（`QDialog#onboarding` / `#welcomeTitle` / …）。
        self.setStyleSheet(stylesheet(theme_id))

        self.practice = False
        # 用户在更新页勾了「以后不再自动显示」。只有真显示了更新页才可能为
        # True；主窗口读它之前会先确认本次确实给了更新说明。
        self.skip_update_notes = False
        self._notes: tuple[Release, ...] = tuple(update_notes or ())

        self._stack = QStackedWidget()
        # 页码由这里统一算，不在各页的按钮里写死。加了更新页之后，原来写死的
        # 1 与 2 会整体错位一格，而且错得没有任何报错——点「先看看教程」翻到
        # 的可能是「跟着做一遍」。
        self._update_index = -1
        if self._notes:
            self._update_index = self._stack.addWidget(
                self._build_update(self._notes)
            )
        self._welcome_index = self._stack.addWidget(self._build_welcome())
        self._tutorial_index = self._stack.addWidget(self._build_tutorial())
        self._practice_index = self._stack.addWidget(self._build_practice())
        self._stack.setCurrentIndex(
            self._tutorial_index if start_at_tutorial else 0
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 30, 36, 22)
        layout.setSpacing(0)
        layout.addWidget(self._stack)

        self.setFixedWidth(520)

    # ---------- 更新页（只有升级时才存在） ----------

    def _build_update(self, notes: tuple[Release, ...]) -> QWidget:
        """升级后第一屏：这一版改了什么。

        与功能列表分开而不是叠在上面，是因为两者回答的问题不同——「新装的人
        该怎么用」与「老用户上次之后变了什么」。叠在一屏里，两类读者都得多扫
        一段与自己无关的话。
        """
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        title = QLabel(f"{notes[0].display_version()} 更新了什么")
        title.setObjectName("welcomeTitle")
        v.addWidget(title)

        sub = QLabel(self._update_subtitle(notes))
        sub.setObjectName("welcomeSub")
        sub.setWordWrap(True)
        v.addWidget(sub)
        v.addSpacing(18)

        v.addWidget(self._build_update_scroll(notes), 1)
        v.addSpacing(12)

        # 「不再自动显示」只关这一页。教程大改时欢迎页照旧弹，只是第一屏回到
        # 功能列表——用户不想看的是「每版都来一遍」的提示，不是教程本身。
        self.skip_note_box = QCheckBox("以后更新不再自动显示这一页")
        self.skip_note_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_note_box.setToolTip(
            "勾选后升级不再弹这一页。想回看时点窗口右下角的版本号"
        )
        v.addWidget(self.skip_note_box)
        v.addSpacing(12)

        v.addLayout(
            self._button_row(
                primary_text="开始使用",
                secondary_text="看看功能介绍",
                on_primary=self._finish_update,
                on_secondary=self._go_welcome,
            )
        )
        return page

    def _update_subtitle(self, notes: tuple[Release, ...]) -> str:
        if len(notes) == 1:
            return f"{notes[0].date}　这次改了这些。"
        oldest = notes[-1].display_version()
        return f"{oldest} 到 {notes[0].display_version()}，共 {len(notes)} 个版本。"

    def _build_update_scroll(self, notes: tuple[Release, ...]) -> QScrollArea:
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 8, 0)
        v.setSpacing(0)

        # 只有一个版本时标题已经写着版本号与日期，正文再顶一行就是重复。
        # 跨版本升级才需要每条前面标出属于哪一版。
        multi = len(notes) > 1
        for index, release in enumerate(notes):
            if index:
                v.addSpacing(18)
            if multi:
                head = QLabel(f"{release.display_version()}　{release.date}")
                head.setObjectName("changelogVersion")
                v.addWidget(head)
                v.addSpacing(7)
            for point in release.points:
                label = QLabel(point)
                label.setObjectName("changelogPoint")
                label.setWordWrap(True)
                v.addWidget(with_accent_bar(label))
                v.addSpacing(7)
        v.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 最多三个版本，内容高度不定。给它一个上限，超出的部分滚动，窗口本身
        # 不跟着长——否则跨版本升级时欢迎页会高过一块 1080p 屏幕。
        scroll.setMaximumHeight(330)
        return scroll

    def _finish_update(self) -> None:
        self.skip_update_notes = self.skip_note_box.isChecked()
        self.accept()

    # ---------- 页间导航 ----------

    def _go_welcome(self) -> None:
        self._stack.setCurrentIndex(self._welcome_index)

    def _go_tutorial(self) -> None:
        self._stack.setCurrentIndex(self._tutorial_index)

    def _go_practice(self) -> None:
        self._stack.setCurrentIndex(self._practice_index)

    # ---------- 欢迎页 ----------

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # 标题与版本号排一行。版本号是「这是什么软件」这句话的一部分，放在标题
        # 右下角会显得像脚注，贴在标题后面才读得连贯。
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.setSpacing(10)

        title = QLabel(_WELCOME_TITLE)
        title.setObjectName("welcomeTitle")
        head_row.addWidget(title)

        badge = QLabel(f"v{APP_VERSION}")
        badge.setObjectName("welcomeBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head_row.addWidget(badge)
        head_row.addStretch(1)
        v.addLayout(head_row)

        sub = QLabel(_WELCOME_SUBTITLE)
        sub.setObjectName("welcomeSub")
        v.addWidget(sub)
        v.addSpacing(14)

        # 一条通栏细线。标题区与要点区是两件事，靠间距分开需要 20px 以上才
        # 看得出层次，一条 1px 的线只用 12px 就够，还不用用户去数留白。
        rule = QLabel()
        rule.setObjectName("divider")
        rule.setFixedHeight(1)
        v.addWidget(rule)
        v.addSpacing(16)

        for head, body in _FEATURES:
            v.addWidget(self._feature_row(head, body))
            v.addSpacing(12)
        v.addStretch(1)

        v.addLayout(
            self._button_row(
                primary_text="开始使用",
                secondary_text="先看看教程",
                on_primary=self.accept,
                on_secondary=self._go_tutorial,
            )
        )
        v.addSpacing(14)

        made = QLabel(_MADE_BY)
        made.setObjectName("muted")
        made.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(made)

        return page

    # ---------- 教程页 ----------

    def _build_tutorial(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        title = QLabel("四步看懂 ArcQuill")
        title.setObjectName("welcomeTitle")
        v.addWidget(title)
        v.addSpacing(22)

        for i, (head, body) in enumerate(_TUTORIAL, start=1):
            head_label = QLabel(f"{i}　{head}")
            head_label.setObjectName("featureTitle")
            v.addWidget(head_label)
            v.addSpacing(4)
            body_label = QLabel(body)
            body_label.setObjectName("featureBody")
            body_label.setWordWrap(True)
            v.addWidget(body_label)
            v.addSpacing(16)
        v.addStretch(1)

        v.addLayout(
            self._button_row(
                primary_text="跟着做一遍",
                secondary_text="返回欢迎页",
                on_primary=self._go_practice,
                on_secondary=self._go_welcome,
            )
        )
        return page

    # ---------- 第三页：跟着做一遍 ----------

    def _build_practice(self) -> QWidget:
        """把「该点哪里」写成三个具体动作，并给一个直接开始的按钮。

        这一页存在的理由：前两页讲完功能，用户仍然停在「然后呢」。而「查一个词」
        是这个产品唯一必须学会的动作——不会它，其余六项功能全都用不上。
        """
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        title = QLabel(_PRACTICE_TITLE)
        title.setObjectName("welcomeTitle")
        v.addWidget(title)

        sub = QLabel(_PRACTICE_SUBTITLE)
        sub.setObjectName("welcomeSub")
        sub.setWordWrap(True)
        v.addWidget(sub)
        v.addSpacing(22)

        for i, (head, body) in enumerate(_PRACTICE_STEPS, start=1):
            head_label = QLabel(f"{i}　{head}")
            head_label.setObjectName("featureTitle")
            v.addWidget(head_label)
            v.addSpacing(4)
            body_label = QLabel(body)
            body_label.setObjectName("featureBody")
            body_label.setWordWrap(True)
            v.addWidget(body_label)
            v.addSpacing(16)
        v.addStretch(1)

        v.addLayout(
            self._button_row(
                primary_text="带我查第一个词",
                secondary_text="我自己看看",
                on_primary=self._start_practice,
                on_secondary=self.accept,
            )
        )
        return page

    def _start_practice(self) -> None:
        """记下「用户要跟着做」，然后关掉对话框。

        关掉之后由主窗口接管（聚焦输入框 + 状态栏一步步提示）。对话框不直接
        去操作主窗口的控件：那一层关系反过来会让这一页离不开主窗口，测试也就
        没法单独渲染它。
        """
        self.practice = True
        self.accept()

    # ---------- 共用部件 ----------

    def _feature_row(self, head: str, body: str) -> QWidget:
        row = QWidget()
        r = QVBoxLayout(row)
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(3)

        head_label = QLabel(head)
        head_label.setObjectName("featureTitle")
        r.addWidget(head_label)

        body_label = QLabel(body)
        body_label.setObjectName("featureBody")
        body_label.setWordWrap(True)
        r.addWidget(body_label)
        return row

    def _button_row(
        self, primary_text: str, secondary_text: str, on_primary, on_secondary
    ) -> QHBoxLayout:
        """一主一次两个按钮。**回调由调用方给**，不在内部判当前是第几页。

        早先这里靠 `currentIndex()` 决定次按钮做什么（首页去看教程、教程页回首页），
        加第三页之后那种写法就要再补一个分支，而分支的判据是「页面序号」——
        页面顺序一调，两个按钮的行为就跟着错位。回调写在各页自己的调用处，
        行为与页面一一对应，不会有第二种解释。
        """
        row = QHBoxLayout()
        row.setSpacing(12)

        primary = QPushButton(primary_text)
        primary.setObjectName("primary")
        primary.setCursor(Qt.CursorShape.PointingHandCursor)
        primary.setMinimumWidth(110)

        # 次按钮用默认描边样式，不是 `#bare`。无边框按钮在正文里是「就地动作」
        # （重查全部、格式说明），摆在对话框底栏会变成一行浮着的字，用户看不出
        # 它可以点；这一页要的是两个都能按下去的按钮。
        secondary = QPushButton(secondary_text)
        secondary.setCursor(Qt.CursorShape.PointingHandCursor)

        # 两个按钮等宽分掉整行，而不是左对齐堆一小撮。底栏是一条横线，
        # 右边留一大片空白会让这一页看起来没做完。
        row.addWidget(primary, 1)
        row.addWidget(secondary, 1)

        primary.clicked.connect(lambda _=False: on_primary())
        secondary.clicked.connect(lambda _=False: on_secondary())
        return row
