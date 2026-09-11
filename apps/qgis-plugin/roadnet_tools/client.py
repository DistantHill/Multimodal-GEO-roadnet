"""Asynchronous HTTP client backed by QGIS' shared network manager."""

from __future__ import annotations

import json
import uuid
from typing import Any

from qgis.PyQt.QtCore import QByteArray, QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import QgsNetworkAccessManager


class BackendClient(QObject):
    request_started = pyqtSignal(str)
    request_succeeded = pyqtSignal(str, object)
    request_failed = pyqtSignal(str, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._manager = QgsNetworkAccessManager.instance()
        self._replies: set[Any] = set()
        self._latest_request: dict[str, str] = {}

    def post_json(self, operation: str, url: str, payload: dict[str, Any]) -> None:
        """Start a non-blocking POST; only the latest result per operation is emitted."""

        request_id = uuid.uuid4().hex
        self._latest_request[operation] = request_id

        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(QByteArray(b"Content-Type"), QByteArray(b"application/json"))
        request.setRawHeader(
            QByteArray(b"Accept"),
            QByteArray(b"application/geo+json, application/json"),
        )
        body = QByteArray(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        reply = self._manager.post(request, body)
        self._replies.add(reply)
        reply.finished.connect(
            lambda current=reply: self._finish(operation, request_id, current)
        )
        self.request_started.emit(operation)

    def cancel_all(self) -> None:
        for reply in tuple(self._replies):
            reply.abort()

    def _finish(self, operation: str, request_id: str, reply: Any) -> None:
        self._replies.discard(reply)
        is_latest = self._latest_request.get(operation) == request_id
        raw = bytes(reply.readAll()).decode("utf-8", errors="replace")
        status = reply.attribute(_http_status_attribute())
        network_error = _enum_number(reply.error())
        error_text = reply.errorString()
        reply.deleteLater()

        if not is_latest:
            return
        if network_error != 0 or not status or not 200 <= int(status) < 300:
            self.request_failed.emit(
                operation, _response_error(status, error_text, raw)
            )
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.request_failed.emit(
                operation, f"Backend returned invalid JSON: {exc.msg}."
            )
            return
        self.request_succeeded.emit(operation, payload)


def _http_status_attribute():
    direct = getattr(QNetworkRequest, "HttpStatusCodeAttribute", None)
    if direct is not None:
        return direct
    return QNetworkRequest.Attribute.HttpStatusCodeAttribute


def _enum_number(value: Any) -> int:
    return int(getattr(value, "value", value))


def _response_error(status: Any, network_error: str, body: str) -> str:
    detail = ""
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            raw_detail = payload.get("detail", payload.get("message", ""))
            if isinstance(raw_detail, dict):
                detail = str(raw_detail.get("message", raw_detail.get("error", raw_detail)))
            elif isinstance(raw_detail, list):
                detail = "; ".join(
                    str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                    for item in raw_detail
                )
            elif raw_detail:
                detail = str(raw_detail)
    except (json.JSONDecodeError, TypeError):
        detail = body.strip()[:300]

    prefix = f"HTTP {status}" if status else "Network request failed"
    explanation = detail or network_error
    return f"{prefix}: {explanation}" if explanation else prefix
