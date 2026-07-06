# Environment Variables

Use this file for setup and credential triage. Never print real values from `.env`.

## Rewrite

- `ARK_API_KEY`: fallback API key for Ark-compatible chat.
- `ARK_BASE_URL`: Ark-compatible base URL.
- `LLM_API_KEY`: preferred API key for rewrite calls.
- `LLM_BASE_URL`: OpenAI-compatible chat base URL.
- `LLM_MODEL`: rewrite model, for example `seed-2-0-pro-260328`.
- `LLM_SYSTEM_PROMPT`: optional system prompt.

## Seed Audio

- `SEED_AUDIO_API_KEY`: recommended API key for Seed Audio 1.0.
- `SEED_AUDIO_URL`: Seed Audio endpoint.
- `SEED_AUDIO_MODEL`: model id, usually `seed-audio-1.0`.
- `SEED_AUDIO_FORMAT`: output format such as `wav` or `mp3`.
- `SEED_AUDIO_SAMPLE_RATE`: output sample rate.
- `SEED_AUDIO_SPEECH_RATE`: global speech-rate adjustment.
- `SEED_AUDIO_LOUDNESS_RATE`: loudness adjustment.
- `SEED_AUDIO_PITCH_RATE`: pitch adjustment.
- `SEED_AUDIO_TIMEOUT`: HTTP timeout in seconds.
- `SEED_AUDIO_MAX_CHARS`: hard prompt splitting limit.
- `SEED_AUDIO_TARGET_CHARS`: preferred target prompt length.
- `SEED_AUDIO_EN_SPEECH_RATE`: English demo speech-rate setting.
- `SEED_AUDIO_APP_ID`: optional legacy app id.
- `SEED_AUDIO_ACCESS_KEY`: optional legacy access key.

## Reference TTS

- `TTS_API_KEY`: TTS API key for creating dry voice references.
- `TTS_URL`: TTS endpoint.
- `TTS_RESOURCE_ID`: TTS resource id.
- `TTS_APP_KEY`: TTS app key when required.
- `TTS_SPEAKER`: default speaker.
- `TTS_FORMAT`: reference voice output format.
- `TTS_SAMPLE_RATE`: reference voice sample rate.

## Optional Speaker Overrides

- `SEED_AUDIO_SPEAKER_NARRATOR`
- `SEED_AUDIO_SPEAKER_ELIAN`
- `SEED_AUDIO_SPEAKER_MARA`
- `SEED_AUDIO_SPEAKER_ARMOR`
- `SEED_AUDIO_SPEAKER_PORTRAIT`

Use these when the workflow should use fixed provider speakers rather than generated reference audio.

## Local Tools

- `FFMPEG_PATH`: optional ffmpeg binary path.
- `DOWNLOAD_TIMEOUT`: output download timeout in seconds.
- `DOWNLOAD_RETRIES`: output download retry count.

