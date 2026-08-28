# Multimodal `input_audio` format probe — live verification

Wish `fallback-texto`, Group 2, Deliverable 8. Date: 2026-08-10.

Purpose: the mocked unit suite can only prove the *shape* of the request that leaves the process.
This document records what the real provider endpoints answered when handed that shape, and in
particular whether they accept the **OGG/Opus** container that WhatsApp actually delivers
(`webhook.py` defaults the audio mimetype to `"audio/ogg"`).

No credential value appears in this file. Only request shape and response bodies are recorded.
Keys were loaded from the untracked, gitignored `.env` (`set -a; . ./.env; set +a`).

---

## Verdict

| Provider | Endpoint | OGG/Opus (WhatsApp) | WAV | Verdict |
|---|---|---|---|---|
| **openrouter** | `https://openrouter.ai/api/v1/chat/completions` | **accepted, transcribed verbatim** | accepted | **OPERATIONAL** |
| openai | `https://api.openai.com/v1/chat/completions` | rejected twice (enum + content sniff) | accepted | not usable for voice notes |
| groq | `https://api.groq.com/openai/v1/chat/completions` | rejected — `input_audio` block itself is not in the schema | rejected, same reason | incompatible |

`OPERATIONAL_MULTIMODAL_PROVIDERS = frozenset({"openrouter"})` — exactly the row proven to accept
WhatsApp audio end to end.

---

## Test payloads

