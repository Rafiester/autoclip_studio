# AutoClip Studio — Agent Handoff & Progress

**Last Updated:** 2026-11-26  
**Current Phase:** YouTube Most Replayed Analysis & Hybrid AI Highlight Detection Complete

## Execution Status

### Completed Tasks
- **Phase 1 — Logo and Application Identity** (`cd597dc`)
- **Phase 2 — SaaS UI/UX Optimization and Brand System** (`64b1e2d`)
- **Phase 3 — Performance Optimization** (`75506ca`)
- **Phase 4 — Automatic Clip Thumbnail Generation** (`202ddd0`)
- **Generated Clips SaaS Master-Detail Redesign** (`a2f2bc6`)
- **First-Time Setup Wizard & Dependency Manager** (`fed338b`)
- **Local Video Import & Optional External SRT Import** (`3121a88`)
- **YouTube Most Replayed Analysis & Hybrid AI Highlight Detection:**
  - **Audience Heatmap (`autoclip_studio/heatmap.py`, `autoclip_studio/youtube_metadata.py`):** Extended yt-dlp metadata extraction (`schema_version=2` in existing `youtube-metadata` cache, zero video download) to retrieve `heatmap`, `subtitles`, `automatic_captions`, and `language`. Implemented local normalization (`parse_heatmap`), peak detection, neighboring segment expansion, adjacent/overlapping region merging (`detect_highlight_regions`), and deterministic interval scoring (`region_replay_score`). Never fabricates replay data when unavailable.
  - **YouTube Subtitle Extraction (`autoclip_studio/youtube_subtitles.py`):** Added optional "Use YouTube subtitles when available" workflow supporting Indonesian, English, and other available languages; prefers manual subtitles in source/preferred language over automatic captions, strips WebVTT tags, deduplicates YouTube's rolling 2-line auto-captions without fabricating word-level timestamps or silently translating, validates coverage/synchronization, and enforces transcript priority: (1) User SRT → (2) YouTube subtitles/auto-captions → (3) local/cloud Speech-to-Text.
  - **Hybrid AI & Most Replayed Ranking (`autoclip_studio/highlights.py`, `autoclip_studio/pipeline.py`, `autoclip_studio/models.py`, `autoclip_studio/database.py`):** Added `Hybrid (AI + Most Replayed)`, `AI Analysis`, and `Most Replayed` modes with deterministic backend weighted ranking (default 60% AI / 40% Replay, user-adjustable), compact replay-region prompt summaries preserving full-video coverage so strong non-peak moments remain eligible, sentence-boundary alignment around replay peaks, automatic fallback to AI Analysis for local videos or videos without heatmap data, and SQLite persistence of `ai_score`, `replay_score`, `reason`, and `detection_mode`.
  - **UI Integration (`autoclip_studio/ui/heatmap_view.py`, `autoclip_studio/gui.py`, `autoclip_studio/ui/clips_workspace.py`):** Added compact interactive `AudienceHeatmapWidget` (hoverable replay intensity curve, peak markers, highlighted regions, availability & mode badges) beneath `YouTubePreview`, Highlight Detection mode selector, Hybrid AI/Replay weight control, YouTube subtitles checkbox/track summary, and Combined / AI / Replay score chips + "Why selected" rationale in Generated Clips details.
  - **Live YouTube & Unit Validation:** Verified real YouTube extraction (`dQw4w9WgXcQ`: 100 heatmap points + manual/auto subs; `jNQXAC9IVRw`: 100 heatmap points + 6-cue manual English subtitles extracted without Whisper; `00000000000`: clean unavailable error) and `tests/test_heatmap_and_hybrid.py` (5 new tests).
  - **Limitations:** Replay intensity is a relative YouTube popularity signal (not an exact replay count, retention percentage, or virality guarantee); YouTube may omit `heatmap` on low-view, very new, or live videos (automatically falls back to AI Analysis); YouTube VTT tracks provide cue-level rather than word-level timestamps.
