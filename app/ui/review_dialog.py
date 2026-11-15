"""复习会话对话框：正面看词、翻面看义、三档评分推进 SM-2。

一次会话把**到期的词**排好队（`repo.due_entries`），逐条「正面看词 → 翻面
看义 → 评分」，评分即落库（`repo.apply_review`）。一轮过完当前到期的全部
词，不递归加回——被评「忘了」的词会回到今天，下一轮复习自然再见。

**第四颗按钮不是评分**：「太简单」表示这个词不用再练，走 `repo.archive`
把它移出生词本与复习队列，数据保留（「已归档」里能翻出来）。早先它是第四档
评分，只把间隔拉长，熟词照样每轮占一个位置——队列只增不减，复习时间全花在
已经会的词上。

**样式**：与欢迎页同一条纪律——不写任何字面颜色，颜色全部来自
`theme.palette()`，结构样式（按钮 / 输入框 / 卡片底）复用 QSS 里已有的
objectName（`#primary` / `#bare` / `#muted`）。本页自己的字体规则
（`#reviewTitle` / `#reviewLemma` / `#reviewHint` / …）**住在 `theme.py` 的
模板里**：早先它们是在这里内联拼出来的，同一个语义就此有了两处定义。
selfcheck 那条「不许在界面代码里出现字面颜色」的扫描盯着这里，另有一条盯着
「这份文件里不许再出现内联的样式串」。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import review
from ..core.log import get_logger, log_exception
from ..core.models import Entry
from ..core.repository import Repository
from .theme import stylesheet

# 三档评分按钮 + 一颗归档。顺序即 FORGOT / FUZZY / REMEMBERED / EASY，
# 但最后一颗的点击走的是另一条路径（见 `_grade`）。
_GRADE_BUTTONS = (
    ("忘了", review.FORGOT),
    ("模糊", review.FUZZY),
    ("记住了", review.REMEMBERED),
    ("太简单", review.EASY),
)


class ReviewDialog(QDialog):
    """一轮复习会话。构造时把到期词排好队，逐条翻面评分。

    `embedded=True` 时不再是一个弹窗，而是主窗口内容区里的一页：窗口标志降级
    成普通控件、Esc 不再关掉它、底部那颗「结束复习 / 关闭」改成「返回生词本」，
    点击只发一个信号由主窗口切页。评分、归档、排队的逻辑一模一样——同一个类
    承担两种外壳，就不会出现「弹窗里能归档、页面上不能」这类分叉。
    """

    # 内嵌模式下的「退出这一页」。弹窗模式走 accept / reject，不发这个。
    leaveRequested = Signal()

    def __init__(
        self,
        repo: Repository,
        theme_id: str,
        parent: QWidget | None = None,
        *,
        embedded: bool = False,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._embedded = embedded
        # 单一时钟读数：due 判断与评分推进用同一个「现在」，会话内一致。
        # `datetime` 对象给 apply_review 做日期运算，`format_timestamp` 转成
        # 字符串给 due_entries 做字典序比较——两种形态都从这一个读数派生。
        self._now = datetime.now()
        self._queue: list[Entry] = repo.due_entries(review.format_timestamp(self._now))
        self._index = 0
        self._graded = 0
        # 本轮归档了几条。单独计数而不是从 `_ratings` 里推：归档不是评分，
        # 混进「四档分布」会让复习质量报告把「这个词我根本不用学」算成
        # 「我记得特别牢」。
        self._archived = 0
        # 三档各评了几条。SM-2 的间隔与难度系数只能靠这份分布来调：
        # 只知道「评了 20 条」时，全评「记住了」和半数「忘了」看起来一模一样。
        self._ratings: dict[int, int] = {
            review.FORGOT: 0,
            review.FUZZY: 0,
            review.REMEMBERED: 0,
        }
        self._revealed = False
        # 三个状态各自建不同的按钮，统一在 __init__ 置初值，避免测试在「完成态」
        # 去摸「翻面按钮」这种不存在的引用。
        self._btn_reveal: QPushButton | None = None
        self._btn_close: QPushButton | None = None
        self._grade_buttons: list[QPushButton] = []

        self.setWindowTitle("复习")
        self.setModal(not embedded)
        self.setObjectName("reviewPane" if embedded else "reviewDialog")
        if embedded:
            # 去掉窗口标志，它就是一个普通控件。不这么做的话它会自己开一个
            # 顶层窗口，`addWidget` 进去只能看到一块空白。
            self.setWindowFlags(Qt.WindowType.Widget)
        else:
            self.setMinimumWidth(480)

        # 样式自持（同欢迎页）：本页的字体规则在 `theme.py` 的模板里。
        self.setStyleSheet(stylesheet(theme_id))

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 22)
        root.setSpacing(8)

        self._title = QLabel("复习")
        self._title.setObjectName("reviewTitle")
        root.addWidget(self._title)

        self._progress = QLabel()
        self._progress.setObjectName("reviewProgress")
        root.addWidget(self._progress)

        # 内容区（正面 / 翻面 / 完成）与底栏（评分 / 关闭）都在这里重建。
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 6, 0, 6)
        self._body_layout.setSpacing(10)
        root.addWidget(self._body, 1)

        self._footer = QVBoxLayout()
        self._footer.setSpacing(8)
        root.addLayout(self._footer)

        self._render()

    # ---------- 状态（供测试读取，也供内部渲染分派） ----------

    @property
    def current_entry(self) -> Entry | None:
        if 0 <= self._index < len(self._queue):
            return self._queue[self._index]
        return None

    @property
    def total(self) -> int:
        return len(self._queue)

    @property
    def graded(self) -> int:
        return self._graded

    @property
    def archived(self) -> int:
        """本轮移出生词本的条数。"""
        return self._archived

    def ratings(self) -> dict[str, int]:
        """三档各评了几条，键名与统计白名单一致。

        返回新字典而不是内部计数器：调用方拿它去记统计，不该顺手把计数改掉。
        键名在这里定，是因为评分按钮也在这里——两处分头起名，报告里就会
        同时出现 `forgot` 与 `again`。

        不含 `easy`：归档不是记忆评分，它记在 `archived` 里。统计侧若还认
        这个键，读到的是「没评过」，而不是假装用户点了一次很轻松的评价。
        """
        return {
            "forgot": self._ratings[review.FORGOT],
            "fuzzy": self._ratings[review.FUZZY],
            "remembered": self._ratings[review.REMEMBERED],
        }

    @property
    def revealed(self) -> bool:
        return self._revealed

    # ---------- 渲染 ----------

    def _fit(self, label: QLabel) -> QLabel:
        """正文限宽后返回自身，方便就地套用。

        弹窗时代这一页最宽 480，用不上。嵌进内容区之后有八百多像素，一句
        「seeming to be everywhere at the same time」会被拉成一条很长的行，
        读的时候眼睛要来回甩。限到 680 之后每行六十来个字符，接近正文的
        舒适宽度。
        """
        label.setMaximumWidth(680)
        return label

    def _clear_layout(self, layout) -> None:
        """清空一个布局里的全部子项（递归拆嵌套布局）。"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _render(self) -> None:
        self._clear_layout(self._body_layout)
        self._clear_layout(self._footer)
        # 三个状态各自建不同按钮，渲染前把引用重置，避免「完成态」还持着上一
        # 状态里被 deleteLater 的控件（跨状态读它会是已析构对象）。
        self._btn_reveal = None
        self._btn_close = None
        self._grade_buttons = []
        entry = self.current_entry
        if entry is None:
            self._render_done()
            return
        self._progress.setText(
            f"第 {self._index + 1} / {len(self._queue)} 个　已评 {self._graded} 个"
        )
        if self._revealed:
            self._render_back(entry)
        else:
            self._render_front(entry)

    def _render_front(self, entry: Entry) -> None:
        lemma = QLabel(entry.lemma)
        lemma.setObjectName("reviewLemma")
        lemma.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lemma.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body_layout.addStretch(1)
        self._body_layout.addWidget(lemma)

        if entry.phonetic:
            phonetic = QLabel(entry.phonetic)
            phonetic.setObjectName("reviewPhonetic")
            phonetic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_layout.addWidget(phonetic)

        hint = QLabel("先回想一下这个词的意思，再翻面看答案")
        hint.setObjectName("reviewHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body_layout.addWidget(hint)
        self._body_layout.addStretch(2)

        reveal = QPushButton("显示答案")
        reveal.setObjectName("primary")
        reveal.setCursor(Qt.CursorShape.PointingHandCursor)
        reveal.setMinimumWidth(300)
        reveal.clicked.connect(self._reveal)
        self._btn_reveal = reveal
        # 居中且限宽。内嵌之后内容区有八百多像素，一个撑满整行的主按钮
        # 会变成一条色带，反而看不出它是按钮。
        self._footer.addWidget(reveal, 0, Qt.AlignmentFlag.AlignHCenter)

        close = QPushButton(self._leave_text("结束复习"))
        close.setObjectName("bare")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self._finish)
        self._btn_close = close
        self._footer.addWidget(close, 0, Qt.AlignmentFlag.AlignHCenter)

    def _render_back(self, entry: Entry) -> None:
        # 上下各一个弹簧，内容就落在垂直中间。只放底部那个的话，内嵌到
        # 八百多像素高的一页里，释义全贴在顶上，中间空出一大块。
        self._body_layout.addStretch(1)
        lemma = QLabel(entry.lemma)
        lemma.setObjectName("reviewLemma")
        lemma.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body_layout.addWidget(lemma)

        if entry.definition_en:
            self._body_layout.addWidget(_section("英文释义"))
            en = self._fit(QLabel(entry.definition_en))
            en.setObjectName("reviewBody")
            en.setWordWrap(True)
            self._body_layout.addWidget(en)

        if entry.definition_zh:
            self._body_layout.addWidget(_section("中文释义"))
            zh = self._fit(QLabel(entry.definition_zh))
            zh.setObjectName("reviewBody")
            zh.setWordWrap(True)
            self._body_layout.addWidget(zh)

        if entry.example:
            self._body_layout.addWidget(_section("例句"))
            example = self._fit(QLabel(entry.example))
            example.setObjectName("reviewBody")
            example.setWordWrap(True)
            self._body_layout.addWidget(example)
            if entry.example_zh:
                example_zh = self._fit(QLabel(entry.example_zh))
                example_zh.setObjectName("reviewSub")
                example_zh.setWordWrap(True)
                self._body_layout.addWidget(example_zh)

        if entry.collocations:
            self._body_layout.addWidget(_section("固定搭配"))
            for coll in entry.collocations:
                text = coll.phrase
                if coll.gloss_zh:
                    text = f"{text}　{coll.gloss_zh}"
                coll_label = self._fit(QLabel(text))
                coll_label.setObjectName("reviewSub")
                coll_label.setWordWrap(True)
                self._body_layout.addWidget(coll_label)

        self._body_layout.addStretch(1)

        # 评分按钮并排，各占一等份。不做主次：让用户按真实记忆状态点。
        row = QHBoxLayout()
        row.setSpacing(8)
        self._grade_buttons: list[QPushButton] = []
        for text, grade in _GRADE_BUTTONS:
            button = QPushButton(text)
            button.setObjectName("grade")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(38)
            button.clicked.connect(lambda _=False, g=grade: self._grade(g))
            if grade == review.EASY:
                # 「太简单」不是一个更宽容的评分，它会把词移走。这颗按钮必须
                # 自己说明这一点：四颗按钮长得一样，用户点下去之前无从预料
                # 这一颗的结果比另外三颗重得多。
                button.setToolTip("这个词不用再复习了，移出生词本（可在「已归档」里找回）")
            row.addWidget(button, 1)
            self._grade_buttons.append(button)
        self._footer.addLayout(row)

        note = QLabel("「太简单」= 移出生词本，不再出现在复习里")
        note.setObjectName("reviewHint")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._footer.addWidget(note)

        close = QPushButton(self._leave_text("结束复习"))
        close.setObjectName("bare")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self._finish)
        self._btn_close = close
        self._footer.addWidget(close, 0, Qt.AlignmentFlag.AlignHCenter)

    def _render_done(self) -> None:
        self._progress.setText("")
        # 末尾附一行全库摘要：用户刚做完一轮，正是想「我整体到什么程度了」的时候。
        # 口径与「数据不足」的说法都在 review_stats 里，界面不另写一套。
        from ..core.review_stats import summary_text

        library_note = summary_text(self.repo.conn)
        if self._queue:
            head = QLabel("本轮复习完成")
            head.setObjectName("reviewLemma")
            head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_layout.addStretch(1)
            self._body_layout.addWidget(head)
            archived_note = (
                f"，其中 {self._archived} 个判为太简单、已移出生词本"
                if self._archived
                else ""
            )
            note = QLabel(
                f"共复习 {self._graded} 个词{archived_note}。\n"
                f"被评「忘了」的词会尽快再出现。{library_note}"
            )
        else:
            head = QLabel("今天没有到期的词")
            head.setObjectName("reviewLemma")
            head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_layout.addStretch(1)
            self._body_layout.addWidget(head)
            note = QLabel(
                f"查词即自动进入复习计划，到期的词会出现在这里。\n{library_note}"
            )
        note.setObjectName("reviewHint")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setWordWrap(True)
        self._body_layout.addWidget(note)
        self._body_layout.addStretch(2)

        close = QPushButton(self._leave_text("关闭"))
        close.setObjectName("primary")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setMinimumWidth(300)
        close.clicked.connect(self._finish)
        self._btn_close = close
        self._footer.addWidget(close, 0, Qt.AlignmentFlag.AlignHCenter)

    # ---------- 动作 ----------

    def _leave_text(self, dialog_text: str) -> str:
        """底部那颗退出按钮的文案。

        弹窗里说「结束复习」是清楚的；嵌进内容区之后，它结束的不是一次会话，
        而是切回生词本这一页，沿用原文案会让人以为点了之后会话被丢弃。
        """
        return "返回生词本" if self._embedded else dialog_text

    def _finish(self) -> None:
        """退出这一轮。

        两条路径分开：**内嵌时不能 accept / reject**——QDialog 的这两个方法会
        把控件 hide 掉，而它是内容区的一页，藏起来只会在原地留一块空白，
        用户还得回左栏再点一次才能回来。
        """
        if self._embedded:
            self.leaveRequested.emit()
            return
        if self.current_entry is None:
            self.accept()
        else:
            self.reject()

    def _reveal(self) -> None:
        self._revealed = True
        self._render()

    def _grade(self, grade: int) -> None:
        entry = self.current_entry
        if entry is None or entry.id is None:
            return
        # 「太简单」走归档而不是评分：它不改变间隔，而是把词移出复习循环。
        if grade == review.EASY:
            if not self._archive(entry):
                return
            self._archived += 1
            self._graded += 1
            self._advance()
            return
        # 写不进去就**不能翻到下一张**：翻过去了等于告诉用户「这一张记下了」，
        # 而它的复习间隔一格没动，下次还按老日期冒出来，用户会以为程序忘了。
        # 停在原地并说明原因，至少这一次评分还有救。
        try:
            self._repo.apply_review(entry.id, grade, self._now)
        except Exception as exc:  # noqa: BLE001 - 写库失败要说人话
            log_exception(get_logger("ui.review"), "复习评分写入失败", exc)
            QMessageBox.warning(
                self,
                "没能记下这次评分",
                f"复习进度没有写进数据库，这一张停在原处。\n\n{exc}",
            )
            return
        self._graded += 1
        # 认不出的档位不记，也不新建键：统计的取值集合是固定的三档，
        # 多一个键写进报告就是多一个要归一化的脏值。
        if grade in self._ratings:
            self._ratings[grade] += 1
        self._advance()

    def _archive(self, entry: Entry) -> bool:
        """把当前词移出生词本。返回是否成功（失败时已就地说明原因）。

        与评分同一条纪律：没写进库就不许翻页。归档是个「看起来没了」的动作，
        最坏的结果恰恰是界面显示成功而库里没动——用户回来找不到那个词，
        以为程序把它弄丢了。
        """
        try:
            self._repo.archive(entry.id, self._now)
        except Exception as exc:  # noqa: BLE001 - 写库失败要说人话
            log_exception(get_logger("ui.review"), "归档写入失败", exc)
            QMessageBox.warning(
                self,
                "没能移出生词本",
                f"这个词条没有改动，仍留在原处。\n\n{exc}",
            )
            return False
        return True

    def _advance(self) -> None:
        """翻到下一张。评分与归档共用这一处，翻页行为只写一遍。"""
        self._index += 1
        self._revealed = False
        self._render()


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("reviewSection")
    return label
