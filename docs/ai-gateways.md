# AI gateways and action routing

Connections → **ИИ-шлюзы** contains credentials and action routes. AI agents remain in their own tab; each agent chooses a task, provider, model and prompt. Existing Yandex/DeepSeek credentials, the enabled microphone and the 15–180 second recording setting survive migration 0019. No default provider changes on upgrade.

## Connections interface

The synchronization master, service settings, gateways and agents use the same
content width, heading, card edges, spacing and light/dark theme tokens. Keep
shared layout rules scoped to `#pane-connections`; do not add a narrower fixed
width to a single tab. The four main tabs wrap into two rows on narrow screens.
The master retains its three inner views and the single synchronization monitor.

Each settings tab starts with a collapsible explanation. Service settings explain
when a separate MAX bot is needed; gateways distinguish saving/checking credentials
from assigning an action; agents explain instructions, defaults and safe tests.
These hints make no provider requests. Existing form IDs, independent save actions
and capability checks remain separate; navigation never saves a draft implicitly.

## Independent accounts

- Yandex: Russian SpeechKit recognition and AI Studio text models.
- DeepSeek: text models only. Selecting DeepSeek for an agent does not send audio to a text endpoint.
- MiMo ordinary API: `https://api.xiaomimimo.com/v1`, separate pay-as-you-go key and balance.
- MiMo Token Plan: `https://token-plan-sgp.xiaomimimo.com/v1`, separate subscription key and quota. Quota consumption is not presented as free usage; there is no automatic switch to the ordinary API.

All secrets use the existing AES-GCM vault, with workspace, purpose and gateway identity bound as authenticated data. Public settings return only the last four characters. Configure keys in the owner/admin interface; never put keys in git, deployment environment examples or command-line arguments. Replacing a key/address/protocol resets its capability checks. Deleting the selected recognition key disables recording; deleting the selected synthesis key disables synthesis. Other providers' credentials stay intact.

## Actions

1. **Recognition:** Yandex by default; MiMo/Token Plan are explicit alternatives. Russian can be selected for MiMo, but the server passes the documented `auto` language tag because MiMo's ASR API lists only Chinese and English. Russian quality is therefore experimental and must be tested on a real recording before operational use; a successful connectivity check with silence does not prove it works. Yandex accepts `ru`; MiMo accepts `auto`, `en` or `zh` at its API boundary. An incompatible language is rejected before a provider call. Browser PCM is wrapped losslessly as a mono 16 kHz WAV; the full three-minute base64 payload stays below MiMo's 10 MB limit. Existing authenticated, cancellable background jobs handle recording requests. DeepSeek receives only already transcribed text as an AI agent model, not audio; its public API does not advertise audio input.
2. **Text:** `mimo-v2.6-flash` and `mimo-v2.6-pro`, or independently credentialed Token Plan aliases. Chat Completions and Responses are selectable per connection. Aliases are translated to real provider model names on the server. Voice-input agents keep the fixed meaning-preservation rules and raw transcript fallback. General text agents use their own prompt without voice rewrite restrictions; they can be saved and tested in the agent tab. This release does not enable automatic replies to clients.
3. **Synthesis:** MiMo basic `mimo-v2.5-tts`, chosen gateway and English/Chinese voice; an explicit sample up to 500 characters creates a playable WAV. No autoplay or automatic client delivery. Voice cloning/design are not exposed without the corresponding reference-audio workflow.

ASR and TTS use **`/chat/completions`**, with the documented audio-specific request bodies. They do not use OpenAI's `/audio/transcriptions` or `/audio/speech`. Responses is a text protocol here; unsupported remote background/conversation-state fields are not sent.

The three capability checks are independent. A text success never verifies ASR/TTS. Checks store timestamp, result and a safe error code; a late result cannot verify a replaced key or changed endpoint. Actual user audio and text are not stored in the usage ledger. HTTP redirects and arbitrary base URLs are rejected. Rate-limit retries are bounded; failures never select another paid account.

## Cost estimates and validation

PAYG estimates follow published USD prices with the existing conservative fixed estimate of 90 ₽/USD: Flash $0.14/$0.28 per million input/output tokens, Pro $0.435/$0.87, ASR $0.074/hour. This is a budget estimate, not an exchange-rate quote or provider invoice. Token Plan is quota billing. Basic TTS is temporarily promotional in the published tariff; recheck pricing before enabling paid production speech delivery. Monthly budget checks and bounded request throttles apply; simultaneous in-flight requests can exceed an estimate, so the local budget is not a provider-side hard spending cap.

Checks: unit transport contracts, secret/account isolation, language and payload limits, safe errors, no paid fallback; SQL migration and route persistence; desktop/mobile native browser flows and existing chat/voice regressions. Production key installation requires an authenticated owner/admin session or authorized server access.

Official references:
- https://mimo.mi.com/docs/en-US/api/chat/openai-api
- https://mimo.mi.com/docs/en-US/api/chat/responses
- https://mimo.mi.com/docs/en-US/api/audio/Speech-Recognition
- https://mimo.mi.com/docs/en-US/api/audio/tts
- https://mimo.mi.com/docs/en-US/quick-start/usage-guide/audio/Speech-Recognition
- https://mimo.mi.com/docs/en-US/quick-start/faq/token-plan/Plans&Pricing
- https://mimo.mi.com/docs/en-US/price/pay-as-you-go

The gateway header reflects saved per-capability checks: active only when all three pass, partial for incomplete verification, error when any check fails. “Проверить всё” runs the three checks sequentially, blocks conflicting actions on that card, and shows the current stage. Credentials, action routes and common recording options have independent save forms.


Yandex SpeechKit synthesis uses the existing encrypted service-account key with `ai.speechkit-tts.user`, API v1 form-encoded requests and PCM-to-WAV wrapping. Available voices are provider-specific (Russian voices and English John); saving a Yandex key resets its separate synthesis verification. API v1 cost is estimated at 1,342 ₽ per million characters including VAT, per the published tariff. No automatic provider switch is performed.

Preview success now requires the browser to load positive audio duration; an explicit Play button and native controls start playback on user action. The production CSP permits same-origin media and local blob media only; script/connect/frame policies stay restricted. Browser tests reproduce the old policy's blocked 0:00 player and verify decoding plus advancing playback time under the fixed policy at desktop/mobile sizes. A billing acknowledgement is not proof of browser playback; decode failures show an inline error while retaining the billing acknowledgement.

Yandex references: https://aistudio.yandex.ru/ru/docs/speechkit/pricing ; https://aistudio.yandex.ru/en/docs/speechkit/tts/request ; https://aistudio.yandex.ru/en/docs/speechkit/tts/voices ; https://aistudio.yandex.ru/en/docs/ai-studio/security/
