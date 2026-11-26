# AutoClip Studio — Agent Handoff & Progress

**Last Updated:** 2026-11-26  
**Current Phase:** Intelligent Auto Director & Cinematic Smart Reframing Complete

## Execution Status

### Completed Tasks
- **Intelligent Auto Director & Cinematic Smart Reframing:**
  - **Root Cause Audit (`02-asal-usul-nama-gia-pratama-dan-tirta.mp4`, `1864.50s–1898.80s`, `34.30s`, `1920×1080 @ 25 FPS` VP9 → `1080×1920 @ 25 FPS` H.264):**
    - Confirmed the original 3-person podcast footage (`Pertemuan Bersejarah dr. Tirta dan dr. Gia`, Raditya Dika) contains **17 real studio camera cuts**, and FFmpeg rendering itself had `0` dropped/duplicated frames (`exact 40ms` PTS intervals).
    - Identified 5 AutoClip Studio root causes behind the perceived abruptness:
      1. **160–200ms Delayed Cut Snap-Back (13 double-cuts):** 5 FPS scene-cut sampling without native-FPS refinement plus missing `setpts=PTS-STARTPTS` before `crop=` caused 3–5 frame delays after every source cut (`10.46s`, `15.98s`, `16.98s`, `21.06s`, `28.50s`, `30.98s`), slicing the new speaker's face before snapping.
      2. **Frame-0 Misframing Flash (`00:00–00:01`):** First keyframe began at `t=0.02s` rather than `0.000s`.
      3. **Haar False-Positive "Faces" on Dark Backpacks & Hands (`00:21–00:24`, `00:28–00:32`):** Bottom-edge dark fabric (`y > 82%`) and hand/chest detections below real faces spawned bogus tracks (e.g., at `28.20s`, a black backpack scored `0.51 > 0.17`, causing a 292px jump cut off dr. Gia onto the backpack).
      4. **Tripod Wobble / Micro-Panning (`00:00–00:10`, `00:22–00:24`):** A tiny `4.5%` (`27px`) dead zone chased minor conversational sway on locked-off close-up shots (`190–370px` drift).
      5. **Missing Shot-Level Lookahead & Offline DP Direction (`00:31–00:32`, multi-person wide shots):** Leading/mid-shot dropouts (dr. Tirta looking down at `31.0s–31.5s`) fell back to jumpy edge saliency instead of backfilling from the same shot, and multi-speaker switching lacked Viterbi/DP lookahead, turn-boundary alignment, and A-B-A suppression.
  - **Modular Auto Director Architecture (`autoclip_studio/auto_director.py`):**
    - Strictly separated camera direction and kinematic trajectory generation from detection/tracking (`smart_reframe.py`) and rendering (`rendering.py`).
    - Implemented 3 tunable styles (`REFRAME_STYLES`): **Natural** (default: `min_shot=2.4s`, `confirm=0.55s`, `cooldown=1.25s`, `lookahead=1.5s`, `dropout=0.55s`, `dead_zone=14%`), **Stable** (`min_shot=3.2s`, `confirm=0.70s`, `cooldown=1.60s`, `lookahead=2.0s`, `dead_zone=18%`), and **Dynamic** (`min_shot=1.6s`, `confirm=0.40s`, `cooldown=0.90s`, `lookahead=1.1s`, `dead_zone=10%`).
    - Implemented per-shot offline **Viterbi / Dynamic Programming** speaker state optimizer with causal+lookahead temporal evidence, reaction-shot damping during low speech VAD, conversational turn-boundary alignment, late-shot switch suppression, and post-DP rapid A-B-A ping-pong cleanup (while allowing genuine solo short responses and original camera cuts to override fixed duration thresholds).
    - Implemented **Stable Composition & Kinematic Trajectory Generator**: lookahead dropout backfill, locked-off tripod detection when a subject stays safely inside dead-zone & edge-safety margins, velocity- & acceleration-bounded eased pans for same-subject movement, and clean instant cuts aligned to mid-frame (`t_cut - 0.5 / source_fps`) for different subjects and original camera cuts.
  - **Detection Hardening, Frame-Accurate Cut Refinement & Rendering Sync (`autoclip_studio/smart_reframe.py`, `autoclip_studio/rendering.py`):**
    - Added `_filter_false_faces()` suppressing dark low-texture bottom-edge fabric and torso-column hand/shirt false positives while preserving existing OpenCV YuNet and Haar backends (zero extra downloads or paid API calls).
    - Added `_refine_exact_cut_frame()` pinpointing exact native-FPS cut frames (`<15ms` overhead) synchronized to `clip_start_frame = ceil(start * fps)` and prepended `setpts=PTS-STARTPTS` before `crop=` in `_build_filter_args` with millisecond `:.3f` expression formatting.
    - Bumped tracking cache schema to `reframe-v2` (includes `reframe_style`).
  - **Compact UI & Persistence (`autoclip_studio/models.py`, `autoclip_studio/config.py`, `autoclip_studio/pipeline.py`, `autoclip_studio/gui.py`):** Added compact **Reframing style** selector (`Natural`, `Stable`, `Dynamic`) under `Automation` in New Project, persisted via `SettingsStore` and auto-disabled when reframing mode is `Off` or `Fit Original`.
  - **Reference Clip Before vs. After Results (`02-asal-usul-nama-gia-pratama-dan-tirta.mp4`, Apple M1 `8 GB`, VideoToolbox `h264_videotoolbox`):**
    - **Visual cuts in rendered MP4:** `30` (BEFORE) → **`17`** (AFTER — exact 1:1 match to the 17 original source camera cuts).
    - **Double-cuts within 350ms (cut-lag snap-backs):** `13` (BEFORE) → **`0`** (AFTER — 100% eliminated across all 5 target windows `00:00–00:01`, `00:10–00:14`, `00:16–00:17`, `00:21–00:24`, `00:28–00:32`).
    - **Artificial speaker switches:** `3` (including the 292px jump onto dr. Gia's backpack at `28.20s`) → **`0` (`0.0/min`)** in `Natural` & `Stable` (`1` in `Dynamic`).
    - **Face visibility:** **`92.5%`**, **crop jitter:** **`34.7 px/s`** (`33.5 px/s` Stable, `38.7 px/s` Dynamic), **zoom jitter:** **`0`** (constant `606×1080` 9:16 full-height scale), **black frames:** **`0`**, **PTS integrity:** `858` frames at exact `25.000 FPS` (`0` non-40ms gaps, `first_pts=0.000s`, `v_dur=34.32s`, `a_dur=34.30s`).
    - **Performance:** **`16.52s`** full end-to-end render (`11.11s` analysis + `5.41s` VideoToolbox encode), **`19.1 MiB`** peak Python traced memory (`169 MiB` RSS).
    - **Remaining Limitations:** Without the optional YuNet ONNX model installed, OpenCV Haar can still miss heavily downward-tilted or extreme-profile faces briefly (mitigated by offline shot lookahead & dropout backfill); visual-only lip-motion ASD on simultaneous cross-talk in wide 2–3 person shots relies on relative mouth energy plus VAD and cannot perform audio beamforming or neural speaker diarization without external models.
- **Processing Queue & Generated Clips UI/UX Refinement, Native Video Player & Apple Silicon Transcription Telemetry:**
  - **Processing Queue Responsive Layout & Job Cards (`autoclip_studio/gui.py`, `autoclip_studio/ui/queue_workspace.py`, `autoclip_studio/ui/theme.py`):** Removed outer nested `QScrollArea` on `_queue_page` so `JobQueuePanel` expands cleanly into available vertical space; eliminated dimmed/low-contrast text on failed/completed/cancelled cards in Light & Dark modes; added semantic status pills, contextual primary CTAs (`Retry AI Analysis`, `Retry Failed Stage`, `Cancel Job`, `View Generated Clips`, `Cancel`), far-right `⋯` overflow menu, copyable redacted error details, and fixed Qt deferred-delete ghosting on theme/list rebuilds (`hide()` + `setParent(None)` + `deleteLater()`).
  - **Recent Activity Sorting & Completion Popup (`autoclip_studio/ui/queue_workspace.py`, `autoclip_studio/gui.py`):** Set `"Recent Activity"` as the default queue sort (active processing → queued in execution order → newest completed/failed/cancelled by timestamp descending so newly completed jobs never appear buried below older failed jobs). Added non-modal, deduplicated `JobCompletedDialog` ("Your clips are ready!", clip count, combined duration, processing time, primary CTA "View Generated Clips" that switches to Generated Clips and filters by project, secondary "Stay on Processing Queue"; never shown on startup for historical jobs).
  - **Theme-Aware Active Job Card & Pipeline Stepper (`autoclip_studio/ui/queue_workspace.py`):** Replaced hardcoded dark fills on `PipelineStepperWidget`, `_SourceThumbnail`, `JobRowCard`, `JobDetailsDialog`, and `QueueOverviewBar` with semantic Light/Dark tokens (`apply_theme()`), and wired stage-aware `Overall ETA` (`Estimating…` before measurable stage progress exists).
  - **Apple Silicon MLX Whisper Real Progress, Speed/RTF & Telemetry (`autoclip_studio/transcription.py`, `autoclip_studio/ui/speech_view.py`):** Instrumented `mlx_whisper.transcribe`'s internal Mel-frame progress loop (`sys.modules["mlx_whisper.transcribe"].tqdm.tqdm` + `ModelHolder.get_model`) without altering chunking or transcription accuracy; added `_InferenceMetricsTracker` (monotonic processed-audio seconds, EMA-smoothed realtime speed excluding model download/load, RTF `1 / speed`, bounded ETA), real substates (`Preparing audio`, `Downloading model`/`Loading model`, `Initializing inference`, `Transcribing audio`, `Finalizing timestamps`, `Saving transcript`), indeterminate vs determinate progress bar transitions, and `sample_apple_memory_telemetry()` (`MLX active/peak/cache MiB`, `App RSS MiB`, `Unified GB`, `memory_pressure`, `thermal_state`) labeled `"Apple Silicon Memory"` on macOS while preserving Windows `"GPU / VRAM"` telemetry.
  - **Live Apple M1 (`8 GB`, `macOS 27.2.1`) MLX Whisper Benchmark (`small`, `76.0s` spoken audio):** Verified monotonic progress `0.0s → 25.32s → 50.36s → 75.99s / 75.99s` (`15` segments, `10.55s` total), live speed `9.45× → 10.77×`, RTF `0.106 → 0.093`, accurate ETA (`5.36s` at `7.21s`, `2.55s` at `8.75s`), and real Apple Silicon telemetry (`MLX 467.7 MiB / peak 1174.0 MiB / cache 1809.4 MiB`, `App 124–248 MiB`, `Unified 3.84/8.0 GB`, `Thermal: Nominal`, `Pressure: Nominal → Warning (25% free)`).
  - **Generated Clips Master-Detail, Native Video Player & Dedicated Fullscreen (`autoclip_studio/ui/clips_workspace.py`, `autoclip_studio/ui/icons.py`, `autoclip_studio/gui.py`):** Upgraded `_clips_page` to a responsive `QSplitter` (stretch `5:4`, non-collapsible); enforced vertical-only clip library scrolling with dynamic-height `ClipLibraryCard` (naturally wrapping titles & descriptions, compact `★ score` + duration + status pills, zero horizontal clipping/overflow); added `chevron-left` and `minimize` Lucide icons; fixed theme-aware Play/Pause & Mute icon colors so Light mode controls are always high-contrast; added Prev/Next Clip navigation, red timeline/volume sliders, scrollable detail panel (hiding `Replay` chip when unavailable), sticky footer, and dedicated `_FullscreenPlayerWindow` sharing the single `QMediaPlayer` cleanly via `setVideoOutput()` handoff (`Escape`/`F`/`Space`).
  - **Visual QA Verified:** Captured and inspected Dark & Light screenshots (`1280×860`) for Processing Queue (idle + active MLX telemetry) and Generated Clips (real H.264 9:16 clip + thumbnail).
- **Phase 1 — Logo and Application Identity** (`cd597dc`)
- **Phase 2 — SaaS UI/UX Optimization and Brand System** (`64b1e2d`)
- **Phase 3 — Performance Optimization** (`75506ca`)
- **Phase 4 — Automatic Clip Thumbnail Generation** (`202ddd0`)
- **Generated Clips SaaS Master-Detail Redesign** (`a2f2bc6`)
- **First-Time Setup Wizard & Dependency Manager** (`fed338b`)
- **Local Video Import & Optional External SRT Import** (`3121a88`)
- **Native Apple Silicon Performance Optimization:**
  - **Dynamic Hardware & Rosetta Detection (`autoclip_studio/platform_utils.py`, `autoclip_studio/performance.py`, `autoclip_studio/ui/performance_view.py`):** Added cached `detect_platform()` with Rosetta translation detection (`sysctl.proc_translated`), P-core / E-core counts (`hw.perflevel0/1.physicalcpu`), GPU core count (`ioreg -rc AGXAccelerator`), swap usage (`psutil.swap_memory()`), throttled `memory_pressure -Q` & `pmset -g batt` (power source, battery %, charging), zero-sudo Objective-C `NSProcessInfo.processInfo.thermalState` (`Nominal`/`Fair`/`Serious`/`Critical`), and runtime-verified acceleration backends (`MLX Metal`, `VideoToolbox H.264`, `VideoToolbox HEVC`). Displays `N/A` whenever a sensor is unavailable (never fabricates metrics).
  - **Unified Memory & Power-Aware Profile Engine (`autoclip_studio/performance.py`):** Dynamically scales Eco / Balanced / Turbo threads, Whisper batch sizes, and face-tracking intervals based on detected P/E cores, available unified memory ratio, memory pressure, swap growth, thermal pressure, and battery power (pausing safely at stage boundaries on `Critical` thermal or memory pressure). No hardcoded chip or RAM capacities.
  - **MLX Whisper Optimization (`autoclip_studio/transcription.py`, `autoclip_studio/speech_engine.py`):** Added configurable quantized MLX model options (`large-v3-turbo-q4`, `small-q4`) without silently downgrading user-selected quality, single-model residency lock (`_MLX_INFERENCE_LOCK`), and `release_mlx_memory()` (`mx.clear_cache()` + `gc.collect()`).
  - **FFmpeg & VideoToolbox Optimization (`autoclip_studio/rendering.py`):** Added process-wide `_ENCODER_PROBE_CACHE` to eliminate repeated startup probes, added `hevc_videotoolbox` (`-tag:v hvc1`) alongside `h264_videotoolbox` with `libx264` CPU fallback, and kept SW decode → CPU `crop`/`scale`/`subtitles` → VT encode as the validated fastest path.
  - **Real Apple M1 (`8 GB`, `4P+4E`, `7-core GPU`, `macOS 27.2.1`) Cold/Warm Benchmarks (`1920×1080` 10s → `1080×1920` crop + Lanczos):**
    - `h264_videotoolbox` (SW decode → VT encode): **1.77s cold / 1.76s warm** (`4.89 MB`) — **22% faster** than `libx264`.
    - `h264_videotoolbox` (`-hwaccel videotoolbox` decode): `1.84s` cold / `1.85s` warm (4.5% slower due to GPU→CPU readback for CPU filters, avoided by default).
    - `hevc_videotoolbox` (`-tag:v hvc1`): `1.88s` cold / `1.84s` warm (`4.14 MB`, **15% smaller**).
    - `libx264` (`medium`, CRF 20 CPU fallback): `2.33s` cold / `2.25s` warm (`4.24 MB`).
- **Centralized Design System, Poppins Typography & Light/Dark/System Theme System:**
  - **Bundled Poppins Typography (`autoclip_studio/assets/fonts/poppins/`):** Bundled official Google Fonts Poppins TTF weights (`Regular`, `Medium`, `SemiBold`, `Bold`) + `OFL.txt` license, loaded via `QFontDatabase` (`load_bundled_fonts()`) across all UI text, dialogs, menus, and tooltips (with monospace retained for `LogView` diagnostic logs and caption/subtitle/export fonts untouched).
  - **Centralized ThemeManager & Design Tokens (`autoclip_studio/ui/theme.py`, `autoclip_studio/ui/style.py`, `autoclip_studio/config.py`):** Implemented semantic Light & Dark color palettes, 4px spacing scale, typography hierarchy, border radius tokens, `QPalette`, cached QSS stylesheets, and `ThemeManager` with instant live switching (no restart, preserves active jobs/navigation/playback), persisted `appearance` setting (defaulting to Dark Mode), and native OS `QStyleHints.colorSchemeChanged` synchronization in `System` mode.
  - **Widget & Card Standardization (`autoclip_studio/assets/icons/*.svg`, `SectionCard`):** Replaced outdated intersecting `QGroupBox` borders on New Project, AI Provider Settings, and Performance with clean `SectionCard` containers; replaced native spinbox/combobox/checkbox indicators with bundled SVG chevrons (`chevron-up/down-{dark|light}.svg`) and `check-white.svg` across `QSpinBox`, `QDoubleSpinBox`, `QComboBox`, and `QCheckBox`.
  - **Sidebar Appearance Switcher & Theme-Aware Assets (`gui.py`, `icons.py`, `log_view.py`, `heatmap_view.py`, `dependency_view.py`, `queue_workspace.py`, `clips_workspace.py`):** Added compact Sun / Moon / Monitor appearance switcher at the bottom of the sidebar, automatic light-mode charcoal wordmark recoloring preserving the red AutoClip symbol, and live theme updates across all custom painters, badges, icons, and logs.
  - **Visual QA Verified:** Captured and inspected all 9 pages in both Dark Mode and Light Mode (`1280×820`) on macOS Retina; verified Poppins loading, spinbox/dropdown chevrons, checkboxes, logo readability, icon contrast, and zero overlapping cards.
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
  - `.venv/bin/python3 -m pytest -q`: **142 passed, 3 skipped, 0 failed** (includes `tests/test_auto_director.py`, `tests/test_ui_refinement_and_telemetry.py`, `tests/test_persistent_settings.py`, `tests/test_startup_and_downloader.py`, `tests/test_smart_reframe.py`, `tests/test_apple_silicon_perf.py`, `tests/test_theme_system.py`, `tests/test_heatmap_and_hybrid.py`; 3 Windows-only/environment skips).
  - `.venv/bin/python3 -m tests.integration_smoke`: **Passed** (`Auto Director (auto/natural, opencv-haar-multi)`, `h264_videotoolbox`, `6.30s`).
  - `.venv/bin/python3 scripts/setup.py --check`: **Passed** (exit code 0).
- **Windows 11 x64 Host (previous session):**
  - `.\.venv\Scripts\python.exe -m pytest -q`: **93 passed**.

---

## Multi-Person Active Speaker Smart Reframing

### Root Cause of Previous Midpoint Bug
Auditing legacy `autoclip_studio/tracking.py` identified three compounding causes:
1. **Frontal-only Haar cascade (`haarcascade_frontalface_default.xml`)** frequently dropped detections on 3/4-turned podcast speakers; every dropout fell back to `target = frame_width / 2.0` (the empty center gap).
2. **Stateless largest-face selection (`max(faces, key=area)`)** had no persistent track IDs or speaker scoring; when two seated hosts had similar face sizes, tiny jitter alternated `target` between left and right faces.
3. **Slow exponential moving average (`0.25 * target + 0.75 * smoothed`) + linear FFmpeg interpolation** averaged alternating targets and center dropouts directly into the empty midpoint between participants and slowly panned across empty background.

### Reference Repository & License Audit
- **`algometrix/vertical_video_convertor`** (*MIT*): Reviewed sticky-subject and refocus-band concepts; adopted conceptually without adding heavy InsightFace/ONNXRuntime dependencies.
- **`Rembr4nt/ai-clipper`** (*MIT*): Reviewed two-up vertical podcast split-screen framing and smoothing concepts; implemented natively in FFmpeg single-pass `vstack` + `concat` filter graphs.
- **`nawta/TalkNet-ASD-py312`** (*No license file / All Rights Reserved*, unclear pretrained weight licensing): Rejected for copying or bundling.
- **`akouris/360-autocam`** (*No license file*): Rejected for copying.
- **`RishikeshAluguvelli/VerticalMaker`**: Not publicly accessible via GitHub search.
- **Decision:** Clean first-principles implementation in `autoclip_studio/smart_reframe.py` using already-installed OpenCV (`cv2.FaceDetectorYN` + multi-orientation Haar fallback) and PyAV (`av`) with zero unlicensed code and zero mandatory large downloads.

### Implemented Architecture
- **Smart Reframing Engine (`autoclip_studio/smart_reframe.py`, backward-compatible wrapper in `autoclip_studio/tracking.py`):**
  - **Optional MIT YuNet 5-Point Landmark Model (`face_detection_yunet_2023mar.onnx`, 227 KB)** integrated into Dependency Manager (`scripts/dependencies.json`, `autoclip_studio/dependencies.py`) with automatic zero-download multi-orientation OpenCV Haar fallback (frontal `alt2` + `default` + left/right mirrored `profileface` + containment NMS).
  - **Audio-Visual Active Speaker Detection:** PyAV 16 kHz speech-band (`250–3600 Hz`) FFT RMS VAD synchronized with head-motion-compensated mouth ROI visual energy (`max(0, mouth_diff - 0.65 * upper_face_diff)`), optionally supported by transcript timing prior.
  - **Aspect-Ratio Feasibility & Stability:** `can_group_frame()` checks whether multiple faces safely fit within `80%` of the 9:16 crop width (never averaging distant faces); enforces persistent track IDs, `1.0s` dropout tolerance, `1.6s` minimum speaker hold (`0.55s` initial confirmation after camera cuts), `1.28×` hysteresis ratio, `4.5%` crop dead zone, bounded pan velocity, scene-cut detection, content-aware edge saliency fallback when no faces are visible, and instant step cuts (`cut=True`) between distant speakers instead of panning across empty space.
  - **Podcast Split Screen & Fit Original Video (`autoclip_studio/rendering.py`, `autoclip_studio/captions.py`):** Single-pass FFmpeg `-filter_complex` supporting stacked vertical 2-person split-screen (`vstack=inputs=2`) with median-stabilized headroom boxes, automatic return to full-frame single-speaker framing on close-up camera cuts (`concat`), continuous synchronized audio (`0:a?`), seam-centered ASS captions (`Alignment=5`) for split-screen clips, and blurred-background `Fit Original Video`.
  - **UI & Settings (`autoclip_studio/gui.py`, `autoclip_studio/models.py`, `autoclip_studio/pipeline.py`):** Added compact **Smart reframing** selector on New Project (`Auto Smart Reframe` default, `Active Speaker`, `Podcast Split Screen`, `Single Subject Tracking`, `Fit Original Video`, `Off (Center Crop)`), preserving Poppins typography, Light/Dark/System themes, and backward-compatible `face_tracking` behavior.

### Real-Footage Validation & Benchmarks (`WxFliJRKMZk`)
Validated on real multi-person Indonesian podcast footage from `https://www.youtube.com/watch?v=WxFliJRKMZk` (*Empat Teman Datang Bawa Parfum*, Raditya Dika — `05:00–05:18` mixed solo close-ups + 2-person wide shots, and `12:00–12:18` 5-person wide table shot):

| Mode / Backend | Analysis Time (18s clip) | Peak Python RAM | Face Visibility | Midpoint Traps | Switches / Cuts | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Legacy tracker (baseline) | 0.42s | 1.6 MB | 34/36 (94.4%) | 2 | — | Off-center drift between distant hosts |
| Smart Reframing `auto` (`opencv-haar-multi`) | 5.28s | 6.6 MB | **36/36 (100.0%)** | **0** | 5 / 5 | Zero-download built-in fallback |
| Smart Reframing `auto` (`opencv-yunet-landmarks`) | **1.01s** | **3.1 MB** | **36/36 (100.0%)** | **0** | 2 / 5 | 5.2× faster, cleaner speaker stability |
| Smart Reframing `active_speaker` (`opencv-yunet-landmarks`) | **0.89s** | **3.1 MB** | **36/36 (100.0%)** | **0** | 2 / 5 | Clean cuts between speaking hosts |
| Smart Reframing `split_screen` (`opencv-yunet-landmarks`) | **0.88s** | **3.1 MB** | 100% both hosts + solo close-ups | **0** | 3 split + 3 single segs | Auto-returns to single speaker on solo close-ups |

- **5-Person Wide Table Detection (`12:00–12:18`):** `opencv-yunet-landmarks` detected all **5/5** seated participants (`cx = [136, 200, 307, 434, 474]` on 640×360).
- **Rendered 9:16 Outputs (`540×960` with Indonesian captions):**
  - `active_speaker`: `18.00s` video / `18.00s` audio (`0.000s` sync diff), `2.08 MB` (`3.01s` CPU).
  - `split_screen` (mixed split + close-up timeline): `18.00s` / `18.00s` (`0.000s` sync diff), `2.15 MB` (`2.67s` CPU; **`2.79s` with Apple `h264_videotoolbox`**).
  - `fit_original` (blurred background): `18.00s` / `18.00s` (`0.000s` sync diff), `1.38 MB` (`2.60s`).
- **Performance Profile Scaling (18s clip, YuNet):** Eco (`1.0s`) `0.70s` (68 frames), Balanced (`0.75s`) `0.86s` (90 frames), Turbo (`0.5s`) `0.98s` (108 frames).

### Remaining Limitations
- Built-in `opencv-haar-multi` works with zero downloads, while installing the optional 227 KB MIT **YuNet Face & Landmark Model** in Dependencies delivers ~5× faster analysis and markedly better 3/4-profile / wide multi-person detection.
- For mixed clips that switch between Podcast Split Screen and solo close-up shots, the clip-level ASS subtitle file keeps seam-centered middle alignment (`Alignment=5`) throughout so captions never overlap split-screen faces; per-segment dynamic ASS positioning could be added as a future enhancement.
- Very dark, heavily occluded, or off-camera speakers (where a speaking host's mouth is completely hidden behind a large studio microphone) rely on audio VAD + last-valid-speaker hold until visual mouth activity recovers.

---

## Startup, Dependency Management & YouTube Downloader Stability Fix

### Confirmed Root Causes (Verified from Logs & Code)
1. **Unexpected First-Time Setup & Silent `yt-dlp` Downgrade:**
   - Confirmed directly in `~/Library/Application Support/AutoClip Studio/logs/launcher.log`:
     - `yt-dlp 2025.11.12 is required (installed 2026.11.23)` → `Uninstalled 1 package ... - yt-dlp==2026.11.23 / + yt-dlp==2025.11.12`.
   - `scripts/setup.py:check()` enforced strict `installed != expected` equality against `requirements.txt` (`yt-dlp==2025.11.12`). Updating `yt-dlp` caused `--check` to exit `1`, which forced `Launcher.m` to re-open First-Time Setup and run `uv pip install -r requirements.txt`, silently downgrading `yt-dlp` back to `2025.11.12`.
2. **Stuck First-Time Setup & Unreadable Dark Mode Controls (`packaging/macos/Launcher.m`):**
   - `Launcher.m` set dark background color (`#101116`) without setting `window.appearance = NSAppearanceNameDarkAqua`, causing macOS Light/System mode to render black checkbox and button text on a dark surface.
   - `Launcher.m` ran `uv run ... scripts/setup.py --no-tests --local-whisper` synchronously with an indeterminate spinner (no `--json-progress` streaming, no 20s startup check timeout, no child-process termination on Cancel, no duplicate-bootstrap lock, and no expandable diagnostic log).
3. **Recurring YouTube Download Failure (`https://www.youtube.com/watch?v=dSq0Z5XpoLc`):**
   - Confirmed via `yt-dlp --verbose`: `yt-dlp` had been downgraded to `2025.11.12` and `autoclip_studio/media.py:yt_dlp_command()` only checked bundled `deno`, ignoring system Node.js (`/opt/homebrew/bin/node` v26.3.1). With `[debug] JS runtimes: none` and an outdated extractor, YouTube skipped SABR web formats, fell back to `android` sdkless format `399+251`, and failed with `HTTP Error 403: Forbidden`.
4. **Opaque Subprocess Error Messages:**
   - `ProcessManager.run()` raised raw `subprocess.CalledProcessError`, whose `__str__()` exposes only `Command '[...]' returned non-zero exit status 1` and drops `error.output` (stderr/stdout).

### Implemented Fixes & Dependency Strategy
- **Idempotent Startup & Version Persistence (`requirements.txt`, `scripts/setup.py`, `scripts/dependencies.json`):**
  - Updated `yt-dlp` specification from strict `yt-dlp==2025.11.12` to minimum floor **`yt-dlp>=2026.11.23`**; `scripts/setup.py` now parses `==`, `>=`, `~=` and never flags or downgrades newer compatible versions of `yt-dlp`, `yt-dlp-ejs`, or `mlx-whisper`.
  - Optional components (`yunet_asd_model`, `whisper_model_large_v3_turbo`, `cuda_runtime`, `caption_fonts`) and dev tools (`pytest`) never trigger mandatory First-Time Setup.
  - Added `--repair-only` selective installation (skips package installation completely when required packages are satisfied; supports both `uv` without `pip` and `.venv` `pip`), a non-blocking `.setup.lock` lockfile against duplicate runs, Cloud-only `settings.json` awareness, and structured `--json-progress` stages + byte/speed/ETA metrics.
- **Upgraded Dependency Manager (`autoclip_studio/dependencies.py`, `autoclip_studio/ui/dependency_view.py`):**
  - Automatic `detect_package_manager()` supporting `uv`-managed venvs without `pip`, `pip`-managed venvs, native macOS ARM64, and Windows Python.
  - Organized components into 7 categories: *Core Application Runtime*, *Media Processing Tools*, *YouTube Downloader*, *Local Transcription*, *Face Tracking*, *Advanced Active Speaker Detection*, and *Platform-Specific Acceleration*.
  - Added official PyPI stable-only `check_yt_dlp_update()`, `update_yt_dlp()` (blocked during active processing jobs, persists versions in `dependency_state.json`, validates CLI + `YoutubeIE` post-update, and automatically rolls back to the prior known-good version on failure), and one-click optional YuNet 227 KB model install.
- **Downloader Reliability & Actionable Errors (`autoclip_studio/media.py`, `autoclip_studio/diagnostics.py`, `autoclip_studio/worker.py`, `autoclip_studio/ui/queue_workspace.py`, `autoclip_studio/gui.py`):**
  - Added `detect_js_runtime()` auto-detecting `deno`, `node`, `bun`, and `quickjs` and passing `--js-runtimes <kind>:<path>` to `yt-dlp`.
  - Added structured `DownloadError` + `classify_download_error()` covering HTTP 403, HTTP 429, auth/bot restrictions, extractor errors, JS runtime/EJS errors, format unavailability, missing FFmpeg/FFprobe, network interruptions, disk space, permissions, FFmpeg merge errors, cancellation, and unknown stderr failures — with automatic secret/cookie/token redaction.
  - Added `--continue` resume, bounded retries (max 3 attempts with backoff + safe fallback format), and post-download `ffprobe`/`validate_mp4` integrity verification before publishing to `cache/downloads/`.
  - Added `ErrorDetailsDialog` with concise headline, expandable redacted Technical Details, **Copy Diagnostics**, **Retry**, and **Open Dependency Manager** buttons.
- **Native macOS Launcher & First-Time Setup UI (`packaging/macos/Launcher.m`, `packaging/build_macos.py`):**
  - Forced `NSAppearanceNameDarkAqua` + explicit white attributed checkbox label and bundled Poppins font loading (`CoreText`, with native system fallback).
  - Added 20-second startup validation timeout, `flock` single-instance setup lock, live `--json-progress` streaming (real percentage, downloaded/total bytes, speed, ETA), expandable diagnostic log viewer + Copy Logs, Retry button, and clean subprocess termination on Cancel.
  - Rebuilt `dist/AutoClipStudio.app` (`Mach-O 64-bit executable arm64`) because `Launcher.m` itself had confirmed dark-mode/progress/timeout root causes. Ordinary Python source edits still require **no** launcher rebuild.

### Actual Regression Test Results (macOS 27.2.1 Apple Silicon ARM64)
- **Dependency Version Persistence:** Updated `yt-dlp` `2025.11.12 → 2026.11.23` via `update_yt_dlp()`; ran `scripts/setup.py --check` (exit `0`) and `scripts/setup.py --repair-only --no-tests` (exit `0`); verified `yt-dlp` remained `2026.11.23`.
- **Repeated Native App Startup:** Launched rebuilt `dist/AutoClipStudio.app` twice consecutively, closing cleanly between runs — both launches started `main.py` directly without opening First-Time Setup (`0 bytes` added to `launcher.log`). Verified fresh-install detection cleanly in an isolated temp root without altering `.venv`.
- **Full End-to-End YouTube Download (`https://www.youtube.com/watch?v=dSq0Z5XpoLc`):**
  - JS runtime auto-detected: `('node', '/opt/homebrew/bin/node')`.
  - Metadata extracted in `3.06s`: *"Pertemuan Bersejarah dr. Tirta dan dr. Gia"*, `1920 × 1080`, `4127s`, **100 real Audience Replay Heatmap points**, **2,324 Indonesian automatic caption segments**.
  - Complete `acquire_source()` 1080p download + FFmpeg 9.1 merge + `ffprobe` validation succeeded in `203.4s`: `dSq0Z5XpoLc.mp4` (`1,592,652,488 bytes`, `vp9 1920×1080` + `opus`, `4126.71s`, `988` progress events).
  - Second call reused verified cache in `0.099s` (`Using cached download: dSq0Z5XpoLc.mp4`).
- **Windows Compatibility:** Reviewed `packaging/launcher/Launcher.cs` and `SetupWizard.cs` (both delegate `--check` and `--json-progress` to `scripts/setup.py`, so they benefit automatically without requiring a Windows `.exe` rebuild); Windows host runtime execution was not re-run on this macOS machine.

### Remaining Limitations & Launcher Notes
- `dist/AutoClipStudio.app` was rebuilt locally for this launcher-level fix (unsigned/ad-hoc for local macOS use); re-run `.venv/bin/python3 packaging/build_macos.py --dmg` if an updated `.dmg` installer image is also needed.
- On systems without any JavaScript runtime (`node`, `deno`, `bun`, or `quickjs`), YouTube may still restrict certain SABR streams; the downloader now reports a clear `js_runtime_error` / `http_403` diagnostic guiding installation of Node.js or Deno.

---

## Persistent Settings & Provider State Management

### Confirmed Root Cause of Custom Provider HTTP 400 (Missing Model ID)
Auditing `autoclip_studio/gui.py`, `autoclip_studio/providers.py`, and `autoclip_studio/config.py` confirmed four interacting defects:
1. **Empty model saved before discovery:** Selecting `custom` and clicking **Save settings** before **Load models** persisted `"model": ""`.
2. **`load_models()` neither persisted the selected model nor synced New Project:** Clicking **Load models** populated `self.model_input` only in-memory without saving the chosen model or updating `self.project_model`, leaving New Project sending `"model": ""` unless the user clicked **Save settings** a second time.
3. **Destructive refresh & race condition:** `load_models()` called `self.model_input.clear()` and only restored `current` if it literally appeared in the refreshed list (dropping manual model IDs), discarded cached lists on every provider switch/restart, and lacked request-sequence/provider guards against stale async responses.
4. **No pre-request empty-model guard:** `AIProvider._with_models()` fell back to `or [self.model]`, sending `{"model": ""}` to `/chat/completions` and triggering HTTP 400.

### Storage Architecture & Migration Strategy
- **Single Source of Truth (`autoclip_studio/config.py` — `SettingsStore` / `SettingsManager`):**
  - Stored in platform-appropriate app data directory (`~/Library/Application Support/AutoClip Studio/settings.json` on macOS; `%LOCALAPPDATA%\AutoClip\AutoClip Studio\settings.json` on Windows — preserving full backward compatibility with existing Windows installs and `Launcher.cs`).
  - **Credentials isolated:** API keys remain strictly in **macOS Keychain** / **Windows Credential Manager** via `keyring`; `_strip_secrets()` recursively strips forbidden secret keys (`api_key`, `token`, `password`, `secret`) before writing JSON so credentials can never leak to `settings.json`.
  - **Schema v2 & Migrations (`SCHEMA_VERSION = 2`):** Automatic `_migrate_settings()` upgrades legacy v0/v1 files cleanly while preserving every user field (`active_provider`, per-provider `base_url`/`model`/`fallback_models`, `caption_template`, `speech`, `performance`, `appearance`, `hardware_encoding`, `output_dir`) and adds structured `project`, `asd`, `downloader`, `dependencies`, `ui`, plus per-provider `display_name` and `cached_models` (stored separately from `model`).
  - **Storage Reliability:** Thread-safe `RLock`, atomic temp-file + `os.fsync` + `os.replace`, `save_debounced()` + shutdown `flush()`, corruption recovery backing up unreadable files to `settings.corrupt-<timestamp>.bak`, and explicit user-only `Reset to defaults` button (First-Time Setup and dependency validation never reset settings).
- **Provider State & Queue Integration (`autoclip_studio/gui.py`, `autoclip_studio/providers.py`, `autoclip_studio/pipeline.py`):**
  - `MainWindow` guards UI hydration (`_hydrating_ui`) and provider form loading (`_loading_provider_form` + `QSignalBlocker`) so startup/switching never overwrites saved values with empty defaults.
  - `load_models()` uses monotonic `_model_load_seq` + target-provider checks, caches model lists separately without clearing the selected/manual model, auto-persists & syncs `project_model` when selecting a model on first load, and preserves existing state when model discovery fails.
  - Persists all user preferences automatically (Smart Reframing mode, clip settings, captions, cache, highlight detection mode, hybrid AI weight, YouTube subtitles toggle, multimodal toggle, Whisper model/device, theme, window dimensions).
  - Pre-request validation in both `gui.py` and `AIProvider._with_models()` refuses empty model IDs with an actionable `ProviderError(..., category="model")` before sending HTTP requests; failed AI Highlights jobs retry via `resume_analysis=True` from the cached transcript without repeating download/transcription or mutating active job snapshots.

### Regression Test Results
- **Unit & UI Suite (`.venv/bin/python3 -m pytest -q`):** **132 passed, 3 skipped, 0 failed** (including 7 new tests in `tests/test_persistent_settings.py` verifying v1→v2 migration & secret stripping, corrupted JSON backup/recovery, concurrent atomic + debounced saves, empty-model HTTP refusal, custom provider single Save+Load & double relaunch persistence, per-provider switching + manual model preservation + failed discovery resilience, and stale async response rejection).
- **Live macOS Validation:** Verified `macOS Keychain` backend, verified v1→v2 migration on a copy of real `~/Library/Application Support/AutoClip Studio/settings.json` with 100% preference preservation and zero plaintext credentials, visually inspected the updated AI Provider Settings UI, and confirmed `scripts/setup.py --check` leaves settings intact.
- **Windows Compatibility:** Preserved Windows Credential Manager, `%LOCALAPPDATA%` path compatibility, and shared PySide6 codebase; live Windows host execution was not re-run on this macOS machine.
