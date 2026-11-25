# Native Windows speech-to-text audit

November 26, 2026

AutoClip Studio keeps Python Faster-Whisper as its portable backend and can run an existing Subtitle Edit Purfview XXL engine as a cancellable Windows subprocess. Auto prefers Purfview for GPU transcription when the executable passes the capability check and compatible selected weights are already available. The default project model remains small, and sequential inference remains the automatic batch setting.

## Implementation decisions

- Subtitle Edit separates executable discovery from model discovery and recognizes CTranslate2 weights under the Purfview model root. AutoClip Studio checks the requested AppData installation, permits explicit paths, and can import a standalone extracted release into its own data directory. Import excludes the other application's model directory. [Subtitle Edit executable and engine selection](https://github.com/SubtitleEdit/subtitleedit/blob/main/src/libuilogic/AudioToText/WhisperHelper.cs) · [Purfview model discovery](https://github.com/SubtitleEdit/subtitleedit/blob/main/src/libuilogic/AudioToText/WhisperPurfviewFasterWhisperModel.cs)
- The detected executable reported Faster-Whisper 1.1.1, identified its XXL build as r245.4 during inference, and returned one CUDA device. The checked help supports explicit device, compute type, model root, word timestamps, VAD, native JSON, and progress output. This build rejected an absolute model path. AutoClip Studio stages the compatible model name using a shared filesystem link for the weights and small private copies of the metadata. Engine working files, downloaded weights, and JSON output stay in AutoClip Studio or temporary paths. [Purfview upstream releases and engine information](https://github.com/Purfview/whisper-standalone-win/releases)
- Both engines receive the original video and decode the audio stream directly. AutoClip Studio probes audio and video start times and translates transcript timestamps onto the video timeline. Validation bounds segment and word times and reports low-confidence repetition or suspected silence hallucinations while retaining the text. Captions animate real word timestamps and use segment timing when word timestamps are disabled. Optional export writes structured JSON and SRT beside the clips. [Faster-Whisper decoding, word timestamps and VAD](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/README.md)
- The RTX 3050's free VRAM guides automatic compute selection and caps manual batches. An allocation or CUDA runtime failure can retry with sequential INT8-FP16, then CPU if enabled. Python unloads the CTranslate2 model before returning. Purfview exits before the pipeline advances. The existing queue serializes rendering and transcription. The progress panel uses Purfview's reported counters for percentage and ETA. Python displays the latest speech position and observed speed with percentage and ETA unavailable during inference. [Faster-Whisper batched inference](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/README.md)
- Transcript cache keys include the normalized source path, size, modification time, sampled source bytes, engine version and model identity, language, compute, batching, VAD, and word timestamp options. Existing cache files and failed-analysis checkpoints remain on disk. The cache action only removes reusable transcript entries.
- WhisperX was reviewed for optional wav2vec2 phoneme alignment. Its language-specific alignment models and PyTorch/CUDA dependencies would add a separate runtime to this Windows configuration. The current app uses its engines' native word timings. [WhisperX alignment and limitations](https://github.com/m-bain/whisperX#limitations-)

## Benchmarks

Windows 11, Ryzen 5 6600H, 15.3 GiB usable RAM, RTX 3050 Laptop GPU with 6,144 MiB, NVIDIA driver 621.40. The same video fixture yielded 76.719 seconds of decoded audio in the Python baseline. The matched large-v2 runs used the same Subtitle Edit model weights, English transcription, beam size five, VAD and word timestamps, with sequential decoding. Model weights were already on disk. The CPU baseline was measured before the pipeline changes.

| Configuration | Wall time | RTF | Peak process RAM | Peak GPU-wide VRAM |
| --- | ---: | ---: | ---: | ---: |
| Existing Python 1.2.1 / CTranslate2 4.9.1, large-v2 CPU INT8 | 93.49 s | 1.219 | 2,494 MiB | CPU |
| Purfview large-v2 CUDA FP16 | 19.90 s | 0.259 | 3,415 MiB | 4,195 MiB |
| Purfview large-v2 CUDA INT8-FP16 | 27.56 s | 0.359 | 3,578 MiB | 2,275 MiB |
| Integrated Purfview large-v3-turbo CUDA FP16 | 22.34 s | 0.291 | 1,990 MiB | 2,403 MiB |
| Integrated Purfview large-v3-turbo CUDA INT8-FP16 | 10.42 s | 0.136 | 1,939 MiB | 1,411 MiB |

RTF is wall time divided by audio duration. Purfview memory is the sampled external-engine working set, and GPU memory is the sampled total device usage. The large-v2 GPU rows are direct engine runs. The turbo rows include app orchestration and use different weights from the large-v2 baseline. These are individual runs in execution order with different OS and engine startup states. The Python CPU constructor took 18.17 seconds. Purfview did not report model loading as a separate duration. Its first large-v2 progress arrived at 16.51 seconds for FP16 and 24.47 seconds for INT8-FP16. The integrated turbo runs reached the inference-stage message after 15.83 and 5.36 seconds respectively. The reported inference rates were 15.72× and 19.67× for turbo, and 6.78× and 7.23× for large-v2.

An additional integrated large-v2 Auto run took 30.94 seconds, including first-run engine capability checks. Its repeat transcript-cache hit took 0.32 seconds. The retained Python tiny CPU path completed in 4.56 seconds, then returned a transcript-cache hit in 0.016 seconds. These cache checks are separate from model performance comparisons.

### Timestamp and transcript comparison

Repeated passages were matched one-to-one by normalized word and nearest start within one second to avoid aligning to the wrong passage. All four GPU runs matched 213 of 214 baseline words. The remaining baseline word was an extra adjacent “work.” Median and 95th-percentile absolute start-time differences against the Python baseline were 0.00 / 0.04 s for both large-v2 runs, 0.02 / 0.32 s for turbo FP16, and 0.02 / 0.35 s for turbo INT8-FP16. All four GPU transcripts had ordered word starts. The comparison measures agreement with the baseline. Hand-labeled WER and timing accuracy were outside this benchmark.

A separate synthetic A/V fixture delayed its speech stream by 3 seconds. The muxed stream began at 2.954 seconds, the validated first segment began at the same video time, and all word ends remained within the video. Nonlinear timestamp discontinuities and larger multilingual quality datasets need separate validation. Batching is available as an explicit setting. These runs evaluated sequential inference.

## Validation

All 42 tests passed. A native PySide6 session at 150% scaling opened Speech-to-Text at the minimum window width and used Test Engine to detect Purfview CUDA and the shared large-v2 weights. A no-key source run used Purfview on CUDA, completed a project with one validated clip, and wrote JSON and SRT transcripts. The process manager also stopped a separate active Purfview run. The PyInstaller OneDir build launched and exited normally. Automated invocation of its Speech-to-Text settings did not complete, so the live settings check was performed in the native source application.

## Files and compatibility

- [Engine discovery, capability checks, weights and CUDA policy](../autoclip_studio/speech_engine.py) · [Decoding orchestration, timestamp checks and persistence](../autoclip_studio/transcription.py)
- [Settings and progress widgets](../autoclip_studio/ui/speech_view.py) · [Main window](../autoclip_studio/gui.py) · [Settings storage](../autoclip_studio/config.py) · [Pipeline](../autoclip_studio/pipeline.py) · [Worker](../autoclip_studio/worker.py)
- [Media timing](../autoclip_studio/media.py) · [Captions](../autoclip_studio/captions.py) · [Regression tests](../tests/test_transcription_engines.py)
- Python cancellation is cooperative between model and decoding calls. The Windows process manager can terminate active Purfview inference and its child processes. Sharing a model across volumes requires a filesystem link the current Windows account can create. Python can read the selected model directly when such a link is unavailable.
