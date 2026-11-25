# AutoClip Studio

AutoClip Studio is a Windows-native desktop application that turns a YouTube video or local video into ranked, captioned short-form clips. It is local-first, has no watermarks or application quotas, and does not require Docker, WSL, Ollama, a VM, or a paid service.

The media pipeline runs on your PC. An AI API is optional: without a key, AutoClip Studio uses a deterministic transcript-density and hook scorer; with a key, it sends timestamped transcript text to Gemini, OpenAI, or an OpenAI-compatible endpoint such as Appverse.

## Quick start

### Packaged Windows application

Open `dist\AutoClipStudio\AutoClipStudio.exe` directly. Keep its `_internal` folder beside the executable. Alternatively, run `release\AutoClipStudio-Setup-x64.exe` to install for the current Windows user with a Start Menu shortcut and an optional desktop shortcut. Uninstall from **Settings → Apps → Installed apps**. User settings, credentials, exports and caches survive uninstall.

### Source checkout

Requirements: Windows 11, an internet connection for initial setup/model downloads, and approximately 2 GB of free disk space. An NVIDIA GPU is optional.

1. Double-click `AutoClipStudio.exe` in the checkout. If dependencies need setup, accept the dialog. Setup installs a project-local Python environment and FFmpeg and runs the unit tests.
2. Optionally open **AI Provider Settings**, enter a key, choose or load a model, test the connection, and save. Keys are stored in Windows Credential Manager.
3. Open **New Project**, paste a YouTube URL or choose a local video, configure the clip settings, then click **Generate Shorts**.
4. Follow progress in **Processing Queue** and preview results in **Generated Clips**.

The source launcher uses the checkout's `.venv` and loads the latest Python source on every launch. Close the app normally or run `AutoClipStudio.exe --stop` to request shutdown for that checkout. The launcher owns its process tree and cleans up remaining children when it exits. Use `--check` to validate dependencies or `--setup` to reopen setup. Launcher diagnostics are in `logs\launcher.log` under the app data directory.

## Workflow

The implemented workflow is:

1. Validate/import a local file or download one YouTube video with yt-dlp.
2. Transcribe locally with Python Faster-Whisper or Purfview XXL, with configurable word timestamps, CUDA, and CPU fallback.
3. Analyze timestamped transcript chunks with the configured provider, or use the no-key local scorer.
4. Sanitize, rank, and remove overlapping highlights.
5. Run OpenCV face detection only over selected highlight ranges and smooth the crop motion.
6. Create ASS karaoke events from Whisper word timings.
7. Reframe, scale, caption, and encode with FFmpeg. NVENC is tried first when enabled and automatically falls back to libx264.
8. Probe every result for a valid video stream and duration, persist clip metadata, write a project manifest, and remove successful job temporary files.

Downloads and transcripts are cached when enabled. Heavy jobs are sequential and execute outside the GUI thread. Cancelling or closing the app terminates application-owned subprocess trees.

## Speech-to-Text

Auto can reuse an existing Subtitle Edit Purfview XXL installation and compatible models. Choose **Test Engine** to see engine compatibility, CUDA availability, and available models. The same page lets you select another executable or model folder, import an extracted standalone release, download models, and manage the transcript cache. **Auto** batching uses sequential inference. Choose **Export transcript JSON and SRT beside the clips** to keep a copy with the project. [Speech-to-text audit and benchmark](docs/speech-to-text-audit.md)

## Providers

- **Gemini** (default): base URL `https://generativelanguage.googleapis.com/v1beta`; model `gemini-2.5-flash`.
- **OpenAI**: base URL `https://api.openai.com/v1`; model `gpt-4o-mini`.
- **Custom / Appverse**: enter the service's OpenAI-compatible `/v1` base URL and model name.

Model discovery uses each provider's model endpoint. Some compatible services do not implement model listing; in that case, type the model name directly. Custom remote endpoints must use HTTPS; HTTP is accepted only for localhost.

Gemini's optional multimodal checkbox uploads the source through the Gemini Files API, waits for processing, requests timestamped highlights, then deletes the remote file. It is off by default. Transcript-only analysis is cheaper and more private.

Provider requests retry temporary gateway and overload errors up to five attempts per request and model. Temporary rate limits follow the provider's Retry-After header when supplied. Zero allocation and exhausted billing quota stop retries for that model. Under **AI Provider Settings**, enter comma-separated fallback model IDs for the selected provider and enable **Automatically try fallback models** to use them in order.

