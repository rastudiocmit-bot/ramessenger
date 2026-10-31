# Browser telephone: current boundary and device checks

## Delivered in this release

Connections → Novofon → “Проверить звук и микрофон” opens a local device check.
The same entry is in a call's expanded transcript → details → player state.
It generates a WAV signal and records up to five seconds from the microphone.
Recording starts only on an explicit click; permission denial is actionable.
Tracks stop on completion, close, page exit, workspace switch and logout. Late
permission results are stopped and discarded. Temporary blob URLs are revoked.
No call, provider API, transcription, upload or persistent recording is made.

Where `HTMLMediaElement.setSinkId` is available, the user can select the output
for the test and call recordings. Selection stays only in session memory.
The call player waits for output selection and rechecks its conversation/context
before starting. Missing outputs are reported rather than silently claiming
success. Other browsers use the system output. A successful permission or play
promise does not prove sound reached the physical speaker; the user confirms
whether the signal is audible. Site/tab/OS mute cannot be read by the page.

## Novofon evidence as of 2026-11-01 (Moscow)

User-provided screenshots confirm an active SIP line, `sip.novofon.ru:5060`,
inbound/outbound direction and a masked password. A historical “Softphone Web”
registration is not evidence of a currently registered browser or a public WSS
endpoint. No SIP password was received or copied into this repository.

The provided texts cover Data API, Call API and HTTP event notifications.
Call API uses JSON-RPC and controls calls; these documents do not specify a
browser media/signalling interface. An HTTP notification delivers an event,
not microphone audio. The screenshot's “tag assigned” event alone is not an
incoming-call notification. No webhook URL or live routing was changed.

Official references checked:

- https://novofon.github.io/call_api/
- https://novofon.github.io/data_api/
- https://novofon.com/instructions/integration/own-crm/
- https://novofon.com/instructions/novofon/pbx/
- https://novofon.com/instructions/softphone/

## Required connection work

There are two viable directions to verify: a supported embeddable Novofon
WebRTC interface, or an isolated WebRTC↔SIP media gateway registered to the
confirmed SIP service. Native SIP port 5060 is not a browser WebSocket URL.
Do not guess WSS hosts, reuse the employee API key as a SIP password, embed
the full provider cabinet, or relabel callback through another device as an
in-messenger microphone call.

The gateway path does not require a proprietary SDK, but needs the actual SIP
password entered through an encrypted settings form, confirmed provider SIP
transport/codecs and registration/routing rules, WSS/TLS, ICE/NAT media handling,
staff authorization and a designated test number. Keep media independent from
import/ASR work. No direct edits to the MAX/browser contour, new deploy owner or
live firewall changes are part of this UI release.

Provider request prepared for the account owner (not sent):

> Встраиваем телефон в собственный мессенджер https://rachat.ru. Нужны
> входящие и исходящие разговоры через микрофон браузера на компьютере и
> телефоне. SIP-линия активна, доступ к Data API уже проверен. Предоставьте,
> пожалуйста, поддерживаемый интерфейс встраивания: WebRTC SDK или SIP over
> WSS, способ авторизации, адреса сигнализации, ICE/STUN/TURN, кодеки,
> требования к домену и поддержку Chrome/Android/Safari iOS. Если прямого
> встраивания нет, подтвердите подключение нашей SIP-линии через отдельный
> WebRTC↔SIP шлюз: доступные SIP-транспорты, RTP/кодеки, NAT, ограничения
> одновременных регистраций и настройку входящей маршрутизации. Отдельно
> уточните роль выданного поддержкой секрета в этом подключении. Нужен
> разговор внутри нашей страницы, без обязательного расширения Chrome,
> внешнего софтфона или обратного вызова на другое устройство.

## Validation and release

`test_phone_devices_ui.cjs` covers cleanup, stale permissions, routing errors,
scope changes, local speaker/recording behavior and honest readiness.
`test_call_history_ui.cjs` also checks output preparation and stale playback.
`check_phone_devices.cjs` uses real Chromium with synthetic microphone input,
decodes the local WAV, records/replays a real MediaRecorder blob and checks
desktop/mobile overflow with no remote mutations. This is not a physical
speaker or Novofon conversation acceptance test.

No backend, schema, worker pins or protected manifest changes are required.
Publish through the existing local-build owner and its unchanged MAX gate.
Night archive behavior remains unchanged: calls older than seven days,
since 2020-10-31, 00:00–08:00 Europe/Moscow, whole-call starts at least
120 seconds apart. The schedule and archive-import unit checks are rerun;
live archive progress and isolated PostgreSQL gate still need VPS evidence.
