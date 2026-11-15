"""使用报告的保存与提交。

这两件事原先只长在设置页里。统计页要放同一排按钮，如果各写一份，「署名记不
记住」「发送失败要不要给一条 GitHub 备选」「发送中要不要锁按钮」这些细节会
在两处慢慢分叉。它们全都只在异常路径上体现，最难被发现，所以收成一份，
两个页面都调这里。

调用方只负责外观：按钮文案与可用状态由宿主自己改，本模块通过 `busy_changed`
说一声「开始了 / 结束了」。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from ..config import load_config, save_config
from ..core import report
from .report_dialog import ReportDialog
from .workers import submit


def ask_github(parent: QWidget, text: str, detail: str = "") -> bool:
    """问一句要不要改用 GitHub 提交，返回用户的选择。

    只报告失败时才带 detail；带了就是警告图标，没带就是普通信息。
    """
    box = QMessageBox(parent)
    box.setWindowTitle("提交使用报告")
    box.setIcon(
        QMessageBox.Icon.Warning if detail else QMessageBox.Icon.Information
    )
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    github = box.addButton("改用 GitHub 提交", QMessageBox.ButtonRole.ActionRole)
    box.addButton("知道了", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is github


class ReportActions(QObject):
    """保存与提交使用报告。一个页面一个实例，别跨页面共用。"""

    busy_changed = Signal(bool)

    def __init__(
        self,
        parent: QWidget,
        config: dict,
        theme_id: str,
        *,
        analytics_enabled: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._parent = parent
        self._config = config
        self._theme_id = theme_id
        self._busy = False
        # 设置页要的是**界面上当前**的勾选状态：用户取消了勾选但还没保存，
        # 此刻点提交就该被拦下。读 config 会放行，因为那里还是上一次保存的值。
        # 统计页没有那个复选框，退回读配置。
        self._enabled = analytics_enabled or (
            lambda: bool(self._config.get("analytics_enabled", False))
        )

    @property
    def busy(self) -> bool:
        return self._busy

    # ---------- 保存 ----------

    def save(self, theme_id: str | None = None) -> Path | None:
        """让用户挑位置存一份。返回落盘的路径，取消或失败返回 None。

        失败已经在对话框里说过一次原因（`ReportDialog._do_save` 会留在原地显示
        错误），这里不再叠一个弹窗。
        """
        payload = report.build_report(30)
        dialog = ReportDialog(
            theme_id or self._theme_id, payload, self._parent, mode="save"
        )
        if dialog.exec() and dialog.saved_path:
            QMessageBox.information(
                self._parent,
                "使用报告",
                f"已保存到：\n{dialog.saved_path}",
            )
            return dialog.saved_path
        return None

    # ---------- 提交 ----------

    def submit(self, theme_id: str | None = None) -> None:
        """存一份到默认目录，再发给作者。

        三道前置检查各自有明确的话要说，所以都拦在这里而不是等发送失败：
        统计没开、没有事件、没配提交地址。
        """
        if not self._enabled():
            # 提交是第二个独立决定：统计开着也不代表愿意发出去，所以这里不默认放行。
            QMessageBox.information(
                self._parent,
                "提交使用报告",
                "提交之前先勾选「在这台电脑上记录使用情况」。\n"
                "记录与提交是两个决定，程序不替你合并。",
            )
            return
        if self._busy:
            QMessageBox.information(
                self._parent, "提交使用报告", "上一份报告还在发送，等它结束再点。"
            )
            return
        payload = report.build_report(30)
        if not payload.get("event_total"):
            QMessageBox.information(
                self._parent,
                "提交使用报告",
                "还没有记录到事件，暂时没有可提交的内容。",
            )
            return
        if not report.endpoint():
            # 地址没配就没必要让用户填一遍表单再看失败。
            if ask_github(
                self._parent,
                "这个版本还没有配置提交地址，报告可以改用 GitHub 提交。",
            ):
                QDesktopServices.openUrl(QUrl(report.issue_url(payload)))
            return

        dialog = ReportDialog(
            theme_id or self._theme_id,
            payload,
            self._parent,
            mode="submit",
            submitter=str(self._config.get("report_name") or ""),
        )
        if not (dialog.exec() and dialog.saved_path):
            return
        self._remember_name(dialog.submitter)
        self._start(payload, dialog.saved_path)

    def _remember_name(self, name: str) -> None:
        """把署名记进配置，下次预填。

        直接落盘，不等设置页的「保存」：用户提交完通常就关窗口了，等不到他
        再去点一次保存。这份写入只覆盖署名一个键。
        """
        self._config["report_name"] = name
        cfg = load_config()
        cfg["report_name"] = name
        save_config(cfg)

    def _start(self, payload: dict, path: Path) -> None:
        self._set_busy(True)
        submit(
            report.submit,
            payload,
            on_success=lambda _: self._sent(path),
            on_error=lambda message, kind: self._failed(message, path, payload),
            on_finished=lambda: self._set_busy(False),
        )

    def _set_busy(self, value: bool) -> None:
        if self._busy == value:
            return
        self._busy = value
        self.busy_changed.emit(value)

    def _sent(self, path: Path) -> None:
        QMessageBox.information(
            self._parent,
            "提交使用报告",
            f"报告已发出。\n\n本机留了一份：\n{path}\n不需要的话可以删掉。",
        )

    def _failed(self, message: str, path: Path, payload: dict) -> None:
        """发不出去时把话说清楚，并留一条备用通道。

        报告已经落盘，所以失败不是白干；把 GitHub 那条路摆在按钮上，
        比让用户自己去项目主页找入口强。
        """
        detail = f"{message}\n\n报告已保存在：\n{path}"
        if ask_github(self._parent, "报告没有发出去。", detail):
            QDesktopServices.openUrl(QUrl(report.issue_url(payload)))
