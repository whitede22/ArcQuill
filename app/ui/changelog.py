"""更新日志对话框：把 `core.changelog.RELEASES` 摊成可滚动的几屏。

## 为什么单独一个对话框，而不复用欢迎页那一屏

欢迎页在升级时显示的是**最近几个版本**，最多三条，够用就走。回看入口要的是
全部历史——从 1.0.0 开始一条不落。两者内容重叠但用途不同：一个是「刚升级，
提醒一下」，一个是「我记得有个功能，哪版加的」。挤进同一个翻页结构里，前者会
被后者撑得翻不完。

## 样式

与欢迎页同一条纪律：不写字面颜色，只复用 QSS 里的 objectName
（`#welcomeTitle` / `#welcomeSub` / `#changelogVersion` / `#changelogPoint`），
并在构造时 `setStyleSheet(stylesheet(tid))` 让对话框样式自持——测试直接构造它
时不必先起 `QApplication` 的全局样式表。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.changelog import RELEASES, Release
from .theme import stylesheet
from .widgets import with_accent_bar


class ChangelogDialog(QDialog):
    """全部版本说明，新到旧。

    `releases` 可传入子集：欢迎页那个「看看更新日志」的出口可以只给本次相关的
    几条，虽然目前直接开全量。
    """

    def __init__(
        self,
        theme_id: str,
        parent: QWidget | None = None,
        releases: tuple[Release, ...] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("更新日志")
        self.setObjectName("changelogDialog")
        self.setModal(True)
        self.setStyleSheet(stylesheet(theme_id))
        self.setMinimumSize(560, 540)

        self._releases = releases if releases is not None else RELEASES

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 18)
        layout.setSpacing(0)

        title = QLabel("更新日志")
        title.setObjectName("welcomeTitle")
        layout.addWidget(title)

        sub = QLabel(f"{len(self._releases)} 个版本，新的在前面。")
        sub.setObjectName("welcomeSub")
        layout.addWidget(sub)
        layout.addSpacing(16)

        layout.addWidget(self._build_scroll(), 1)
        layout.addSpacing(14)

        close = QPushButton("关闭")
        close.setObjectName("primary")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setMinimumWidth(90)
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

    def _build_scroll(self) -> QScrollArea:
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 8, 0)
        v.setSpacing(0)

        for index, release in enumerate(self._releases):
            if index:
                v.addSpacing(20)
            v.addLayout(self._release_block(release))
        v.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        return scroll

    def _release_block(self, release: Release) -> QVBoxLayout:
        block = QVBoxLayout()
        block.setContentsMargins(0, 0, 0, 0)
        block.setSpacing(7)

        head = QLabel(
            f"{release.display_version()}　{release.date}"
        )
        head.setObjectName("changelogVersion")
        block.addWidget(head)

        for point in release.points:
            label = QLabel(point)
            label.setObjectName("changelogPoint")
            label.setWordWrap(True)
            block.addWidget(with_accent_bar(label))
        return block
