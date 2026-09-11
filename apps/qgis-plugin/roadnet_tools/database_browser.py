"""Asynchronous two-pane database catalog browser."""

from urllib.parse import urlencode

from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QListWidget,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .client import BackendClient
from .core import endpoint_url
from .settings import PluginSettings


class RowCountItem(QTableWidgetItem):
    """Sort counts numerically, placing unknown below zero in ascending order."""

    def __init__(self, label, count):
        super().__init__(label)
        self.count = count

    def __lt__(self, other):
        if isinstance(other, RowCountItem):
            return (-1 if self.count is None else self.count) < (
                -1 if other.count is None else other.count
            )
        return super().__lt__(other)


class DatabaseBrowserDialog(QDialog):
    def __init__(self, parent=None, error_callback=None):
        super().__init__(parent)
        self.setWindowTitle("Roadnet 数据库浏览")
        self.resize(850, 520)
        self._error_callback = error_callback
        self._base_url = endpoint_url(PluginSettings.load().backend_url, "/api/v1/map/databases")
        self._client = BackendClient(self)
        self._client.request_succeeded.connect(self._loaded)
        self._client.request_failed.connect(self._failed)
        self.finished.connect(lambda _: self._client.cancel_all())

        self.databases = QListWidget()
        self.databases.currentTextChanged.connect(self._select_database)
        self.tables = QTableWidget(0, 3)
        self.tables.setHorizontalHeaderLabels(["Schema", "Table name", "Rows"])
        self.tables.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tables.setSortingEnabled(True)
        self.tables.horizontalHeader().setToolTip("点击列标题排序，再次点击切换升序/降序；Rows 按数值排序。")
        self.tables.horizontalHeader().setStretchLastSection(True)
        self.status = QLabel("正在读取数据库…")
        self.status.setWordWrap(True)
        refresh = QPushButton("刷新数据库")
        refresh.clicked.connect(self.refresh)
        panes = QSplitter()
        for title, widget in (("Database（点击选择）", self.databases), ("Table name / Rows", self.tables)):
            pane = QWidget()
            layout = QVBoxLayout(pane)
            layout.addWidget(QLabel(title))
            layout.addWidget(widget)
            panes.addWidget(pane)
        panes.setSizes([230, 620])
        layout = QVBoxLayout(self)
        layout.addWidget(panes)
        layout.addWidget(QLabel("默认显示大概值，* 表示 COUNT 的精确值"))
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addWidget(refresh)
        buttons.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        self._client.cancel_all()
        self.databases.clear()
        self.tables.setRowCount(0)
        self.status.setText("正在读取数据库…")
        self._client.get_json("databases", self._base_url)

    def _select_database(self, name):
        self.tables.setRowCount(0)
        if not name:
            return
        self.status.setText(f"正在读取 {name} 的表…")
        self._client.get_json("tables", self._base_url + "/tables?" + urlencode({"database_name": name}))

    def _loaded(self, operation, payload):
        try:
            if operation == "databases":
                self.databases.addItems(payload["databases"])
                if self.databases.count():
                    self.databases.setCurrentRow(0)
                else:
                    self.status.setText("没有检测到数据库。")
            elif payload["database_name"] == self.databases.currentItem().text():
                rows = payload["tables"]
                self.tables.setSortingEnabled(False)
                self.tables.setRowCount(len(rows))
                for index, row in enumerate(rows):
                    count = row.get("row_count", row["estimated_rows"])
                    approximate = row.get("row_count_is_estimate", True)
                    label = "未知" if count is None else (
                        f"{count:,}" if approximate else f"{count:,}*"
                    )
                    for column, value in enumerate((row["schema_name"], row["table_name"],
                                                     label)):
                        item = RowCountItem(value, count) if column == 2 else QTableWidgetItem(value)
                        if column == 2 and row.get("count_error"):
                            item.setToolTip(row["count_error"])
                        self.tables.setItem(index, column, item)
                self.tables.resizeColumnsToContents()
                self.tables.setSortingEnabled(True)
                self.status.setText(f"{payload['database_name']}：{len(rows)} 张表（不含系统表）。")
        except (KeyError, TypeError, AttributeError, ValueError):
            self.tables.setSortingEnabled(True)
            self._failed(operation, "后端返回的数据库目录格式无效。")

    def _failed(self, operation, message):
        self.status.setText(message)
        if self._error_callback:
            self._error_callback(message)
