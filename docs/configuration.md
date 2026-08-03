# Configuration

Every setting is an environment variable, set in your shell or in a `.env`
file at the repo root. `.env` is loaded at startup (a real shell variable
always wins over `.env`); changes need a server restart, because most
config is read once at import.

## Model

| Variable      | Default                        | Description |
| ------------- | ------------------------------ | ----------- |
| `MODEL`       | `e4b`                          | Gemma 4 size: `e2b` (fastest), `e4b` (better answers, ~1.8x e2b latency), `12b` (needs ~8GB and llama.cpp b9512+) |
| `MODEL_PATH`  | auto-download from HuggingFace | Path to a local Gemma 4 `.gguf` file (overrides `MODEL`) |
| `MMPROJ_PATH` | auto-download from HuggingFace | Path to the matching `mmproj` `.gguf` (audio + vision encoders) |

## Server

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `PORT`   | `8000`  | Web server port (serves on `localhost` only — browsers require a secure context for mic/camera) |

## llama.cpp

| Variable           | Default         | Description |
| ------------------ | --------------- | ----------- |
| `TEMPERATURE`      | `0.7`           | Sampling temperature for speech (0 = deterministic; the action decider always runs at 0) |
| `LLAMA_CTX`        | `16384`         | Context size. The server drops the oldest exchanges shortly before it fills |
| `LLAMA_PORT`       | `8081`          | Port for the spawned llama-server |
| `LLAMA_SERVER_URL` | (spawn our own) | Use an already-running llama-server instead of spawning one |

## Background research

Off entirely unless `REASONER_API_KEY` is set — without it Parlor stays
fully on-device. The default endpoint is OpenRouter; any OpenAI-compatible
chat-completions endpoint works, including a second local llama-server,
and api.openai.com is handled specially (its reasoning models require
`max_completion_tokens`, sent automatically). Note that web search is an
OpenRouter mechanism (the `:online` model suffix): on other endpoints
research still runs, but without live web access. When pointing directly
at a provider, use that provider's model id (e.g. `gpt-5.6-luna`, not
`openai/gpt-5.6-luna`).

| Variable              | Default                        | Description |
| --------------------- | ------------------------------ | ----------- |
| `REASONER_API_KEY`    | (unset — research off)         | API key for the endpoint |
| `REASONER_BASE_URL`   | `https://openrouter.ai/api/v1` | Any OpenAI-compatible chat endpoint |
| `REASONER_MODEL`      | `openai/gpt-5.6-luna`          | Model the endpoint should run |
| `REASONER_WEB_SEARCH` | `1`                            | On OpenRouter, append `:online` for provider-side web search |
| `REASONER_TIMEOUT`    | `90`                           | Seconds before a background task fails (and is delivered as a spoken apology) |

## Behavior

| Variable          | Default | Description |
| ----------------- | ------- | ----------- |
| `TIME_NOTE_MIN_S` | `120`   | Seconds of quiet before the next turn tells the model how long the silence was |

## Practice scenes

Pick a scene and Parlor role-plays it, speaking the first line itself. The
catalogue ships with 24 hand-written scenes across eight categories; the
"More scenes" button invents more with the same local model and caches them
to disk — the only file Parlor ever writes. A corrupt or unreadable cache
is ignored rather than fatal, so deleting it is always safe.

| Variable              | Default                        | Description |
| --------------------- | ------------------------------ | ----------- |
| `COACH_LEVEL`         | `intermediate`                 | Starting English level: `beginner`, `intermediate`, `advanced`, or `off` for no coaching |
| `PARLOR_TOPICS_CACHE` | `~/.cache/parlor/topics.json`  | Where generated scenes are kept (honours `XDG_CACHE_HOME`); capped at 60, oldest dropped |

## Avatar

The face is rendered in the browser and lip-synced from the TTS audio
already playing, so it adds nothing to Parlor's latency — but it does share
the machine's GPU with llama.cpp and Kokoro. If that shows up in your
time-to-first-audio, `AVATAR_BACKEND=canvas` costs almost nothing to draw.

No model is downloaded for you: export any VRM 0.x or 1.0 character (VRoid
Studio is free and exports VRM 1.0) and drop it at
`src/parlor/web/static/avatar/model.vrm`. Without one — or if the CDN is
unreachable — the 2D face takes over automatically.

| Variable          | Default                     | Description |
| ----------------- | --------------------------- | ----------- |
| `AVATAR_BACKEND`  | `vrm`                       | `vrm` (3D character), `canvas` (drawn 2D face, no dependencies), `none` (no avatar) |
| `AVATAR_VRM_URL`  | `/static/avatar/model.vrm`  | Where to load the VRM from; any URL works |

## TTS

| Variable      | Default | Description |
| ------------- | ------- | ----------- |
| `KOKORO_ONNX` | (unset) | Set to force the ONNX (CPU) TTS backend on Apple Silicon instead of MLX — a debugging escape hatch |

## Testing

| Variable          | Default            | Description |
| ----------------- | ------------------ | ----------- |
| `PARLOR_TEST_URL` | (spawn our own)    | Point the e2e suite at an already-running server, e.g. `ws://localhost:8000/ws` |
