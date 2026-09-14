"""深色主题的应用(单主题)。

为什么只有一套主题
------------------

界面的设计稿本身就是深色单主题(近黑底 + B站粉强调色),不做浅色变体。因此这里
不再有"按名字取调色板"的入口:``icons.DARK`` 就是全部颜色来源,少一层"主题名 →
配色"的间接,也就少一处"改了一处忘了另一处"的可能。

为什么 QPalette 与样式表都要
----------------------------

两者分管的东西不重叠,缺一不可:

* ``QPalette`` 决定**控件默认色**(底/字/选中/禁用),``QAbstractItemView`` 的选中色、
  ``QMenu`` 底色这些由 QSS 管不到的地方全靠它;禁用态更是只有它能给(``AGENTS.md``
  第 5 节:禁用文字与正常文字必须能一眼分开)。
* QSS 决定**版式细节**:圆角、内边距、悬停与选中态的底色、粉色主按钮 —— 设计稿里的
  搜索框、侧栏选中胶囊、圆形播放按钮都不是 QPalette 能表达的。

为什么切到 ``Fusion`` 样式:Windows 的原生样式对 ``QPalette`` 响应不完整(深色主题会
部分失效),``Fusion`` 在两个平台上都老老实实遵循 ``QPalette`` 与 QSS。代价是控件外观
不再是系统原生 —— 对自绘界面的项目可以接受。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .icons import DARK, Palette

__all__ = [
    "SURFACES",
    "Surfaces",
    "apply_theme",
    "build_palette",
    "stylesheet",
]


@dataclass(frozen=True, slots=True)
class Surfaces:
    """深色主题里**不属于图标调色板**的表面色。

    图标调色板(:class:`~bilibili_music.ui.icons.Palette`)只管文字与图标颜色;面板、
    卡片、分隔线这些"面"放在这里,是为了让 QSS 与本模块的 ``QPalette`` 共用同一份取值
    —— 否则同一个底色会在样式字符串和调色板里各写一遍。

    取值思路是"比纯黑浅一点、层次靠亮度差":纯黑底配纯白字对比过强,长时间看很累;
    卡片的亮度比面板高一档,靠这个差别表达"这块是可点的"。
    """

    window: str
    """窗口与侧栏底色(最暗的一层)。"""

    panel: str
    """内容面板与播放条底色。"""

    card: str
    """卡片、列表行悬停时的底色。"""

    card_active: str
    """当前播放行/选中行的底色(带一点粉,与强调色呼应)。"""

    border: str
    """分隔线、输入框描边。"""

    field: str
    """输入框、下拉框、滑块槽的底色。"""

    accent_soft: str
    """强调色的低饱和铺底(侧栏选中胶囊)。"""


#: 全应用共用的表面色;QSS 与 :func:`build_palette` 都从这里取。
SURFACES = Surfaces(
    window="#17171A",
    panel="#1E1E22",
    card="#26262C",
    card_active="#37222C",
    border="#2C2C31",
    field="#26262B",
    accent_soft="#3A2531",
)


def build_palette() -> QPalette:
    """构造应用级 ``QPalette``(深色)。

    Returns:
        可直接交给 ``QApplication.setPalette`` 的调色板。
    """
    result = QPalette()
    #: 角色到颜色的映射;逐一 setColor 而不是用 setBrush,便于读
    mapping = {
        QPalette.ColorRole.Window: SURFACES.window,
        QPalette.ColorRole.WindowText: DARK.text,
        QPalette.ColorRole.Base: SURFACES.panel,
        QPalette.ColorRole.AlternateBase: SURFACES.window,
        QPalette.ColorRole.Text: DARK.text,
        QPalette.ColorRole.Button: SURFACES.panel,
        QPalette.ColorRole.ButtonText: DARK.text,
        QPalette.ColorRole.Highlight: DARK.accent,
        QPalette.ColorRole.HighlightedText: DARK.on_accent,
        QPalette.ColorRole.ToolTipBase: SURFACES.card,
        QPalette.ColorRole.ToolTipText: DARK.text,
        QPalette.ColorRole.PlaceholderText: DARK.muted,
    }
    for role, value in mapping.items():
        result.setColor(role, QColor(value))
    # 禁用态要单独给:否则禁用文字与正常文字几乎一样,按钮"看起来能点"
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
    ):
        result.setColor(QPalette.ColorGroup.Disabled, role, QColor(DARK.disabled))
    return result


def stylesheet() -> str:
    """生成覆盖全应用的 QSS。

    选择器一律用 ``#objectName`` 而不是按控件类型(``QPushButton {...}``)选:按类型选会
    连弹窗、下拉列表里的按钮一起改掉,而这里只想给设计稿里那几个具名部件上样式。
    控件侧因此有义务设置 ``setObjectName`` —— 名字由本模块集中约定,不散落在各处。

    Returns:
        可直接交给 ``QApplication.setStyleSheet`` 的样式表字符串。
    """
    s = SURFACES
    return f"""
