# AutoClip Studio

AutoClip Studio is a native cross-platform desktop application for **macOS (Apple Silicon ARM64)** and **Windows (x86-64)** that transforms YouTube videos and local recordings into ranked, reframed, captioned vertical shorts. It is local-first, has no watermarks or application quotas, and does not require Docker, WSL, a VM, or a paid subscription.

The entire media pipeline runs locally on your machine. An AI API key is optional: without a key, AutoClip Studio uses a local transcript-density, hook, and YouTube replay-heatmap scorer; with a key, it analyzes timestamped transcripts via **Google Gemini**, **OpenAI**, or any **OpenAI-compatible** endpoint.

---

## Key Features

### Modern Desktop UI & Unified Settings
- **Floating Collapsible Sidebar:** Inset rounded navigation container (`Dashboard`, `New Project`, `Processing Queue`, `Generated Clips`, `Caption Templates`, and an expandable `Settings` tree) with icon-only collapse mode, tooltips, and anchored Settings flyout.
- **Light, Dark & System Themes:** Centralized design system with bundled **Poppins** typography, visual theme preview cards under **Settings → Appearance**, and instant theme switching with zero restart.
- **Persistent Workspace State:** Atomic local settings (`settings.json` v2) preserving AI providers and selected models, project defaults, sidebar layout, appearance, and video player volume/mute across restarts.

### Flexible Ingest & Highlight Discovery
- **YouTube & Local Video Import:** Paste a YouTube URL (with debounced metadata, thumbnail, and Most Replayed heatmap preview) or drag-and-drop a local video file.
- **Flexible Subtitle Pipeline:** Priority chain supporting optional user-supplied `.srt` files (with offset sync, skipping transcription), YouTube manual/auto subtitle extraction, or local/cloud Speech-to-Text.
- **Hybrid AI + Heatmap Highlight Ranking:** Combine AI transcript scoring with YouTube's **Most Replayed** audience heatmap (`Hybrid`, `AI Analysis`, or `Most Replayed` modes) with configurable weighting.

### Intelligent Auto Director & Smart Reframing
- **Single Active-Speaker Auto Reframing:** Tracks all visible participants in 9:16 portrait composition using OpenCV (YuNet / Haar) with lip-motion and voice-activity scoring, automatically locking onto one dominant speaker at a time (never splitting frames automatically; **Podcast Split Screen** remains selectable as an explicit manual mode).
- **Offline Viterbi/DP Auto Director:** Eliminates rapid ping-pong speaker cuts and tripod micro-jitter with configurable reframing styles (**Natural**, **Stable**, **Dynamic**), frame-accurate original camera cut synchronization, and eased same-subject pans.
- **Animated Caption Templates:** Word-level ASS karaoke captions with customizable built-in templates and live preview in **Caption Templates**.

### Reliable Downloader with Pause, Resume & Rate-Limit Recovery
- **Persistent Partial Downloads:** Stable per-video/per-job download workspaces preserve `.part` files and resume metadata across pauses, network interruptions, and restarts using `yt-dlp --continue --part`.
- **Distinct Pause, Resume & Cancel Controls:** Pause active YouTube downloads without losing progress, resume from preserved bytes, or cancel with clean subprocess tree termination and workspace cleanup.
- **HTTP 429 & Verification Protection:** Stops retry storms immediately on `HTTP 429 Too Many Requests`, respects `Retry-After` headers and bounded cooldowns with live countdown badges, deduplicates concurrent metadata/subtitle requests, and suspends automatic retries when YouTube requests sign-in or verification.

### Native Hardware Acceleration & Telemetry
- **macOS Apple Silicon (ARM64):**
  - **MLX Whisper** on Apple Metal with real-time progress, speed (`×`), RTF, ETA, and live Apple Silicon memory/thermal/pressure telemetry (plus optional quantized models such as `large-v3-turbo-q4` and `small-q4`).
  - **Apple VideoToolbox** hardware encoding (`h264_videotoolbox` / `hevc_videotoolbox`) with automatic `libx264` fallback.
  - Dynamic P-core/E-core, unified memory, battery/AC, and thermal adaptation across **Eco**, **Balanced**, and **Turbo** profiles (with Rosetta translation detection).
- **Windows (x86-64):**
  - **Faster-Whisper** and **Purfview Faster-Whisper XXL** with NVIDIA CUDA acceleration and CPU fallback.
  - **NVENC** hardware encoding with automatic `libx264` fallback, Windows Job Object process cleanup, and **Windows Credential Manager** storage.

