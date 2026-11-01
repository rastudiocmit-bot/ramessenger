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
endpoint. No SIP password from the screenshots is copied into this repository
or installed on the server by this release. Credentials are entered in the
authenticated SIP-line form described below.

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

### Why there is no “Позвонить” action on a client card

The client-card contact row currently exposes a `tel:` link and a copy action.
It has no in-messenger dial handler or outgoing-call API. The SIP registry
explicitly returns `browser_call_ready: false`; saving employee profiles does
not register a telephone or establish a media session. The family-care release
adds post-call work, not this missing dial path. A healthy deployment and passing
history/task tests do not confirm the ability to speak to a client in the browser.

Delivering the real card action requires an authorized employee SIP selection,
a verified browser media connection, outgoing/incoming call lifecycle controls,
and acceptance on a designated test call. Keep this separate from the existing
number link; do not label opening a default external app as an embedded call.

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

## Employee SIP profiles

Connections → Novofon → “SIP-линии сотрудников” now lets an owner/admin add,
edit and remove profiles assigned to actual active messenger member UUIDs.
Names/emails and the provider's employee IDs are not used to guess identity.
Members must first be added through the existing team/invitation workflow.
The admin selector includes each member's email to distinguish identical names,
while assignment still uses the member UUID. Globally disabled users are excluded
along with inactive memberships. The employee-only response has no directory or
other employees' emails.

Each profile has a SIP number (text, with leading zeros), optional internal
extension (separate text), server, port, password, enabled flag and primary
flag. Default server/port are `sip.novofon.ru:5060` from the supplied settings;
no SIP transport, WSS host or registration is inferred from them. Up to five
profiles can be assigned per member, 100 per workspace. A SIP account cannot
be duplicated across employees. There is one primary enabled profile per
employee when enabled profiles exist. Disabling/deleting it selects another
enabled profile; inactive profiles are never selected as primary.

The registry uses the established workspace-scoped settings pattern in
`outbox_events`, its own topic `novofon-sip-profiles-v1`, and `published` state.
It is not enqueued as a message or loader job. No migration or worker update
is needed. Read-modify-write operations take a workspace advisory transaction
lock (including first insert) and require an exact registry version. Member
assignment is validated against active memberships under a shared row lock.

Passwords are encrypted with AES-GCM and the existing connection master key,
with separate AAD binding workspace, profile and assigned member. This is a
separate `sl1` secret format; it does not reinterpret an API key as a password
or weaken the connector vault's validation. Settings responses contain only
`password_configured`; ciphertext and plaintext are never returned. Empty
password preserves an unchanged profile's secret. Changing assignment, SIP
number, server or port requires explicit password reentry. No provider request
or automatic registration occurs during save.
Malformed Unicode in a submitted password is rejected before encryption with
a safe validation message. Valid UTF-8 passwords retain their exact value;
validation never returns the submitted password or partially updates a profile.

`GET /api/workspaces/{workspace}/integrations/novofon/sip-lines` and the PUT /
DELETE mutations are owner/admin-only. `GET .../sip-lines/mine` checks active
workspace/message access and returns only the actor's assigned metadata. The
device-check window shows this list without any password. An observer has no
calling permission. Disabling workspace membership immediately denies this
read. Future call-session authorization must also check live membership,
profile enablement and profile version before issuing any media credentials.

UI checks include stale save replies, workspace changes, blank-secret edits,
leading zeros, duplicate submissions, escaping and optimistic-version conflicts.
Deleting another line preserves an open draft and its entered password; deleting
the edited line clears that form. An automatic primary-line promotion after a
deletion updates the draft's untouched primary flag, preserving explicit edits
and assignment changes. A vault outage blocks writes while retaining working
cancel and password visibility controls. Background refreshes never advance the version
behind an open form, including before its first edit, so old fields cannot
silently overwrite another administrator's newer settings. Malformed stored
profile IDs return a safe settings error without exposing stored credentials.
Server checks use the real encryption and actual ASGI authentication/CSRF/input
validation chain with synthetic accounts and storage, including employee-only
reads and DELETE permission, CSRF and version checks. Chromium checks exercise
add/edit on desktop and mobile and reject invalid SIP numbers before sending
them; leading zeros and supported hyphen/underscore/dot logins remain valid.
No production password, registration or call
was used. Live persistence remains subject to the normal VPS deployment and
its database/readiness checks; local storage tests do not replace that gate.

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