- **Global UI/UX Refinement (UI/UX Pro Max):**
  - **Shared Layout & Style Architecture (`autoclip_studio/ui/style.py`):** Added reusable `configure_form_layout()` (`ExpandingFieldsGrow`, `AlignLeft | AlignTop`, `WrapLongRows`, consistent 18×12px spacing) and `configure_input()` (`Expanding` horizontal policy, `AdjustToMinimumContentsLengthWithIcon`, unelided dropdown popups) to eliminate macOS `QFormLayout` narrow/centered field collapse across the entire app while preserving Windows parity.
  - **Global Stylesheet Polish:** Added `QDoubleSpinBox` and `QDialog` dark-surface rules, unified input/button `min-height: 20px` alignment, styled `QComboBox::drop-down`, `QScrollBar:horizontal`, transparent `QGroupBox QStackedWidget`, and crisp dark-mode `QCheckBox::indicator:unchecked` borders.
  - **Platform-Aware Credential Storage Label (`autoclip_studio/config.py`, `autoclip_studio/gui.py`):** Added `credential_store_name()` backed by the active `keyring` backend (`macOS Keychain` on macOS, `Windows Credential Manager` on Windows).
  - **Page & Dialog Improvements:** Refined all 9 pages (`Dashboard`, `New Project`, `Processing Queue`, `Generated Clips`, `Caption Templates`, `AI Provider Settings`, `Speech-to-Text`, `Performance`, `Dependencies`) and dialogs (`ExportThumbnailDialog`, `FramePickerDialog`, `_LogViewerDialog`, `JobDetailsDialog`): paired page title/subtitle headers, equal-stretch Dashboard KPI cards & table columns, dynamic `source_stack` height on New Project, 2-row responsive toolbars on Dependencies and Generated Clips (preventing 45px input collapse at `760×560`), unclipped wrapped labels & escaped `&&` mnemonics on Performance (`Thermal protection & Gaming Mode`) and Processing Queue (`Activity & Logs`), non-clipping Caption Template card descriptions, and macOS `Menlo` / Windows `Consolas` badge fonts.
  - **Validated on macOS Retina (`1280×820` & `760×560`):** Visual screenshot inspection and automated geometry audit confirmed zero narrow form inputs (`805–967px` at `1280×820`, `262–563px` at `760×560`), zero clipped labels, and `macOS Keychain` subtitle rendering.