### Processing Queue & Native Clip Player
- **Processing Queue:** Live 6-stage visual pipeline stepper, transfer & transcription telemetry cards, pause/resume/retry actions, recent-activity sorting, searchable history, redacted copyable/exportable logs, and a non-modal completion popup.
- **Generated Clips Workspace:** Master-detail library with automatic clip thumbnails (custom frame picker & export), score breakdown chips, and an embedded native 9:16 video player with synchronized audio (native macOS AVFoundation `darwin` backend with CoreAudio Bluetooth/device hot-switching, persisted volume/mute, keyboard shortcuts, and seamless fullscreen handoff).
- **Dependency Manager & Setup:** Built-in dependency diagnostics, `yt-dlp` updates with rollback, FFmpeg/runtime verification, and Whisper model management under **Settings → Dependencies**.

---

## Navigation & Settings Structure

```text
AutoClip Studio
─────────────────────
Dashboard
New Project
Processing Queue
Generated Clips
Caption Templates
─────────────────────
Settings
  ├── General         (Output folder, hardware encoding, cache management, reset)
  ├── Appearance      (Light / Dark / System preview cards, collapsed sidebar toggle)
  ├── AI Providers    (Gemini / OpenAI / Custom endpoint, secure keys, model discovery)
  ├── Transcription   (MLX Whisper / Faster-Whisper / Purfview XXL / cloud engines)
  ├── Performance     (Eco / Balanced / Turbo profiles, live hardware telemetry)
  └── Dependencies    (Runtimes, FFmpeg, yt-dlp maintenance, Whisper models)
```

---

## Quick Start

### macOS (Apple Silicon ARM64)

Requirements: macOS on Apple Silicon (M1 or newer), Homebrew `ffmpeg-full` (with `libass` / `h264_videotoolbox`), Node.js (for `yt-dlp` YouTube JS challenges), and a native ARM64 Python 3.12 virtual environment (`.venv`).

1. Launch from a source checkout:
   ```zsh
   .venv/bin/python3 main.py
   ```
   Or open the generated `dist/AutoClipStudio.app` bundle.
2. Optionally open **Settings → AI Providers**, enter your API key (stored in **macOS Keychain**), load or type a model ID, test the connection, and click **Save settings**.
3. Open **New Project**, paste a YouTube URL or drop a local video, configure clip duration, highlight mode, reframing style, and captions, then click **Generate Shorts**.
4. Monitor transfers and rendering in **Processing Queue** (with Pause/Resume support) and play or export results in **Generated Clips**.

### Windows (x86-64)

#### Packaged Windows application
Open `dist\AutoClipStudio\AutoClipStudio.exe` directly (keep its `_internal` folder beside the executable), or run `release\AutoClipStudio-Setup-x64.exe` to install for the current user. User settings, credentials, exports, and caches survive uninstall.

#### Source checkout
Requirements: Windows 11, an internet connection for initial setup/model downloads, and ~2 GB free disk space (NVIDIA GPU optional).

1. Double-click `AutoClipStudio.exe` in the checkout (or run `.\.venv\Scripts\python.exe main.py`). If dependencies need setup, accept the setup dialog to install the project-local Python environment and FFmpeg.
2. Optionally open **Settings → AI Providers**, enter a key (stored in **Windows Credential Manager**), choose or load a model, test, and save.
3. Open **New Project**, choose a YouTube URL or local video, and click **Generate Shorts**.

The Windows source launcher owns its process tree and supports `--check`, `--setup`, and `--stop`.

---

## Processing Workflow

1. **Ingest:** Import a local video or download a YouTube source into a persistent workspace with `yt-dlp` (`--continue --part --sleep-requests`), verifying media integrity before caching.
2. **Transcript:** Import user `.srt` if provided, reuse/extract YouTube subtitles when enabled, or transcribe locally via **MLX Whisper** (macOS) / **Faster-Whisper** or **Purfview XXL** (Windows).
3. **Highlight Selection:** Rank candidate clips using the configured AI provider (`Gemini`, `OpenAI`, or custom OpenAI-compatible endpoint) and/or YouTube Most Replayed heatmap data, or fall back to the local no-key scorer.
4. **Auto Director & Smart Reframing:** Detect faces and active speakers across selected highlight intervals, solve shot framing via offline Viterbi/DP optimization, and align cuts to exact source cut frames.
5. **Captioning & Rendering:** Generate word-timed ASS captions and render vertical MP4 clips via FFmpeg using **VideoToolbox** (macOS) or **NVENC** (Windows) with automatic `libx264` CPU fallback.
6. **Validation & Thumbnails:** Probe every rendered MP4 for valid streams and duration, generate clip thumbnails, persist metadata in SQLite, and write a `project.json` manifest beside the exported clips.

---

## AI Providers

- **Gemini (default):** `https://generativelanguage.googleapis.com/v1beta` (`gemini-2.5-flash`) — also supports optional multimodal upload through the Gemini Files API (off by default).
- **OpenAI:** `https://api.openai.com/v1` (`gpt-4o-mini`).
- **Custom OpenAI-Compatible:** Enter any HTTPS `/v1` base URL (HTTP allowed for `localhost`), display name, and model ID (either discovered via **Load models** or entered manually).