Two payloads were used. There is **no `ffmpeg` on the machine** (`which ffmpeg` is empty), so the
OGG/Opus file was produced with `libsndfile 1.2.2` via the `soundfile` wheel, installed in a
throwaway venv at `/tmp/probevenv` (the project's Poetry environment and lockfile were not touched).

Source audio: `https://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_0010_8k.wav`
(Harvard sentences, list 1, 8 kHz mono PCM16, HTTP 200, 538 014 bytes). First 5.0 s were cut and
written twice:

```python
import soundfile as sf
data, sr = sf.read('/tmp/speech.wav', dtype='float32')   # sr = 8000
clip = data[:int(sr * 5.0)]
sf.write('/tmp/probe_speech.wav',  clip, sr, subtype='PCM_16')          #  80 044 bytes
sf.write('/tmp/probe_speech.opus', clip, sr, format='OGG', subtype='OPUS')  # 14 061 bytes
```

**The OGG/Opus file was verified to be genuinely decodable before being used**, so that any
rejection could be attributed to the provider and not to a malformed file:

```
$ head -c 48 /tmp/probe_speech.opus | xxd | head -3
00000000: 4f67 6753 0002 0000 0000 0000 0000 7c4a  OggS..........|J
00000010: 1d72 0000 0000 2c51 78f6 0113 4f70 7573  .r....,Qx...Opus
00000020: 4865 6164 0101 3801 401f 0000 0000 004f  Head..8.@......O

$ sf.info('/tmp/probe_speech.opus')
samplerate: 8000 Hz / channels: 1 / duration: 5.000 s
format: OGG (OGG Container format) [OGG] / subtype: Opus [OPUS]

decoded rms 0.0567
energy-envelope correlation vs. source WAV: 0.9996
```

`OggS` magic + `OpusHead` identification header, decodes back to 5.000 s of audio that correlates
at 0.9996 with the source. This is a real Opus stream, not a hand-rolled file.

Request body sent to every endpoint (the exact shape `transcribe_audio_multimodal` emits):

```json
{
  "model": "<model>",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "Transcribe the speech in this audio verbatim. If there is no speech, describe the sound in one word."},
      {"type": "input_audio", "input_audio": {"data": "<base64 of the file>", "format": "<format>"}}
    ]
  }]
}
```

---

## 1. OpenRouter — `https://openrouter.ai/api/v1/chat/completions`

### 1a. OGG/Opus, `google/gemini-2.5-flash`, `format: "ogg"` — **the decisive result**

- source mimetype: `audio/ogg` (WhatsApp's default), file `/tmp/probe_speech.opus`, 14 061 bytes
- `format` sent: `"ogg"`
- result: **HTTP 200**

```json
{
  "id": "gen-1786410424-sf0W9kI9PA9ienCKsoOi",
  "model": "google/gemini-2.5-flash",
  "provider": "Google",
  "choices": [{
    "index": 0,
    "finish_reason": "stop",
    "message": {"role": "assistant", "content": "The birch canoe slid on the smooth planks. Glue the sheets"}
  }],
  "usage": {
    "prompt_tokens": 147, "completion_tokens": 12, "total_tokens": 159,
    "cost": 0.0001616,
    "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 125, "video_tokens": 0}
  }
}
```

The returned text is the verbatim first Harvard sentence of the source recording, and
`prompt_tokens_details.audio_tokens: 125` shows the Opus stream was actually decoded. **OGG/Opus
works end to end. No transcoding, and therefore no ffmpeg runtime dependency, is required for
Group 3.**

### 1b. Same OGG file with `format: "opus"` and `format: "wav"`

- result: **HTTP 200** in both cases, identical content
  (`"The birch canoe slid on the smooth planks. Glue the sheets"`, `audio_tokens: 125`,
  cost 0.0001616 each).

Interpretation: on the Gemini route OpenRouter sniffs the container rather than trusting the
`format` label. That is *not* a licence to send a wrong label — see 2a, where the OpenAI route is
strict about it — so the adapter still maps mimetype → format honestly.

### 1c. OGG/Opus on a second audio model, `mistralai/voxtral-small-24b-2507`, `format: "ogg"`

- result: **HTTP 200**

```json
{
  "id": "gen-1786410480-FIn1mK0He3wwUvNGFWem",
  "model": "mistralai/voxtral-small-24b-2507",
  "provider": "Mistral",
  "choices": [{"message": {"content": "The audio contains speech. Here is the transcription:\n\n\"The birch canoe slid on the smooth planks.\""}}]
}
```

OGG support is therefore not a single-model accident on OpenRouter. (An earlier attempt at this
model returned HTTP 400 `"top_p must be 1 when using greedy sampling"` because the probe sent
`temperature: 0`; the adapter does not send `temperature`, and the retry without it succeeded.)

### 1d. Earlier WAV baseline (probe run by the orchestrator, carried over verbatim)

> **OpenRouter WORKS.** `POST https://openrouter.ai/api/v1/chat/completions` with
> `google/gemini-2.5-flash` accepted the same WAV and answered `"Tone"` for a 440 Hz sine, with
> `usage.prompt_tokens_details.audio_tokens: 25` proving the audio was really decoded. Cost
> $0.0000317. OpenRouter exposes 36 models whose `architecture.input_modalities` includes `audio`,
> including `google/gemini-*`, `openai/gpt-audio`, `openai/gpt-audio-mini`,
> `mistralai/voxtral-small-24b-2507`.

---

## 2. OpenAI — `https://api.openai.com/v1/chat/completions`

**No direct `OPENAI_API_KEY` exists on this machine** (`.env` holds `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `LLM_API_KEY` for `api.neuralake.cloud`, and no OpenAI key). OpenAI was
therefore reached through OpenRouter's OpenAI route with the model `openai/gpt-audio-mini`. The
responses below are OpenAI's own error bodies passed through verbatim by OpenRouter
(`provider_name: "OpenAI"`, `param: "messages[0].content[1].input_audio.format"`, and on the
success control an OpenAI `system_fingerprint`), so they characterise OpenAI's validation, not
OpenRouter's. A direct-key confirmation is still the stronger evidence and remains untaken.

### 2a. OGG/Opus, `format: "ogg"`

- source mimetype: `audio/ogg`; `format` sent: `"ogg"`
- result: **HTTP 400**

```json
{"error": {
  "message": "Invalid value: 'ogg'. Supported values are: 'wav' and 'mp3'.",
  "type": "invalid_request_error",
  "param": "messages[0].content[1].input_audio.format",
  "code": "invalid_value"
}}
```

### 2b. Same OGG/Opus bytes mislabelled `format: "wav"` — closes the loophole

- source mimetype: `audio/ogg`; `format` sent: `"wav"` (deliberately wrong)
- result: **HTTP 400**

```json
{"error": {
  "message": "This model does not support the format you provided.",
  "type": "invalid_request_error",
  "param": "messages",
  "code": "unsupported_format"
}}
```

So OpenAI rejects OGG both at the enum and after sniffing the bytes. There is no label trick that
gets a WhatsApp voice note into OpenAI.

### 2c. Control — real WAV, `format: "wav"`

- source mimetype: `audio/wav`, file `/tmp/probe_speech.wav`, 80 044 bytes; `format` sent: `"wav"`
- result: **HTTP 200**

```json
{
  "model": "openai/gpt-audio-mini",
  "provider": "OpenAI",
  "system_fingerprint": "fp_3dcc55d19f",
  "choices": [{"message": {"content": "The audio contains speech. Here is the transcription:\n\n\"The birch canoe slid on the smooth planks.\""}}],
  "usage": {"prompt_tokens": 84, "completion_tokens": 21, "prompt_tokens_details": {"audio_tokens": 50}}
}
```

The control proves the model, the route and the prompt are all fine, and isolates the failures in
2a/2b to the OGG container alone.

**Consequence:** OpenAI could serve this adapter only for audio that is already WAV or MP3.
WhatsApp never delivers that, so `openai` is excluded from `OPERATIONAL_MULTIMODAL_PROVIDERS`.
`ACCEPTED_FORMATS_BY_KIND["multimodal_openai"] = {"wav", "mp3"}` records the capability faithfully,
so the adapter refuses locally instead of burning a request on a guaranteed 400.

---

## 3. Groq — `https://api.groq.com/openai/v1/chat/completions`

### 3a. Orchestrator's WAV probe (carried over verbatim)

> **Groq is INCOMPATIBLE. Ruled out.** `POST https://api.groq.com/openai/v1/chat/completions`
> rejects the `input_audio` content block at schema validation:
>
> ```
> 'messages.0.content.1.type' : value is not one of the allowed values ['text']
> ```
>
> Reproduced on `openai/gpt-oss-120b`, `groq/compound` and `qwen/qwen3.6-27b`, sending
> `format: "wav"` with a valid 4044-byte 8kHz mono PCM WAV. The rejection is at the *content-type*
> level, not the audio format, so no mimetype mapping can rescue Groq. Groq's only audio models are
> `whisper-large-v3` and `whisper-large-v3-turbo`, which are STT endpoints at
> `/audio/transcriptions`, not chat multimodal. This is a property of the Groq API, not of the
> account.

### 3b. OGG/Opus confirmation (this group — the OGG question was still open for Groq)

- model `openai/gpt-oss-120b`; source mimetype `audio/ogg`; `format` sent: `"ogg"`
- result: **HTTP 400**

```json
{"error": {
  "message": "'messages.0' : for 'role:user' the following must be satisfied[('messages.0.content' : one of the following must be satisfied[('messages.0.content' : value must be a string) OR ('messages.0.content.1' : one of the following must be satisfied[('messages.0.content.1.type' : value is not one of the allowed values ['text']) OR ('messages.0.content.1.type' : value is not one of the allowed values ['image_url']) OR ('messages.0.content.1.type' : value is not one of the allowed values ['document'])])])]",
  "type": "invalid_request_error"
}}
```

Groq's chat schema admits only `text`, `image_url` and `document` content parts. The container is
irrelevant — the `input_audio` block never gets to be inspected.

(A first attempt returned HTTP 403 `error code: 1010`, a Cloudflare bot-rule rejection of the
probe's default `urllib` User-Agent, not a schema answer. The run above set a normal User-Agent and
got Groq's real 400.)

---

## Decisions this evidence drives

1. **`OPERATIONAL_MULTIMODAL_PROVIDERS = frozenset({"openrouter"})`**
   (`src/services/api_credentials.py`). Exactly the providers proven to accept WhatsApp audio.
   Group 4 must therefore only ever show `Ativo` for OpenRouter.

2. **No transcoding, no ffmpeg.** Because OpenRouter takes OGG/Opus directly, Group 3 forwards the
   downloaded bytes untouched. The wish does not grow an ffmpeg runtime dependency.

3. **The `multimodal_groq` egress policy is kept, not dropped**, with `api.groq.com` pinned. The
   wish specifies all three policies and the reason to keep this one is not aspirational: `groq` is
   still a *selectable* value in `PROVIDER_ALLOWLISTS["multimodal"]`, so a clinic can persist it
   today. If any future code path reaches the adapter with a Groq URL, a missing policy would make
   `resolve_policy` raise a generic `unsafe_api_url`, while an unpinned one would let a
   `groq`-labelled credential aim at an arbitrary host. A pinned policy plus
   `ACCEPTED_FORMATS_BY_KIND["multimodal_groq"] = frozenset()` gives the honest behaviour: the host
   can only ever be `api.groq.com`, and the call is refused locally with
   `provider_not_multimodal_capable` before a request is spent. Removing `groq` from the credential
   allowlist would be the cleaner end state, but that contract is Group 4's, not this group's.

4. **`format` is derived from the mimetype, never hardcoded.** OpenRouter tolerates a wrong label
   on the Gemini route (1b) but OpenAI does not (2a), so the mapping is real behaviour, not
   ceremony.

## Reproduction

The probe script lived at `/tmp/probe_multimodal.py` and is not committed: it takes a bearer token
from the environment and is a throwaway. To repeat:

```bash
set -a; . ./.env; set +a
python3 /tmp/probe_multimodal.py openrouter google/gemini-2.5-flash /tmp/probe_speech.opus ogg
python3 /tmp/probe_multimodal.py openrouter openai/gpt-audio-mini  /tmp/probe_speech.opus ogg
python3 /tmp/probe_multimodal.py groq       openai/gpt-oss-120b    /tmp/probe_speech.opus ogg
```

Total spend across every call in this document: well under one US cent.
