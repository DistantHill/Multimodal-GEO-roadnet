"""Persistent settings and their small configuration dialog."""

from __future__ import annotations

from dataclasses import dataclass

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)
from qgis.core import QgsSettings

from .core import (
    ContractError,
    DEFAULT_BACKEND_URL,
    DEFAULT_CLIP_PATH,
    DEFAULT_REPLAY_PATH,
    normalize_backend_url,
    normalize_endpoint_path,
)


SETTINGS_PREFIX = "RoadnetTools"


@dataclass(frozen=True)
class PluginSettings:
    backend_url: str
    replay_path: str
    clip_path: str

    @classmethod
    def load(cls) -> "PluginSettings":
        settings = QgsSettings()
        return cls(
            backend_url=settings.value(
                f"{SETTINGS_PREFIX}/backendUrl", DEFAULT_BACKEND_URL, type=str
            ),
            replay_path=settings.value(
                f"{SETTINGS_PREFIX}/replayPath", DEFAULT_REPLAY_PATH, type=str
            ),
            clip_path=settings.value(
                f"{SETTINGS_PREFIX}/clipPath", DEFAULT_CLIP_PATH, type=str
            ),
        ).validated()

    def validated(self) -> "PluginSettings":
        return PluginSettings(
            normalize_backend_url(self.backend_url),
            normalize_endpoint_path(self.replay_path, "Replay endpoint"),
            normalize_endpoint_path(self.clip_path, "Clip endpoint"),
        )

    def save(self) -> None:
        valid = self.validated()
        settings = QgsSettings()
        settings.setValue(f"{SETTINGS_PREFIX}/backendUrl", valid.backend_url)
        settings.setValue(f"{SETTINGS_PREFIX}/replayPath", valid.replay_path)
        settings.setValue(f"{SETTINGS_PREFIX}/clipPath", valid.clip_path)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Roadnet Tools settings")
        self.setMinimumWidth(520)

        current = PluginSettings.load()
        self.backend_url = QLineEdit(current.backend_url)
        self.backend_url.setPlaceholderText(DEFAULT_BACKEND_URL)
        self.replay_path = QLineEdit(current.replay_path)
        self.clip_path = QLineEdit(current.clip_path)

        form = QFormLayout()
        form.addRow("Backend URL", self.backend_url)
        form.addRow("Replay path", self.replay_path)
        form.addRow("Clip path", self.clip_path)

        hint = QLabel(
            "Requests are sent asynchronously. Responses must be GeoJSON and are "
            "rendered only in an in-memory layer named tmp."
        )
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _save_and_accept(self) -> None:
        candidate = PluginSettings(
            self.backend_url.text(),
            self.replay_path.text(),
            self.clip_path.text(),
        )
        try:
            candidate.save()
        except ContractError as exc:
            QMessageBox.warning(self, "Invalid Roadnet Tools settings", str(exc))
            return
        self.accept()