/* ---------------------------------------------------------- 骨架 */
QWidget#AppRoot {{
    background: {s.window};
}}
QWidget#TitleBar {{
    background: {s.window};
}}
QWidget#Sidebar {{
    background: {s.window};
}}
QWidget#PlayerBar {{
    background: {s.panel};
    border-top: 1px solid {s.border};
}}
QWidget#QueuePanel {{
    background: {s.panel};
    border-radius: 10px;
}}
QWidget#CenterPanel {{
    background: {s.panel};
    border-radius: 10px;
}}

/* ---------------------------------------------------------- 标题栏 */
QLineEdit#SearchInput {{
    background: {s.field};
    border: 1px solid transparent;
    border-radius: 17px;
    padding: 0 34px 0 34px;
    min-height: 34px;
    color: {DARK.text};
    selection-background-color: {DARK.accent};
}}
QLineEdit#SearchInput:focus {{
    border: 1px solid {DARK.accent};
}}
QPushButton#PrimaryButton {{
    background: {DARK.accent};
    color: {DARK.on_accent};
    border: none;
    border-radius: 17px;
    padding: 0 22px;
    min-height: 34px;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background: #FF85A9;
}}
QPushButton#PrimaryButton:pressed {{
    background: #E4628A;
}}
QPushButton#PrimaryButton:disabled {{
    background: {s.card};
    color: {DARK.disabled};
}}
QPushButton#WindowButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
}}
QPushButton#WindowButton:hover {{
    background: {s.card};
}}
QPushButton#WindowButton[closing="true"]:hover {{
    background: #E81123;
}}

/* ---------------------------------------------------------- 侧栏 */
QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 0 12px;
    min-height: 36px;
    text-align: left;
    color: {DARK.muted};
}}
QPushButton#NavButton:hover {{
    background: {s.card};
    color: {DARK.text};
}}
QPushButton#NavButton:checked {{
    background: {s.accent_soft};
    color: {DARK.accent};
}}
QLabel#SectionLabel {{
    color: {DARK.muted};
    font-size: 12px;
}}
QPushButton#PlaylistRow {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 0 12px;
    min-height: 32px;
    text-align: left;
    color: {DARK.muted};
}}
QPushButton#PlaylistRow:hover {{
    background: {s.card};
    color: {DARK.text};
}}
QPushButton#PlaylistRow:checked {{
    background: {s.accent_soft};
    color: {DARK.accent};
}}
QPushButton#IconGhostButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
}}
QPushButton#IconGhostButton:hover {{
    background: {s.card};
}}

/* ---------------------------------------------------------- 内容区 */
QLabel#PageTitle {{
    color: {DARK.text};
    font-size: 20px;
    font-weight: 600;
}}
QLabel#PageQuery {{
    color: {DARK.text};
    font-size: 14px;
}}
QLabel#MutedLabel {{
    color: {DARK.muted};
}}
QLabel#PanelTitle {{
    color: {DARK.text};
    font-size: 15px;
    font-weight: 600;
}}
/* 本地缓存页的过滤框。与播放条上的音质下拉同尺寸口径(30px 高 / 8px 圆角),
   两者是同一层级的"页内小控件",长得差太多会显得不是一套界面 */
