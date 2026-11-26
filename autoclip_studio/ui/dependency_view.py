from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ..dependencies import (
    CATEGORY_LABELS, CATEGORY_ORDER, DependencyStatus, check_yt_dlp_update,
    detect_package_manager, download_whisper_model_checked, format_bytes,
    inspect_all_dependencies, inspect_whisper_models, install_yunet_asd_model,
    remove_owned_whisper_model, repair_component, update_yt_dlp,
)
from ..paths import APP_ROOT, LOG_DIR
from ..speech_engine import SpeechSettings
from ..worker import FunctionThread
from .style import configure_input


class _LogViewerDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Installation & Launcher Logs")
        self.resize(740, 460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        chunks: list[str] = []
        for path in (LOG_DIR / "launcher.log", LOG_DIR / "autoclip.log"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()[-150:]
                chunks.append(f"=== {path.name} ===\n" + "\n".join(lines))
        viewer.setPlainText("\n\n".join(chunks) or "No installation logs recorded yet.")
        layout.addWidget(viewer, 1)
        row = QHBoxLayout()
        copy_btn = QPushButton("Copy Logs")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(viewer.toPlainText()))
        close_btn = QPushButton("Close", objectName="primary")
        close_btn.clicked.connect(self.accept)
        row.addWidget(copy_btn)
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)


