# AutoClip Studio

AutoClip Studio is a Windows-native desktop application that turns a YouTube video or local video into ranked, captioned short-form clips. It is local-first, has no watermarks or application quotas, and does not require Docker, WSL, Ollama, a VM, or a paid service.

The media pipeline runs on your PC. An AI API is optional: without a key, AutoClip Studio uses a deterministic transcript-density and hook scorer; with a key, it sends timestamped transcript text to Gemini, OpenAI, or an OpenAI-compatible endpoint such as Appverse.

## Quick start

Requirements: Windows 11, an internet connection for initial setup/model downloads, and approximately 2 GB of free disk space. An NVIDIA GPU is optional.

1. Double-click `SETUP.bat` once. It downloads an isolated Python 3.12 runtime to `.runtime`, creates `.venv`, installs pinned packages, downloads FFmpeg into `tools`, and runs the unit tests. It does not alter the system Python or PATH.
2. Double-click `START.bat`.
3. Optionally open **AI Provider Settings**, enter a key, choose/load a model, test the connection, and save. Keys are stored in Windows Credential Manager; they are never written to the settings JSON.
4. Open **New Project**, paste a YouTube URL or choose a local video, configure the clip settings, then click **Generate Shorts**.
5. Follow progress in **Processing Queue** and preview results in **Generated Clips**.

`STOP.bat` reads the application-owned PID file, verifies that it belongs to this workspace, and terminates that process tree. Normal window closure already cancels and terminates owned subprocesses.

## Workflow

The implemented workflow is:

1. Validate/import a local file or download one YouTube video with yt-dlp.
2. Transcribe locally with Faster-Whisper and word timestamps. CUDA is attempted when selected/available and automatically retried on CPU if runtime libraries are unavailable.
3. Analyze timestamped transcript chunks with the configured provider, or use the no-key local scorer.
4. Sanitize, rank, and remove overlapping highlights.
5. Run OpenCV face detection only over selected highlight ranges and smooth the crop motion.
6. Create ASS karaoke events from Whisper word timings.
7. Reframe, scale, caption, and encode with FFmpeg. NVENC is tried first when enabled and automatically falls back to libx264.
8. Probe every result for a valid video stream and duration, persist clip metadata, write a project manifest, and remove successful job temporary files.

Downloads and transcripts are cached when enabled. Heavy jobs are sequential and execute outside the GUI thread. Cancelling or closing the app terminates application-owned subprocess trees.

## Providers

- **Gemini** (default): base URL `https://generativelanguage.googleapis.com/v1beta`; model `gemini-2.5-flash`.
- **OpenAI**: base URL `https://api.openai.com/v1`; model `gpt-4o-mini`.
- **Custom / Appverse**: enter the service's OpenAI-compatible `/v1` base URL and model name.

Model discovery uses each provider's model endpoint. Some compatible services do not implement model listing; in that case, type the model name directly. Custom remote endpoints must use HTTPS; HTTP is accepted only for localhost.

Gemini's optional multimodal checkbox uploads the source through the Gemini Files API, waits for processing, requests timestamped highlights, then deletes the remote file. It is off by default. Transcript-only analysis is cheaper and more private.

## Local data

By default, application data is stored below `%LOCALAPPDATA%\AutoClip\AutoClip Studio` (the exact platform directory is supplied by `platformdirs`):

- `autoclip.db` — projects and generated clip metadata
- `settings.json` — non-secret preferences
- `cache` — optional downloads, transcript JSON, and in-progress work
- `output` — project folders, MP4 clips, and `project.json` manifests

Set `AUTOCLIP_DATA_DIR` before launch to use another data root, or choose an output folder in Settings.

## Development and validation

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m tests.integration_smoke
```

The build was validated on the target machine with:

- 7 unit tests covering URL handling, highlight parsing/deduplication, local selection, caption generation, and crop expressions.
- A headless construction test of all six GUI pages and provider-dependent controls.
- A synthetic audio/video render using ASS/libass, AAC, vertical scaling, MP4 validation, and NVIDIA NVENC.
- A no-key speech-video pipeline using Faster-Whisper on CPU, local highlight selection, animated captions, NVENC rendering, SQLite persistence, manifest generation, and final ffprobe validation.

## Known limitations

- Provider connections and Gemini multimodal upload require the user's own credentials and therefore were implemented but not live-tested in this keyless development environment.
- Haar-cascade tracking favors the largest frontal face. Profiles, rapid cuts, screen recordings, and multi-speaker compositions may work better with face tracking disabled or with manual editing after export.
- The first transcription with a given Whisper model downloads model weights. `small` is the balanced default; `tiny`/`base` use less RAM, while `medium` and `large-v3` need significantly more memory.
- YouTube availability depends on yt-dlp and YouTube behavior. DRM-protected, private, age-restricted, or region-blocked sources may require user-provided access/cookies, which this GUI does not currently expose.
- The app generates clips and metadata but does not include a timeline editor or social-network uploader.

## Reference audit and licensing

The accessible `Anil-matcha/AI-Youtube-Shorts-Generator` repository is MIT-licensed. Its high-level patterns—transcript caching, chunk overlap, score-based deduplication, and local face-aware crop—were reviewed and independently implemented here. The OpenShorts repository was temporarily unavailable, and the Rayneth article endpoint could not be retrieved during the 2026-11-25 audit. See `THIRD_PARTY_NOTICES.md` for dependency licenses.