When provider analysis fails, the project remains in **Processing Queue** with its progress and prepared transcript. To switch providers, choose the provider and model in **AI Provider Settings** and click **Save settings**. Select the failed project and click **Retry AI Analysis**. The retry uses the active saved provider, model, credentials, and fallbacks while reusing the prepared video, transcript, and clip settings. Providers other than Gemini use transcript analysis.

The queue log supports mouse selection, Ctrl+A, Ctrl+C, and a right-click menu. **Copy Logs** copies all entries in the panel, **Export Logs** saves them as a TXT file, and **Clear Logs** clears the panel. Entries include timestamps and severity. Provider diagnostics are redacted before display and storage.

## Local data

By default, application data is stored below `%LOCALAPPDATA%\AutoClip\AutoClip Studio` (the exact platform directory is supplied by `platformdirs`):

- `autoclip.db` — projects and generated clip metadata
- `settings.json` — non-secret preferences
- `cache` — optional downloads, transcript JSON, and in-progress work
- `cache\models` — Faster-Whisper model weights. Set `HF_HUB_CACHE` to reuse an existing Hugging Face cache.
- `logs\autoclip.log` — rotating startup, project, and error diagnostics
- `output` — project folders, MP4 clips, and `project.json` manifests

Set `AUTOCLIP_DATA_DIR` before launch to use another data root, or choose an output folder in Settings.

## Repository layout

| Location | Contents |
| --- | --- |
| [autoclip_studio/](autoclip_studio/) | Application logic, media pipeline, providers, and Windows process management |
| [autoclip_studio/ui/](autoclip_studio/ui/) | PySide6 views, progress panels, and shared stylesheet |
| [scripts/](scripts/) | Python dependency checks, setup, and bootstrap pins |
| [packaging/](packaging/) | Native launcher source, Python build script, installer, PyInstaller spec, and icon generator |
| [tests/](tests/) | Unit tests and media smoke tests |
| [docs/](docs/) | Audits, benchmarks, and third-party notices |

The source launcher is in the repository root. Local tools and generated builds go in `tools/`, `build/`, `dist/`, and `release/`. Git ignores these directories.

## Development and validation

Run the Python build script from a Windows x64 checkout. Its default output is the small source launcher. Rebuild it when changing the C# launcher or its icon. The optional OneDir build includes its own runtime, Qt plugins, FFmpeg, ffprobe, yt-dlp, and Deno.

Fresh setups install the SHA-256-verified FFmpeg 9.1 Windows Essentials archive. The source checkout uses the Python yt-dlp version in `requirements.txt` when there is no project-local yt-dlp executable. OneDir uses the version pinned alongside Deno in `packaging/build.py`.

Installer compilation requires Inno Setup 6 or 7. Use `--iscc "C:\path\to\ISCC.exe"` to specify its compiler. The executable and installer are unsigned.

```powershell
.\.venv\Scripts\python.exe packaging/build.py
.\.venv\Scripts\python.exe packaging/build.py --standalone
.\.venv\Scripts\python.exe packaging/build.py --installer
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m tests.integration_smoke
```

The render smoke test creates a synthetic audio/video fixture, burns in captions, and validates the resulting MP4. The full pipeline smoke test also accepts a local speech video. See the [optimization audit and validation results](docs/optimization-plan.md).

## Known limitations

- Provider connections and Gemini multimodal upload require the user's own credentials and therefore were implemented but not live-tested in this keyless development environment.
- Haar-cascade tracking favors the largest frontal face. Profiles, rapid cuts, screen recordings, and multi-speaker compositions may work better with face tracking disabled or with manual editing after export.
- The first transcription with a given Whisper model downloads model weights. `small` is the balanced default; `tiny`/`base` use less RAM, while `medium` and `large-v3` need significantly more memory.
- YouTube availability depends on yt-dlp and YouTube behavior. DRM-protected, private, age-restricted, or region-blocked sources may require user-provided access/cookies, which this GUI does not currently expose.
- The app generates clips and metadata but does not include a timeline editor or social-network uploader.

## Reference audit and licensing

The accessible `Anil-matcha/AI-Youtube-Shorts-Generator` repository is MIT-licensed. Its patterns for transcript caching, chunk overlap, score-based deduplication, and local face-aware crop were reviewed and independently implemented here. The OpenShorts repository and Rayneth article endpoint were unavailable during the November 25, 2026 audit. [Third-party notices and dependency licenses](docs/THIRD_PARTY_NOTICES.md)