API keys are never written to `settings.json`; they are stored exclusively in **macOS Keychain** or **Windows Credential Manager** and redacted from all logs and diagnostics. Optional comma-separated fallback model IDs and **Retry AI Analysis** let you recover failed AI analysis without re-downloading or re-transcribing the video.

---

## Local Data Directories

By default, application data is stored in the platform user data directory:

- **macOS:** `~/Library/Application Support/AutoClip Studio/`
- **Windows:** `%LOCALAPPDATA%\AutoClip\AutoClip Studio\`

Contents:
- `autoclip.db` — SQLite database for projects, download recovery state, and generated clips
- `settings.json` — Non-secret versioned user preferences (schema v2)
- `cache/` — Cached downloads, persistent partial download workspaces, metadata/subtitle caches, transcripts, and model weights (override model cache via `HF_HUB_CACHE`)
- `logs/autoclip.log` — Rotating application, downloader, and media diagnostics
- `output/` — Exported project folders, MP4 clips, thumbnails, and `project.json` manifests

Set `AUTOCLIP_DATA_DIR` before launch to use a custom data root, or configure a custom export folder under **Settings → General**.

---

## Repository Layout

| Location | Contents |
| --- | --- |
| [autoclip_studio/](autoclip_studio/) | Core pipeline, `auto_director.py`, `smart_reframe.py`, `download_state.py`, `media.py`, `transcription.py`, `performance.py`, `providers.py`, `database.py`, `config.py` |
| [autoclip_studio/ui/](autoclip_studio/ui/) | PySide6 views, `sidebar.py`, `theme.py`, `components.py`, `icons.py`, `queue_workspace.py`, `clips_workspace.py`, `dependency_view.py`, `performance_view.py`, `speech_view.py` |
| [autoclip_studio/assets/](autoclip_studio/assets/) | Official AutoClip Studio logos, app icons, bundled Poppins fonts (`OFL.txt`), and SVG UI assets |
| [scripts/](scripts/) | Dependency verification, setup, and bootstrap helpers |
| [packaging/](packaging/) | Native Windows launcher, macOS/Windows build scripts, PyInstaller spec, and installer |
| [tests/](tests/) | Unit, UI, audio playback, download recovery, Auto Director, and integration smoke tests |
| [docs/](docs/) | Audits, benchmarks, and third-party notices |

---

## Development & Validation

### macOS (Apple Silicon)

```zsh
# Full unit & UI regression suite
AUTOCLIP_DATA_DIR=/tmp/autoclip-test-data .venv/bin/python3 -m pytest -q

# Native Cocoa audio playback & floating sidebar verification
AUTOCLIP_DATA_DIR=/tmp/autoclip-test-cocoa .venv/bin/python3 -m pytest -v tests/test_player_audio.py tests/test_floating_sidebar_navigation.py

# End-to-end FFmpeg + Auto Director + VideoToolbox render smoke test
AUTOCLIP_DATA_DIR=/tmp/autoclip-smoke-data .venv/bin/python3 -m tests.integration_smoke
```

### Windows (PowerShell)

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m tests.integration_smoke
.\.venv\Scripts\python.exe packaging/build.py
.\.venv\Scripts\python.exe packaging/build.py --standalone
.\.venv\Scripts\python.exe packaging/build.py --installer
```

Installer compilation on Windows requires Inno Setup 6 or 7 (`--iscc "C:\path\to\ISCC.exe"`).

---

## Known Limitations

- **YouTube access & bot checks:** Private, members-only, DRM-protected, or verification-gated videos cannot be downloaded automatically; AutoClip Studio suspends requests on bot checks rather than attempting to bypass YouTube restrictions.
- **Visual-only speaker detection:** Without optional YuNet ONNX weights, OpenCV Haar detection can briefly lose extreme profiles or heavily downward-tilted faces (mitigated by Auto Director shot lookahead and dropout backfill). Simultaneous cross-talk relies on visual lip-motion + VAD rather than neural audio diarization.
- **First-run Whisper downloads:** Initial transcription with a new Whisper model downloads model weights (`small` is the balanced default; `medium` and `large-v3` require more memory).
- **Scope:** AutoClip Studio focuses on local clip generation, previewing, and export; it does not include a multi-track NLE timeline editor or direct social-media uploading.

---

## Reference Audit & Licensing

See [docs/THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md) for third-party dependency licenses and [autoclip_studio/assets/fonts/poppins/OFL.txt](autoclip_studio/assets/fonts/poppins/OFL.txt) for the bundled Poppins SIL Open Font License.
