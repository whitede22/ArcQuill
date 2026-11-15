"""学习进度概览页。

四项数字 + 近七天新增条形图 + 一段复习摘要，底部是使用报告的入口。

**只做算得出来的指标。**「首次答对率」「长期记忆率」都需要每条词的复习历史，
而 `entries` 只存最后一次的间隔与次数（见 `review_stats` 的模块说明）。这一页
因此不显示它们，也不显示成 0%——把「没采集」画成 0% 会被读成「全忘了」。

七天图只画**新增词条数**。复习次数没有流水表可查，硬画就得拿 `review_count`
去凑，那是「这个词历史上复习过几次」，不是「哪天复习的」。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import review, review_stats
from ..core.repository import Repository
from .report_flow import ReportActions

# 条形图的最高柱高。再高会把下面那排日期挤到窗口外面去。
_BAR_MAX = 84
_CHART_DAYS = 7


class StatsPanel(QFrame):
    """学习进度一页。`refresh()` 重新算一遍，切页时调用。"""

    def __init__(
        self,
        repo: Repository,
        theme_id: str,
        config: dict | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._config = config if config is not None else {}
        self.setObjectName("panel")

        self._report = ReportActions(self, self._config, theme_id)
        self._report.busy_changed.connect(self._on_report_busy)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        title = QLabel("学习进度")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        self._subtitle = QLabel()
        self._subtitle.setObjectName("pageSub")
        self._subtitle.setWordWrap(True)
        root.addWidget(self._subtitle)

        # 四块数字卡。等宽排布，值在上、名在下。
        self._cards: dict[str, QLabel] = {}
        card_row = QHBoxLayout()
        card_row.setSpacing(10)
        for key, name in (
            ("total", "生词总数"),
            ("due", "待复习"),
            ("mastered", "已掌握"),
            ("archived", "已归档"),
        ):
            card_row.addWidget(self._build_card(key, name), 1)
        root.addLayout(card_row)

        chart_title = QLabel("近七天新增")
        chart_title.setObjectName("section")
        root.addWidget(chart_title)

        self._chart = QWidget()
        # 高度钉死。交给布局去伸缩的话，柱子会浮在一大块空白底下，
        # 看起来像图画到一半没画完。
        self._chart.setFixedHeight(150)
        self._chart_layout = QHBoxLayout(self._chart)
        self._chart_layout.setContentsMargins(0, 0, 0, 0)
        self._chart_layout.setSpacing(10)
        root.addWidget(self._chart)

        self._summary = QLabel()
        self._summary.setObjectName("muted")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        root.addStretch(1)
        root.addLayout(self._build_report_row())
        self.refresh()

    # ---------- 构建 ----------

    def _build_card(self, key: str, name: str) -> QFrame:
        card = QFrame()
        card.setObjectName("statCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)

        value = QLabel("0")
        value.setObjectName("statValue")
        caption = QLabel(name)
        caption.setObjectName("statName")
        layout.addWidget(value)
        layout.addWidget(caption)
        self._cards[key] = value
        return card

    def _build_report_row(self) -> QHBoxLayout:
        """使用报告的两个入口。

        保存是本地动作，随时可点；提交会真的发邮件出去，所以它排在后面、
        并且不带 `#primary`——这一页的主操作是「看进度」，不是「发数据」。
        """
        self._save_btn = QPushButton("保存使用报告…")
        self._save_btn.setToolTip("把这份使用情况存成 JSON，位置自己挑")
        self._save_btn.clicked.connect(self._save_report)

        self._submit_btn = QPushButton("提交使用报告…")
        self._submit_btn.setToolTip(
            "存一份到本机，再发给作者。署名与统计开关在设置里改。"
        )
        self._submit_btn.clicked.connect(self._submit_report)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._save_btn)
        row.addWidget(self._submit_btn)
        row.addStretch(1)
        return row

    # ---------- 数据 ----------

    def refresh(self) -> None:
        """重算全部数字。切到这一页、以及复习会话结束后都会调。"""
        now = review.now_timestamp()
        data = review_stats.summary(self._repo.conn)
        self._cards["total"].setText(str(self._repo.count()))
        self._cards["due"].setText(str(self._repo.due_count(now)))
        self._cards["mastered"].setText(str(data["mastered"]))
        self._cards["archived"].setText(str(self._repo.count_archived()))
        self._summary.setText(review_stats.summary_text(self._repo.conn))
        self._subtitle.setText(self._subtitle_text(data))
        self._rebuild_chart()

    def _subtitle_text(self, data: dict) -> str:
        if not self._repo.count():
            return "生词本还是空的。查一个词，这里就有数了。"
        return (
            f"复习计划里 {data['planned']} 个词，已开始复习 {data['reviewed']} 个，"
            f"平均间隔 {data['avg_interval']} 天。"
        )

    def _rebuild_chart(self) -> None:
        """按天分桶画七根柱子。每次刷新整块重建，不做增量。"""
        while self._chart_layout.count():
            item = self._chart_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

        buckets = self._daily_counts()
        peak = max(buckets.values()) if buckets else 0
        for day, count in buckets.items():
            self._chart_layout.addWidget(self._build_bar(day, count, peak), 1)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _daily_counts(self) -> dict[date, int]:
        """最近七天每天新增了几条，从最早的一天排到今天。

        取的是 `entries.created_at` 的日期部分。时间戳定长且高位在前，
        所以按前 10 个字符分组就够了，不必解析成 datetime。
        """
        today = datetime.now().date()
        days = [today - timedelta(days=offset) for offset in range(_CHART_DAYS - 1, -1, -1)]
        counts = {day: 0 for day in days}
        start = f"{days[0].isoformat()} 00:00:00"
        end = f"{days[-1].isoformat()} 23:59:59"
        for entry in self._repo.entries_added_between(start=start, end=end):
            try:
                stamp = date.fromisoformat(entry.created_at[:10])
            except ValueError:
                continue
            if stamp in counts:
                counts[stamp] += 1
        return counts

    def _build_bar(self, day: date, count: int, peak: int) -> QWidget:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addStretch(1)

        # 数字压在柱顶。没有它，两根差不多高的柱子只能靠猜。
        number = QLabel(str(count) if count else "")
        number.setObjectName("chartLabel")
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(number)

        # 零值那天不画柱子。给一根 2px 的横线，读起来像坐标轴刻度，
        # 而不是「这天没有新词」——上面那个 0 已经把话说清楚了。
        if count:
            bar = QFrame()
            bar.setObjectName("chartBar")
            bar.setFixedHeight(max(6, round(_BAR_MAX * count / max(1, peak))))
            # 定宽而不是撑满列。列宽随窗口走，撑满时一天只有两根柱子也叫柱状图，
            # 七根都撑满就成了一排色块。
            bar.setFixedWidth(36)
            layout.addWidget(bar, 0, Qt.AlignmentFlag.AlignHCenter)
        else:
            spacer = QWidget()
            spacer.setFixedHeight(2)
            layout.addWidget(spacer)

        caption = QLabel(f"{day.month}/{day.day}")
        caption.setObjectName("chartLabel")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(caption)
        return column

    # ---------- 报告 ----------

    def _save_report(self) -> None:
        self._report.save()

    def _submit_report(self) -> None:
        self._report.submit()

    def _on_report_busy(self, busy: bool) -> None:
        self._submit_btn.setEnabled(not busy)
        self._submit_btn.setText("提交中…" if busy else "提交使用报告…")
