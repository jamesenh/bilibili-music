"""下载任务对话框:列出"缓存整个合集"的任务、进度与暂停/继续/移除。

这一页回答的是"我让它缓存的那几个合集,现在下到哪儿了"。它与本地缓存页、最近播放页
同一条纪律:数据由上层从 :class:`~bilibili_music.audio.downloader.Downloader` 取出来
推下去,用户操作变成信号发上去 —— 控件不 import 下载器,也不自己算进度。

设计取舍
--------

* **非模态**:缓存任务本来就在后台跑,把主窗口锁住只为了让用户看进度是说不通的。
  关掉对话框任务照跑,再点"下载任务"就能重新打开(因此主窗口只保留一个实例,
  见 ``MainWindow._show_task_dialog``)。
* **一行一个任务,进度条按分P个数走**:任务是一整个合集,行里放不下 200 个分P的明细,
  也没必要 —— "已完成 3 / 共 200 个分P" + 当前分P的百分比已经说清楚了。
  按分P数而不是字节数画进度条,是为了不出现"卡在 99% 十几分钟"(最后几首是长曲)。
* **每个任务自带暂停/继续/移除**,顶部另有"全部暂停 / 全部继续 / 清空已完成":
  单个任务的操作是最常用的,批量操作则用来一把停下(比如要出门了)。
* **不显示速度与剩余时间**:那要靠滑动平均估算,估得不准反而添乱;模块 docstring
  里也说明了不预先跑一遍 playurl 去算总量(那要几百次请求)。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.download_task import DownloadTask, TaskState
from ...core.models import format_size
from ..icons import DARK, get_icon
from .elided_label import ElidedLabel
from .placeholder import PlaceholderPage

__all__ = [
    "DIALOG_SIZE",
    "TaskDialog",
    "TaskRow",
]

#: 对话框初始尺寸(像素):宽度要放得下"标题 + 状态 + 两个按钮"一行,
#: 高度够显示四五个任务,再多就滚动。
DIALOG_SIZE = (660, 460)


class TaskRow(QFrame):
    """任务列表里的一行:标题、状态、进度条与两个按钮。

    信号:
        pause_requested(str): 点了"暂停",携带 ``bvid``
        resume_requested(str): 点了"继续",携带 ``bvid``
        remove_requested(str): 点了"移除",携带 ``bvid``

    Args:
        bvid: 这一行对应的任务标识(信号里带出去,免得上层再去猜行号)。
        parent: Qt 父对象。
    """

    pause_requested = Signal(str)
    resume_requested = Signal(str)
    remove_requested = Signal(str)

    def __init__(self, bvid: str, parent: QWidget | None = None) -> None:
        """建好标题行、详情行与进度条(内容留空,等 :meth:`set_task` 填)。

        Args:
            bvid: 任务标识。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("TaskRow")
        self.bvid = bvid
        #: 当前状态;按钮点击时据此决定该发"暂停"还是"继续"
        self._state = TaskState.PAUSED

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 10, 4, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.title_label = ElidedLabel()
        self.title_label.setObjectName("TaskTitle")
        self.state_label = QLabel("")
        self.state_label.setObjectName("TaskState")
        self.action_button = QPushButton("暂停")
        self.action_button.setObjectName("GhostTextButton")
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.clicked.connect(self._on_action)
        self.remove_button = QPushButton("移除")
        self.remove_button.setObjectName("GhostTextButton")
        self.remove_button.setIcon(get_icon("trash", DARK.muted, 14))
        self.remove_button.setToolTip("只从列表里去掉这条任务,已经下载的音频不会被删除")
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.bvid))
        top.addWidget(self.title_label, 1)
        top.addWidget(self.state_label)
        top.addWidget(self.action_button)
        top.addWidget(self.remove_button)
        layout.addLayout(top)

        self.detail_label = ElidedLabel()
        self.detail_label.setObjectName("TaskDetail")
        layout.addWidget(self.detail_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("TaskProgress")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

    def set_task(self, task: DownloadTask, page_progress: tuple[int, int] = (0, 0)) -> None:
        """按任务当前的样子刷新这一行。

        Args:
            task: 任务(它的字段是唯一的数据来源;本控件不做任何推断)。
            page_progress: 当前分P的 ``(已收字节, 总字节)``;总长未知时为 ``(0, 0)``。
        """
        self.bvid = task.bvid
        self._state = task.state
        self.title_label.setText(task.display_title)
        self.state_label.setText(task.state.text)
        self.progress_bar.setValue(task.percent)
        self.detail_label.setText(self._detail_text(task, page_progress))
        self.action_button.setText("暂停" if task.state.is_active else "继续")
        # 已完成的任务没有什么可暂停/继续的,把按钮禁掉而不是留着让人点了没反应
        self.action_button.setEnabled(not task.state.is_finished)

    @staticmethod
    def _detail_text(task: DownloadTask, page_progress: tuple[int, int]) -> str:
        """拼详情行:进度 + 已下载体积 + 当前分P + 失败原因(有的话)。

        失败原因留在行里(而不是只在弹窗里露一次),因为弹窗关掉之后用户很可能要回头
        再看一眼到底为什么挂了。
        """
        parts = [task.progress_text]
        if task.bytes_done:
            parts.append(f"已下载 {format_size(task.bytes_done)}")
        if task.state is TaskState.RUNNING and task.page_index > 0:
            parts.append(f"正在缓存 P{task.page_index}{_page_percent_text(page_progress)}")
        elif task.state is TaskState.FAILED and task.page_index > 0:
            parts.append(f"停在 P{task.page_index}")
        if task.error:
            parts.append(f"失败原因:{task.error}")
        return " · ".join(parts)

    def _on_action(self) -> None:
        """按钮点击:活动状态发"暂停",否则发"继续"。"""
        if self._state.is_active:
            self.pause_requested.emit(self.bvid)
        else:
            self.resume_requested.emit(self.bvid)


def _page_percent_text(page_progress: tuple[int, int]) -> str:
    """把当前分P的进度拼成 `` (42%)``;总长未知时返回空串。

    CDN 不一定给 ``Content-Length``(实测部分 CDN 响应就没有),那时只能不显示百分比,
    而不是拿一个瞎猜的总长去算。

    Args:
        page_progress: ``(已收字节, 总字节)``。

    Returns:
        形如 ``" (42%)"`` 的后缀,或空串。
    """
    done, total = page_progress
    if total <= 0:
        return ""
    return f" ({max(0, min(100, int(done / total * 100)))}%)"


class TaskDialog(QDialog):
    """下载任务列表(非模态)。

    信号:
        pause_requested(str): 请求暂停某个任务
        resume_requested(str): 请求继续某个任务
        remove_requested(str): 请求从列表移除某个任务
        pause_all_requested(): 请求暂停全部任务
        resume_all_requested(): 请求继续全部任务
        clear_finished_requested(): 请求清掉已完成的任务记录

    Args:
        progress_of: 取"某个任务当前分P下载进度"的回调(形如
            ``Downloader.page_progress``);``None`` 表示不显示当前分P的百分比。
            用回调而不是把下载器整个传进来:控件只该认识它要显示的那几个数字。
        parent: Qt 父对象。
    """

    pause_requested = Signal(str)
    resume_requested = Signal(str)
    remove_requested = Signal(str)
    pause_all_requested = Signal()
    resume_all_requested = Signal()
    clear_finished_requested = Signal()

    def __init__(
        self,
        progress_of: Callable[[str], tuple[int, int]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """建好头部按钮、滚动区与空态页。

        Args:
            progress_of: 当前分P进度的查询回调。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("TaskDialog")
        self.setWindowTitle("下载任务")
        self.setModal(False)
        self.resize(*DIALOG_SIZE)
        self._progress_of = progress_of
        #: ``bvid -> 行控件``。按 ``bvid`` 而不是行号索引:任务可能在刷新之间被删掉,
        #: 行号会错位,而 ``bvid`` 不会。
        self._rows: dict[str, TaskRow] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())

        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.rows_layout.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("TaskScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.rows_host)

        self.empty_page = PlaceholderPage()
        self.empty_page.set_content(
            "还没有下载任务",
            "在搜索结果里右键一个视频,选择缓存整个合集",
        )

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(self.scroll)
        self.body.addWidget(self.empty_page)
        layout.addLayout(self.body, 1)

        self._render_empty(True)

    # ------------------------------------------------------------ 构建界面

    def _build_header(self) -> QHBoxLayout:
        """建"标题 + 全部暂停 / 全部继续 / 清空已完成"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        self.title_label = QLabel("下载任务")
        self.title_label.setObjectName("PageTitle")
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedLabel")
        self.pause_all_button = QPushButton("全部暂停")
        self.pause_all_button.setObjectName("GhostTextButton")
        self.pause_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_all_button.clicked.connect(self.pause_all_requested.emit)
        self.resume_all_button = QPushButton("全部继续")
        self.resume_all_button.setObjectName("GhostTextButton")
        self.resume_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resume_all_button.clicked.connect(self.resume_all_requested.emit)
        self.clear_button = QPushButton("清空已完成")
        self.clear_button.setObjectName("GhostTextButton")
        self.clear_button.setIcon(get_icon("trash", DARK.muted, 14))
        self.clear_button.setToolTip("只从列表里去掉已完成的任务,音频文件不会被删除")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.clicked.connect(self.clear_finished_requested.emit)

        row.addWidget(self.title_label)
        row.addWidget(self.summary_label)
        row.addStretch(1)
        row.addWidget(self.pause_all_button)
        row.addWidget(self.resume_all_button)
        row.addWidget(self.clear_button)
        return row

    # ------------------------------------------------------------ 上层推状态

    def set_tasks(self, tasks: Sequence[DownloadTask]) -> None:
        """整体重建任务列表(增删或状态变化后调用)。

        Args:
            tasks: 全部任务,顺序由上层决定(创建顺序)。
        """
        wanted = [task.bvid for task in tasks]
        for bvid in list(self._rows):
            if bvid not in wanted:
                row = self._rows.pop(bvid)
                self.rows_layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
        for task in tasks:
            row = self._rows.get(task.bvid)
            if row is None:
                row = TaskRow(task.bvid)
                row.pause_requested.connect(self.pause_requested.emit)
                row.resume_requested.connect(self.resume_requested.emit)
                row.remove_requested.connect(self.remove_requested.emit)
                self._rows[task.bvid] = row
                # 插到末尾那个 stretch 之前
                self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
            row.set_task(task, self._page_progress(task.bvid))
        self._update_summary(tasks)
        self._render_empty(not tasks)

    def update_task(self, task: DownloadTask) -> None:
        """只刷新一个任务那一行(进度回调走这条,免得整列表重建)。

        Args:
            task: 变化了的任务;列表里没有它时**新建一行**(正常不会发生,
                但进度回调与列表刷新之间确实存在竞态,补一行比丢掉进度好)。
        """
        row = self._rows.get(task.bvid)
        if row is None:
            row = TaskRow(task.bvid)
            row.pause_requested.connect(self.pause_requested.emit)
            row.resume_requested.connect(self.resume_requested.emit)
            row.remove_requested.connect(self.remove_requested.emit)
            self._rows[task.bvid] = row
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        row.set_task(task, self._page_progress(task.bvid))
        self._render_empty(False)

    def row_for(self, bvid: str) -> TaskRow | None:
        """取某个任务的行控件(测试与"滚动到某一行"用)。"""
        return self._rows.get(bvid)

    # ------------------------------------------------------------ 内部

    def _page_progress(self, bvid: str) -> tuple[int, int]:
        """取某个任务当前分P的进度;没有查询回调时返回 ``(0, 0)``。"""
        if self._progress_of is None:
            return (0, 0)
        return self._progress_of(bvid)

    def _update_summary(self, tasks: Sequence[DownloadTask]) -> None:
        """更新标题旁的"共 N 个任务,其中 M 个未完成"与按钮可用性。"""
        total = len(tasks)
        active = sum(1 for task in tasks if not task.state.is_finished)
        self.summary_label.setText(f"{total} 个任务 · {active} 个未完成" if total else "")
        self.pause_all_button.setEnabled(active > 0)
        self.resume_all_button.setEnabled(active > 0)
        self.clear_button.setEnabled(total > active)

    def _render_empty(self, empty: bool) -> None:
        """列表为空时显示空态页,否则显示列表。"""
        self.scroll.setVisible(not empty)
        self.empty_page.setVisible(empty)