class DependencyManagerView(QWidget):
    """In-app Dependency Manager, yt-dlp Updater & Whisper Model Manager page."""

    def __init__(
        self,
        get_speech_settings: Callable[[], SpeechSettings],
        parent: QWidget | None = None,
        *,
        is_job_active: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._get_speech = get_speech_settings
        self._is_job_active = is_job_active or (lambda: False)
        self._threads: list[FunctionThread] = []
        self._statuses: list[DependencyStatus] = []
        self._model_rows: list[dict] = []
        self._available_versions: dict[str, str] = {}
        self._action_buttons: list[QPushButton] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        # Toolbar card
        bar = QFrame(objectName="card")
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(16, 12, 16, 12)
        bar_layout.setSpacing(8)
        primary_row = QHBoxLayout()
        primary_row.setSpacing(8)
        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(8)

        self.repair_btn = QPushButton("Repair Missing Components", objectName="primary")
        self.repair_btn.clicked.connect(self.repair_missing)
        self.check_btn = QPushButton("Check Dependencies")
        self.check_btn.clicked.connect(self.refresh_async)
        self.update_btn = QPushButton("Check yt-dlp Updates")
        self.update_btn.clicked.connect(self.check_updates_async)
        self.open_folder_btn = QPushButton("Open Install Folder")
        self.open_folder_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_ROOT))))
        self.logs_btn = QPushButton("View Install Logs")
        self.logs_btn.clicked.connect(lambda: _LogViewerDialog(self).exec())

        for btn in (self.repair_btn, self.check_btn, self.update_btn):
            primary_row.addWidget(btn)
        primary_row.addStretch()

        self.pm_badge = QLabel("", objectName="muted")
        for btn in (self.open_folder_btn, self.logs_btn):
            secondary_row.addWidget(btn)
        secondary_row.addStretch()
        secondary_row.addWidget(self.pm_badge)

        bar_layout.addLayout(primary_row)
        bar_layout.addLayout(secondary_row)
        root.addWidget(bar)

        self.status_label = QLabel("Ready", objectName="muted")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # Components list inside scroll area
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(8)
        self.scroll.setWidget(self._list_host)
        root.addWidget(self.scroll, 1)

        # Optional Whisper models card
        models_card = QFrame(objectName="card")
        ml = QVBoxLayout(models_card)
        ml.setContentsMargins(16, 14, 16, 14)
        ml.setSpacing(10)
        ml.addWidget(QLabel("Whisper Transcription Models (Optional — Local STT)", objectName="sectionTitle"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.model_combo = configure_input(QComboBox(), min_contents_length=24)
        self.model_combo.currentIndexChanged.connect(self._update_model_buttons)
        self.download_model_btn = QPushButton("Download Model", objectName="primary")
        self.download_model_btn.clicked.connect(self.download_selected_model)
        self.remove_model_btn = QPushButton("Remove Unused Model", objectName="danger")
        self.remove_model_btn.clicked.connect(self.remove_selected_model)
        row.addWidget(self.model_combo, 1)
        row.addWidget(self.download_model_btn)
        row.addWidget(self.remove_model_btn)
        ml.addLayout(row)
        self.model_detail = QLabel("", objectName="muted")
        self.model_detail.setWordWrap(True)
        ml.addWidget(self.model_detail)
        root.addWidget(models_card)

        self.refresh_sync()

    def _is_cloud_only(self, speech: SpeechSettings) -> bool:
        engine = str(getattr(speech, "engine", "") or "").lower()
        return engine in {"cloud", "openai", "gemini", "none"}

    def refresh_sync(self) -> None:
        speech: SpeechSettings = self._get_speech()
        pm = detect_package_manager()
        self.pm_badge.setText(f"Package manager: {pm.summary}")
        self._statuses = inspect_all_dependencies(
            speech_settings=speech,
            cloud_only=self._is_cloud_only(speech),
            available_versions=self._available_versions,
        )
        self._model_rows = inspect_whisper_models(speech)
        self._render_components()
        self._render_models()

    def refresh_async(self) -> None:
        self._set_busy(True, "Checking dependencies…")
        speech: SpeechSettings = self._get_speech()
        cloud_only = self._is_cloud_only(speech)

        def work() -> tuple[list[DependencyStatus], list[dict], str]:
            pm = detect_package_manager()
            statuses = inspect_all_dependencies(
                speech_settings=speech,
                cloud_only=cloud_only,
                available_versions=self._available_versions,
            )
            return statuses, inspect_whisper_models(speech), pm.summary

        def done(result: object) -> None:
            statuses, models, pm_summary = result  # type: ignore[misc]
            self.pm_badge.setText(f"Package manager: {pm_summary}")
            self._statuses = statuses
            self._model_rows = models
            self._render_components()
            self._render_models()
            missing = sum(1 for s in statuses if s.spec.mandatory and s.state in {"Missing", "Corrupted"})
            self._set_busy(
                False,
                "All required dependencies verified."
                if missing == 0
                else f"{missing} required component(s) need repair.",
            )

        self._run_bg(work, done, "Dependency check")

    def check_updates_async(self) -> None:
        self._set_busy(True, "Checking official PyPI releases for yt-dlp…")

        def work() -> dict:
            return check_yt_dlp_update()

        def done(result: object) -> None:
            info = result  # type: ignore[assignment]
            if info.get("error"):
                self._set_busy(False, f"Could not check yt-dlp updates: {info['error']}")
                return
            latest = str(info.get("latest") or "")
            if latest:
                self._available_versions["yt_dlp"] = latest
            self.refresh_sync()
            if info.get("update_available"):
                self._set_busy(
                    False,
                    f"yt-dlp update available: {info['installed']} → {latest}. Click 'Update yt-dlp' on the YouTube Downloader card.",
                )
            else:
                self._set_busy(False, f"yt-dlp {info['installed']} is already up to date (latest stable: {latest}).")

        self._run_bg(work, done, "Check yt-dlp updates")

    def update_yt_dlp_async(self) -> None:
        if self._is_job_active():
            QMessageBox.information(
                self, "Job in progress",
                "Wait for the active processing job to finish (or cancel it) before updating yt-dlp.",
            )
            return
        self._set_busy(True, "Updating yt-dlp to latest stable release and verifying compatibility…")

        def work() -> dict:
            return update_yt_dlp(is_job_active=self._is_job_active)

        def done(result: object) -> None:
            info = result  # type: ignore[assignment]
            new_ver = info["installed_version"]
            self._available_versions["yt_dlp"] = new_ver
            self.refresh_sync()
            prev = info.get("previous_version")
            if prev and prev != new_ver:
                self._set_busy(False, f"Updated yt-dlp {prev} → {new_ver}. Version persisted across restarts.")
            else:
                self._set_busy(False, f"yt-dlp {new_ver} is installed and verified.")

        self._run_bg(work, done, "yt-dlp update")

    def repair_missing(self) -> None:
        self._run_setup_script("Repairing missing required components (preserving existing versions)…", ["--repair-only", "--no-tests"])

    def update_components(self) -> None:
        self.check_updates_async()

    def _run_component_action(self, comp_id: str, label: str) -> None:
        if comp_id == "yt_dlp":
            self.update_yt_dlp_async()
            return
        if self._is_job_active():
            QMessageBox.information(self, "Job in progress", "Wait for active processing jobs to finish first.")
            return
        speech = self._get_speech()
        cloud_only = self._is_cloud_only(speech)
        self._set_busy(True, f"{label}…")

        def work() -> str:
            return repair_component(comp_id, is_job_active=self._is_job_active, cloud_only=cloud_only)

        def done(msg: object) -> None:
            self.refresh_sync()
            self._set_busy(False, str(msg))

        self._run_bg(work, done, label)

    def _run_setup_script(self, label: str, extra_args: list[str]) -> None:
        setup_py = APP_ROOT / "scripts" / "setup.py"
        if not setup_py.is_file():
            QMessageBox.warning(self, "Unavailable", "scripts/setup.py was not found.")
            return
        if self._is_job_active():
            QMessageBox.information(self, "Job in progress", "Wait for active processing jobs to finish before repairing dependencies.")
            return
        self._set_busy(True, label)

        def work() -> str:
            res = subprocess.run(
                [sys.executable, str(setup_py), *extra_args],
                cwd=APP_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            if res.returncode != 0:
                raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f"Exited with code {res.returncode}")
            return "Required dependencies verified and repaired."

        def done(msg: object) -> None:
            self.refresh_sync()
            self._set_busy(False, str(msg))

        self._run_bg(work, done, "Dependency repair")

    def download_selected_model(self) -> None:
        idx = self.model_combo.currentIndex()
        if idx < 0 or idx >= len(self._model_rows):
            return
        model_id = self._model_rows[idx]["id"]
        self._set_busy(True, f"Downloading Whisper model '{model_id}'…")
        speech = self._get_speech()

        def work() -> str:
            path = download_whisper_model_checked(model_id, speech)
            return f"Whisper model '{model_id}' ready at {path}"

        def done(msg: object) -> None:
            self.refresh_sync()
            self._set_busy(False, str(msg))

        self._run_bg(work, done, f"Download {model_id}")

    def remove_selected_model(self) -> None:
        idx = self.model_combo.currentIndex()
        if idx < 0 or idx >= len(self._model_rows):
            return
        row = self._model_rows[idx]
        if not row["owned"]:
            QMessageBox.information(
                self, "External model protected",
                "This model is outside AutoClip Studio's cache (e.g., Subtitle Edit) and will not be modified.",
            )
            return
        if QMessageBox.question(
            self, "Remove unused model",
            f"Remove cached Whisper model '{row['id']}' ({format_bytes(row['size_bytes'])})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        removed = remove_owned_whisper_model(row["id"])
        self.refresh_sync()
        self.status_label.setText(f"Removed model '{row['id']}'." if removed else "Model files already absent.")

    def _render_components(self) -> None:
        self._action_buttons.clear()
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                w = item.widget()
                w.hide()
                w.setParent(None)
                w.deleteLater()

        grouped: dict[str, list[DependencyStatus]] = {}
        for st in self._statuses:
            cat = st.spec.category if st.spec.category in CATEGORY_ORDER else "core_runtime"
            grouped.setdefault(cat, []).append(st)

        for cat in CATEGORY_ORDER:
            items = grouped.get(cat)
            if not items:
                continue
            header = QLabel(CATEGORY_LABELS.get(cat, cat), objectName="sectionTitle")
            header.setStyleSheet("padding-top: 6px; padding-bottom: 2px;")
            self._list_layout.addWidget(header)

            for st in items:
                card = QFrame(objectName="card")
                lay = QHBoxLayout(card)
                lay.setContentsMargins(14, 10, 14, 10)
                lay.setSpacing(12)

                left = QVBoxLayout()
                left.setSpacing(3)
                kind = "Required" if st.spec.mandatory else "Optional"
                title = QLabel(f"{st.spec.name}  ({kind})")
                title.setStyleSheet("font-weight: 600;")
                title.setWordWrap(True)
                title.setMinimumWidth(0)
                title.setToolTip(st.resolved_path or st.spec.install_path)

                meta_parts = [
                    f"Installed: {st.installed_version}",
                    f"Policy: {st.spec.version_policy} ({st.spec.version})",
                    f"Compatibility: {st.compatibility}",
                ]
                if st.available_version:
                    meta_parts.append(f"Available: {st.available_version}")
                if st.storage_bytes:
                    meta_parts.append(f"Size: {format_bytes(st.storage_bytes)}")
                sub = QLabel("  •  ".join(meta_parts), objectName="muted")
                sub.setWordWrap(True)
                sub.setMinimumWidth(0)

                detail_text = st.detail + (f"\nError: {st.error_detail}" if st.error_detail else "")
                detail = QLabel(detail_text, objectName="muted")
                detail.setWordWrap(True)
                detail.setMinimumWidth(0)
                left.addWidget(title)
                left.addWidget(sub)
                left.addWidget(detail)

                right = QHBoxLayout()
                right.setSpacing(8)
                badge = QLabel(st.state, objectName="statusChip")
                if st.state in {"Ready", "Reused existing"}:
                    badge.setProperty("tone", "success")
                elif st.state in {"Missing", "Corrupted"}:
                    badge.setProperty("tone", "danger")
                else:
                    badge.setProperty("tone", "")
                if st.update_available:
                    badge.setText(f"{st.state} · Update")
                right.addWidget(badge, alignment=Qt.AlignmentFlag.AlignVCenter)

                if st.spec.id == "yt_dlp":
                    btn_label = "Update yt-dlp" if st.update_available else "Reinstall / Update"
                    act = QPushButton(btn_label, objectName="primary" if st.update_available else "")
                    act.clicked.connect(self.update_yt_dlp_async)
                    self._action_buttons.append(act)
                    right.addWidget(act, alignment=Qt.AlignmentFlag.AlignVCenter)
                elif st.can_install and st.spec.id == "yunet_asd_model":
                    act = QPushButton("Install (227 KB)")
                    act.clicked.connect(lambda _c=False, cid=st.spec.id: self._run_component_action(cid, "Installing YuNet ASD model"))
                    self._action_buttons.append(act)
                    right.addWidget(act, alignment=Qt.AlignmentFlag.AlignVCenter)
                elif st.can_repair:
                    act = QPushButton("Repair", objectName="primary")
                    act.clicked.connect(lambda _c=False, cid=st.spec.id, nm=st.spec.name: self._run_component_action(cid, f"Repairing {nm}"))
                    self._action_buttons.append(act)
                    right.addWidget(act, alignment=Qt.AlignmentFlag.AlignVCenter)

                lay.addLayout(left, 1)
                lay.addLayout(right)
                self._list_layout.addWidget(card)
        self._list_layout.addStretch()

    def refresh_theme(self) -> None:
        self._render_components()

    def _render_models(self) -> None:
        current = self.model_combo.currentData()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for row in self._model_rows:
            status = "Installed" if row["installed"] else "Not downloaded"
            self.model_combo.addItem(f"{row['id']} — {status} ({format_bytes(row['size_bytes'])})", row["id"])
        if current:
            idx = self.model_combo.findData(current)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
        self.model_combo.blockSignals(False)
        self._update_model_buttons()

    def _update_model_buttons(self) -> None:
        idx = self.model_combo.currentIndex()
        if idx < 0 or idx >= len(self._model_rows):
            self.download_model_btn.setEnabled(False)
            self.remove_model_btn.setEnabled(False)
            self.model_detail.setText("")
            return
        row = self._model_rows[idx]
        self.download_model_btn.setEnabled(not row["installed"])
        self.remove_model_btn.setEnabled(bool(row["installed"] and row["owned"]))
        self.model_detail.setText(f"Source: {row['source']}  •  Path: {row['path']}")

    def _set_busy(self, busy: bool, message: str) -> None:
        self.status_label.setText(message)
        for btn in (self.check_btn, self.repair_btn, self.update_btn, self.download_model_btn, self.remove_model_btn, *self._action_buttons):
            btn.setEnabled(not busy)
        if not busy:
            self._update_model_buttons()

    def _run_bg(self, fn: Callable[[], object], on_ok: Callable[[object], None], operation: str) -> None:
        thread = FunctionThread(fn, self, operation=operation)
        self._threads.append(thread)
        thread.succeeded.connect(on_ok)
        thread.failed.connect(lambda err: self._set_busy(False, f"{operation} failed: {err}"))
        thread.finished.connect(lambda t=thread: self._threads.remove(t) if t in self._threads else None)
        thread.start()