QLineEdit#FilterInput {{
    background: {s.field};
    border: 1px solid {s.border};
    border-radius: 8px;
    padding: 0 10px;
    min-height: 30px;
    color: {DARK.text};
    selection-background-color: {DARK.accent};
}}
QLineEdit#FilterInput:focus {{
    border: 1px solid {DARK.accent};
}}
QPushButton#GhostTextButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 3px 8px;
    color: {DARK.muted};
}}
QPushButton#GhostTextButton:hover {{
    background: {s.card};
    color: {DARK.text};
}}
QLabel#StatusLabel {{
    color: {DARK.muted};
    font-size: 12px;
}}
QTableWidget#TrackTable {{
    background: transparent;
    border: none;
    outline: none;
}}
QTableWidget#TrackTable::item {{
    border-bottom: 1px solid {s.border};
    padding: 0 8px;
    color: {DARK.text};
}}
QTableWidget#TrackTable::item:hover {{
    background: {s.card};
}}
QTableWidget#TrackTable::item:selected {{
    background: {s.card_active};
    color: {DARK.text};
}}
QTableWidget#TrackTable QHeaderView::section {{
    background: transparent;
    border: none;
    border-bottom: 1px solid {s.border};
    padding: 6px 8px;
    color: {DARK.muted};
    font-size: 12px;
}}
QLabel#TrackTitle {{
    color: {DARK.text};
    font-weight: 600;
}}
QLabel#TrackPrefix {{
    color: {DARK.muted};
}}
QLabel#TrackSubtitle {{
    color: {DARK.muted};
    font-size: 12px;
}}
/* 下载任务对话框:一行一个任务,行与行之间只用一条细线分隔 */
QFrame#TaskRow {{
    border-bottom: 1px solid {s.border};
}}
QLabel#TaskTitle {{
    color: {DARK.text};
    font-weight: 600;
}}
QLabel#TaskState {{
    color: {DARK.muted};
    font-size: 12px;
}}
QLabel#TaskDetail {{
    color: {DARK.muted};
    font-size: 12px;
}}
/* 全局的进度条被压成 4px(播放条上那条很细的缓存进度),任务行要高一点才看得出进度 */
QProgressBar#TaskProgress {{
    max-height: 6px;
}}
QLabel#CoverThumb {{
    background: {s.field};
    border-radius: 6px;
}}
QPushButton#RowActionButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
}}
QPushButton#RowActionButton:hover {{
    background: {s.card};
}}

/* ---------------------------------------------------------- 收藏夹显示/隐藏弹窗 */
/* 列表本身不该有第二个底色:滚动区与它的视口透明,漏出弹窗的窗口色即可 */
QScrollArea#FavVisibilityScroll, QWidget#FavVisibilityViewport {{
    background: transparent;
    border: none;
}}
QCheckBox#FavVisibilityCheck {{
    color: {DARK.text};
    padding: 4px 2px;
}}

/* ---------------------------------------------------------- 播放条 */
QPushButton#TransportButton {{
    background: transparent;
    border: none;
    border-radius: 16px;
}}
QPushButton#TransportButton:hover {{
    background: {s.card};
}}
QPushButton#TransportButton:checked {{
    background: {s.accent_soft};
}}
QPushButton#PlayButton {{
    background: {DARK.accent};
    border: none;
    border-radius: 22px;
}}
QPushButton#PlayButton:hover {{
    background: #FF85A9;
}}
QComboBox#QualityCombo {{
    background: {s.field};
    border: 1px solid {s.border};
    border-radius: 8px;
    padding: 0 10px;
    min-height: 30px;
    color: {DARK.text};
}}
QComboBox#QualityCombo::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox#QualityCombo QAbstractItemView {{
    background: {s.card};
    border: 1px solid {s.border};
    selection-background-color: {DARK.accent};
    color: {DARK.text};
}}

