# Provider availability receipts

- wish_slug: api-provider-availability
- governing_design_sha: 527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a
- status: IN_PROGRESS
- depends_on: api-provider-settings, api-config-clarity
- blocks: api-provider-runtime-shutdown

No pair has direct non-mocked validation in this delivery, so every displayable pair remains fail-closed.

| capability | provider | test_command | test_exit_code | test_result | validation_kind | validation_authority | validated_at | decision | displayable |
|---|---|---|---|---|---|---|---|---|---|
| whatsapp | meta_cloud | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | coming_soon | true |
| whatsapp | zapi | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | coming_soon | true |
| whatsapp | uazapi | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | coming_soon | true |
| whatsapp | evolution | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| whatsapp | dialog360 | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| whatsapp | twilio | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| llm | dropout | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| llm | openai | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| llm | outros | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| llm | slm_cortex | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| multimodal | openrouter | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | coming_soon | true |
| multimodal | groq | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | removed | false |
| multimodal | openai | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | removed | false |
| stt | outros | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| stt | openai_whisper | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| stt | elevenlabs_scribe | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| stt | google_stt | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| stt | deepgram_stt | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| stt | microsoft_stt | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |
| tts | elevenlabs | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | coming_soon | true |
| tts | google | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | coming_soon | true |
| tts | microsoft | not_run | not_run | not_run | not_available | not_available | 2026-08-30T00:00:00Z | removed | false |
