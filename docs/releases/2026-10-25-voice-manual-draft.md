# Voice recognition stays in the ordinary draft

After recording, an inline recognition notice added another row below the composer, pushing the message field upwards. A separate preview also increased its height and could send the recognised text when its countdown expired.

Voice capture now only fills the ordinary message draft. The preview, countdown and all voice-triggered form submissions are removed. Releasing a hold on either the microphone or history, finishing a tapped recording, reaching the duration limit and retrying recognition all require the operator to press the existing Send button afterwards. The recording hint reflects that behavior; upward dragging still cancels capture.

Empty speech, short clips, early release during microphone preparation and provider fallback notices leave no persistent service message. Empty recognition preserves the existing draft. Usable recognised text, including fallback text, stays editable in the same input. Genuine microphone/service failures keep explicit retry/cancel controls and the failed clip in memory.

Preparation, recording, recognition and actual errors occupy the existing composer row. They no longer append a footer, move the field or cover the conversation. Voice insertion preserves the history scroll anchor, adds to the current draft and never opens the keyboard or steals focus. Normal textarea growth matches manually typed text. Chat/workspace/session fencing and bounded microphone/network cleanup remain in place.

Validation: 128 UI tests; Chromium checks with a fake microphone for tap, hold, upward cancellation, downward motion, history gestures, provider fallback, retry and explicit sending. Layout checks at 320, 390 and 1440 px compare composer coordinates before/during recognition, after empty recognition and service errors, and against the same manually typed text. All checks passed with zero measured composer or history-anchor drift in the relevant comparisons. All API requests were intercepted. Physical iPhone hardware was unavailable.

Assets: voice.js 1.5.0, cabinet.css 2.20.1. No server API or database schema changes. Reload the messenger page after deployment. This replaces the preview/countdown behavior described in the earlier mobile voice release.