/* ---------------------------------------------------------- 分P选择器 */
/* 内边距 10 要与 page_selector.SELECTOR_PADDING 一致:控件按它扣掉可用宽度做省略 */
QPushButton#PageSelector {{
    background: {s.field};
    border: 1px solid {s.border};
    border-radius: 8px;
    padding: 0 10px;
    text-align: left;
    color: {DARK.text};
}}
QPushButton#PageSelector:hover {{
    border: 1px solid {DARK.accent};
}}
QPushButton#PageSelector:disabled {{
    color: {DARK.disabled};
}}
/* 菜单是 Qt::Popup 顶层窗口 + 半透明底:圆角要真的透出下面的内容 */
QFrame#PagePopup {{
    background: {s.card};
    border: 1px solid {s.border};
    border-radius: 8px;
}}
QScrollArea#PagePopupScroll, QWidget#PagePopupViewport {{
    background: transparent;
    border: none;
}}
QPushButton#PageRow {{
    background: transparent;
    border: none;
    border-radius: 6px;
    text-align: left;
}}
QPushButton#PageRow:hover {{
    background: {s.accent_soft};
}}
QPushButton#PageRow[current="true"] {{
    background: {s.card_active};
}}
QLabel#PageDot {{
    color: {DARK.muted};
    font-size: 12px;
}}
QLabel#PageDot[current="true"] {{
    color: {DARK.accent};
}}
QLabel#PageIndex {{
    color: {DARK.muted};
}}
/* 菜单行标题**不许**用 #PageTitle:那是内容区大标题(20px 加粗),
   菜单一行只有 34px 高,套上去字会撑满整行。见 page_selector.PageRow */
QLabel#PageRowTitle {{
    color: {DARK.text};
    font-size: 13px;
}}
QLabel#PageDuration {{
    color: {DARK.muted};
    font-size: 12px;
}}

/* ---------------------------------------------------------- 滑块与进度 */
QSlider::groove:horizontal {{
    height: 4px;
    background: {s.field};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {DARK.accent};
    border-radius: 2px;
}}
QSlider::add-page:horizontal {{
    background: {s.field};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 12px;
    height: 12px;
    margin: -4px 0;
    border-radius: 6px;
    background: {DARK.on_accent};
}}
QSlider#VolumeSlider::groove:horizontal {{
    height: 3px;
}}
QSlider#VolumeSlider::handle:horizontal {{
    width: 10px;
    height: 10px;
    margin: -4px 0;
    border-radius: 5px;
}}
QProgressBar {{
    background: {s.field};
    border: none;
    border-radius: 2px;
    max-height: 4px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {DARK.accent};
    border-radius: 2px;
}}

/* ---------------------------------------------------------- 滚动条与浮层 */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {s.border};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {DARK.disabled};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
}}
QScrollBar::handle:horizontal {{
    background: {s.border};
    border-radius: 4px;
    min-width: 24px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QMenu {{
    background: {s.card};
    border: 1px solid {s.border};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: 6px;
    color: {DARK.text};
}}
QMenu::item:selected {{
    background: {DARK.accent};
    color: {DARK.on_accent};
}}
QMenu::separator {{
    height: 1px;
    background: {s.border};
    margin: 4px 8px;
}}
QToolTip {{
    background: {s.card};
    color: {DARK.text};
    border: 1px solid {s.border};
    padding: 4px 8px;
}}
"""


def apply_theme(app: QApplication) -> Palette:
    """把深色主题应用到整个应用。

    主题是**应用级**的:同一个进程里所有窗口一起生效,避免出现"主窗口深色、弹窗浅色"。

    Args:
        app: 应用实例。

    Returns:
        图标调色板(:data:`~bilibili_music.ui.icons.DARK`);调用方拿它给需要显式染色的
        图标取色(SVG 是栅格化成位图后再染色的,样式表管不到)。
    """
    app.setStyle("Fusion")
    app.setPalette(build_palette())
    app.setStyleSheet(stylesheet())
    return DARK