- **Native macOS Apple Silicon Support** (`8ab99b3`, validated on Apple Silicon in `90e4abf`)
- **Processing Queue SaaS Redesign & Scheduler:**
  - Replaced legacy spreadsheet table and console block on `_queue_page` with [queue_workspace.py](file:///c:/Users/Suzurui/Documents/Antigravity/OShorts/autoclip_studio/ui/queue_workspace.py):
    - `QueueOverviewBar`: 4 dynamic status KPI cards (`Processing`, `Queued`, `Completed`, `Failed`), live engine status + active performance profile badges, and `Pause Queue` / `Resume Queue` control.
    - `ActiveJobCard` + `PipelineStepperWidget`: compact idle state when idle, active job card with source thumbnail/icon, project name, source type & media specs, 6-stage dynamic horizontal stepper (YouTube vs Local Import, Transcription vs SRT Import, AI Highlights, Face Tracking, Captions, Export & Thumbs with `Skipped`/`Waiting`/`Failed`/`Completed`/`Running` states), preserved `DownloadView` and `SpeechProgressView` transfer/transcription cards, elapsed timer, and measurable ETA (`—` until meaningful progress exists).
    - `JobQueuePanel` + `JobRowCard`: scrollable modern cards with filter tabs (`All`, `Queued`, `Processing`, `Completed`, `Failed`, `Cancelled`), search & sort (`Queue Order`, `Newest First`, `Oldest First`, `Progress`, `Name`), inline redacted error summary + expandable full details, and contextual `⋯` menu (`View Details`, `Open Project Folder`, `View Generated Clips`, `Move Up`, `Move Down`, `Cancel Queued Job`, `Retry AI Analysis`, `Retry Failed Stage`, `Remove from History` preserving media files).
    - `CollapsibleLogsPanel` + bounded `LogView`: collapsed by default, 4000-entry capped ring buffer, severity filter (`All`, `INFO`, `SUCCESS`, `WARNING`, `ERROR`), search filter, `Auto-scroll` toggle, and `Copy Logs` / `Clear Logs` / `Export Logs` preserving credential redaction.
  - Added persisted `queue_order` and `stage` columns, `reorder_queued_project()`, `delete_project_history()`, and `next_queued_project()` to `autoclip_studio/database.py`.
  - Updated `autoclip_studio/gui.py` so `New Project` queues jobs sequentially when busy or paused without blocking and dispatches queued jobs automatically when idle and unpaused.
  - **Phase 1 (Cross-Platform Architecture):** Added `autoclip_studio/platform_utils.py` (`detect_platform`, `subprocess_creation_flags`, `venv_python_path`, `uv_executable_path`, `marker_matches`); updated `paths.py` (macOS `/opt/homebrew/bin` & `/usr/local/bin` tool lookup), `process.py` (POSIX process-group session cleanup), and `captions.py` (macOS system font scan & font mapping).
  - **Phase 2 (Native macOS Launcher):** Added `packaging/make_icns.py` (generates multi-res `autoclip_studio/assets/autoclip.icns`), `packaging/macos/Launcher.m` (native Objective-C Cocoa arm64 `.app` launcher + First-Time Setup UI with zero Terminal/Rosetta), and `packaging/build_macos.py` (builds `AutoClipStudio.app` + optional `AutoClipStudio.dmg`).
  - **Phase 3 (Platform Dependency Manager):** Updated `scripts/toolchain.json` (`uv_macos_arm64`), `requirements.txt` (`mlx-whisper` marker for `darwin` + `arm64`), `scripts/dependencies.json` (`platforms` filtering), `scripts/setup.py` (macOS `uv` `.tar.gz` extraction, Homebrew/system FFmpeg, marker evaluation), and `autoclip_studio/dependencies.py` (never probes/downloads Windows `.exe` or CUDA libs on macOS).
  - **Phase 4 (Apple Silicon Hardware Acceleration):** Added `mlx_whisper_available()` in `speech_engine.py`, `_run_mlx_whisper()` with `mx.metal.clear_cache()` cleanup and CPU Faster-Whisper fallback in `transcription.py`, and `videotoolbox_available()` / `detect_hw_encoder()` (`h264_videotoolbox -q:v 65` + CPU fallback) in `rendering.py`.
  - **Phase 5 (Apple Silicon Performance Profiles):** Updated `performance.py` (`unified_memory` & `gpu_backend` on `HardwareSnapshot`, Apple Silicon Eco/Balanced/Turbo allocation, NVML skipped on macOS, `N/A` for unavailable sensors) and `performance_view.py`.
  - **Phase 6 (Native macOS UI Compatibility):** Updated `style.py` (`SF Pro Text` / `Helvetica Neue` + `Menlo` monospace on macOS), `gui.py` (`autoclip.icns` icon on macOS), and `speech_view.py` (macOS MLX option & cross-platform file picker filters).
  - **Phase 7 (Tests & Build Staging):** Added `tests/test_cross_platform.py` (6 tests), generated `autoclip_studio/assets/autoclip.icns`, and staged `build/macos_stage/AutoClipStudio.app` (`Info.plist`, resources, icon).

### Completed Native macOS Apple Silicon Validation (`macOS 27.2.1`, `arm64`)
- **Host & Toolchain Verified:** Native `arm64` (`macOS 27.2.1` Build `26B131`, `Apple clang 21.0.0`, `hdiutil`, `sips`, `iconutil`, Homebrew `/opt/homebrew`).
- **Native Runtime & Dependencies:** Provisioned `.venv` (`CPython 3.12.16 aarch64` via `tools/uv 0.13.10`), all 59 native Apple Silicon wheels (`PySide6==6.8.3`, `mlx==0.32.5`, `mlx-metal==0.32.5`, `mlx-whisper==0.4.2`, `faster-whisper==1.2.1`, `opencv-python-headless==4.10.0.84`, `av==15.1.0`, `yt-dlp==2025.11.12`, `psutil==7.2.2`), and Homebrew `ffmpeg-full 9.1` (`arm64_golden_gate`, with both `h264_videotoolbox` and `libass` `subtitles`/`ass` filters).
- **Native `.app` & DMG Built and Verified:**
  - `python3 packaging/build_macos.py --dmg` compiled `dist/AutoClipStudio.app` (`976 KB`, non-fat `Mach-O 64-bit executable arm64`, `Info.plist` valid) and `dist/AutoClipStudio.dmg` (`1.4 MB`, `hdiutil verify` checksum `VALID`).
  - Lightweight launcher architecture preserved: ordinary Python source edits do not require rebuilding `AutoClipStudio.app`.
  - LaunchServices / Finder / Spotlight check (`mdls`, `open dist/AutoClipStudio.app`): bundle ID `com.autoclip.studio`, launches `.venv/bin/python3 main.py` cleanly with zero visible Terminal windows and clean shutdown.
- **Real Apple Silicon Pipeline Verified:**
  - **MLX Whisper Local Transcription:** Verified `mlx-whisper 0.4.2` (`mlx-community/whisper-tiny-mlx`) on Apple Silicon Metal with word-level timestamps (`Hello from AutoClip Studio on Apple Silicon.`, 7 words) and `mx.clear_cache()` cleanup.
  - **FFmpeg `h264_videotoolbox` + Captions + Face Tracking + Thumbnails:** Verified `detect_hw_encoder() == "videotoolbox"`, local video + Indonesian SRT import, `Hormozi` ASS caption burn-in via `libass`, OpenCV Haar face tracking (8–9 sample points), and `tests.integration_smoke` (`948,804 bytes`, `6.30s`) with zero CPU fallback.
  - **GUI, Processing Queue, Performance Profiles & Video Playback:** Verified all 9 pages (including redesigned Processing Queue, Generated Clips + `QMediaPlayer` FFmpeg 7.1 backend, Dependency Manager, Speech-to-Text, and Performance View with Eco/Balanced/Turbo scaling on Apple Silicon Unified Memory).

### macOS Issues Resolved During Validation
1. **Homebrew `ffmpeg` lacks `libass` (`subtitles` filter):** Updated `autoclip_studio/paths.py` and `scripts/setup.py` to prefer `/opt/homebrew/opt/ffmpeg-full/bin` (and `/usr/local/opt/ffmpeg-full/bin`), verify the `subtitles` filter on macOS, and install `ffmpeg-full` via Homebrew.
2. **System `python3` build compatibility:** Updated `packaging/build_macos.py` to reuse existing `autoclip_studio/assets/autoclip.icns` when invoked with system `python3` lacking `PySide6`.
3. **Native Cocoa Launcher (`packaging/macos/Launcher.m`):** Fixed `launchButton` right-edge clipping (`560px` window width), prepended Homebrew `ffmpeg-full`/Homebrew bin paths to `PATH` for Finder/Spotlight launches, and redirected setup output to `~/Library/Application Support/AutoClip Studio/logs/launcher.log`.
4. **POSIX `.venv` site-packages & macOS UI labels:** Fixed `_venv_site_packages()` and `caption_fonts` path in `autoclip_studio/dependencies.py`, updated hardware encoder checkbox label and `setApplicationDisplayName` in `autoclip_studio/gui.py`, updated macOS error hint in `autoclip_studio/process.py`, and replaced deprecated `mx.metal.clear_cache()` with `mx.clear_cache()` in `autoclip_studio/transcription.py`.
5. **Cross-platform unit tests:** Updated `tests/test_dependencies.py`, `tests/test_pipeline_regressions.py`, and `tests/test_transcription_engines.py` so the suite runs cleanly on both Windows and macOS.

### Remaining Limitations
- `dist/AutoClipStudio.app` and `dist/AutoClipStudio.dmg` are unsigned/ad-hoc for local use; public distribution outside the local machine requires an Apple Developer ID certificate and notarization (`codesign` + `notarytool`).
- Because the lightweight launcher spawns `.venv/bin/python3 main.py` directly (preserving zero-rebuild source updates), macOS System Events lists the child process as `python3` while Qt sets the window title, application display name, and Dock icon to AutoClip Studio.

## Modified Files
- `autoclip_studio/platform_utils.py`
- `autoclip_studio/paths.py`
- `autoclip_studio/process.py`
- `autoclip_studio/captions.py`
- `autoclip_studio/dependencies.py`
- `autoclip_studio/speech_engine.py`
- `autoclip_studio/transcription.py`
- `autoclip_studio/rendering.py`
- `autoclip_studio/performance.py`
- `autoclip_studio/ui/performance_view.py`
- `autoclip_studio/ui/speech_view.py`
- `autoclip_studio/ui/style.py`
- `autoclip_studio/gui.py`
- `autoclip_studio/assets/autoclip.icns`
- `packaging/make_icns.py`
- `packaging/macos/Launcher.m`
- `packaging/build_macos.py`
- `scripts/toolchain.json`
- `scripts/dependencies.json`
- `scripts/setup.py`
- `requirements.txt`
- `autoclip_studio/database.py`
- `autoclip_studio/gui.py`
- `autoclip_studio/ui/icons.py`
- `autoclip_studio/ui/log_view.py`
- `autoclip_studio/ui/queue_workspace.py`
- `autoclip_studio/ui/style.py`
- `tests/test_log_panel.py`
- `tests/test_queue_workspace.py`
- `HANDOFF.md`

## Test Results
- **macOS 27.2.1 (`arm64`, Apple Silicon):**
  - `.venv/bin/python3 -m pytest -q`: **95 passed, 3 skipped, 0 failed** (includes `tests/test_heatmap_and_hybrid.py`; 3 Windows-only/environment skips).
  - `.venv/bin/python3 -m tests.integration_smoke`: **Passed** (`h264_videotoolbox`, 9 face-tracking sample points, `948,804 bytes`, `6.30s`).
  - `.venv/bin/python3 scripts/setup.py --check`: **Passed** (exit code 0).
- **Windows 11 x64 Host (previous session):**
  - `.\.venv\Scripts\python.exe -m pytest -q`: **93 passed**.
