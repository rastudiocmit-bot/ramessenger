# Mobile voice capture and quiet feedback

The mobile microphone could start after a cancelled tap or attach a late permission result to a different chat. A suspended audio context could leave a hold unresolved indefinitely. Recognition and its preview also kept the history scroll locked, while blue global notifications covered the conversation.

## Result

- Removed the global toast element, blue overlay styles and notification timers throughout the cabinet. Routine successes are quiet. Action errors stay as plain text in the relevant form; microphone statuses and retry controls stay in the composer.
- Voice capture has one scoped attempt across preparation, recording, recognition, retry and preview. Audio is prepared in the actual gesture; scrolling never requests microphone permission. Startup, permission, first-frame and stalled-stream waits are bounded. Cancelling stops even a stream that arrives late.
- Recording time counts captured samples, excluding permission/startup latency. Silent samples differ from missing callbacks. The capture graph has a zero-gain output so the microphone is never played through the speakers.
- Release, pointer cancellation/lost capture, chat/workspace/session changes, disabled input, backgrounding and page exit free capture resources and scroll locks. Interrupted or unusable contexts are replaced on the next recording gesture.
- Recognition allows 60 seconds including upload and response processing (provider stages allow 10 seconds for SpeechKit and up to 20 seconds for normalization). Network failure, deadline and HTTP service errors are distinguished. A failed clip remains in memory for explicit retry. Retry and fallback never automatically send.
- The preview is inline in the composer; history remains usable. Touching or scrolling the chat, typing or leaving the page cancels automatic sending and keeps recognised text as a draft. Late results cannot be inserted into another chat. No audio is stored on disk by this change.
- Added a CSRF-protected, permission-checked and rate-limited diagnostic endpoint accepting strict enum/numeric metadata only. Capture logs contain attempt ID, outcome, error category, audio state and durations; recognition request logs correlate attempt ID, HTTP status and duration. Audio, transcripts, credentials and device labels are excluded. This improves future diagnosis; it does not establish the cause of a previous service outage.

## Validation

Synthetic audio lifecycle tests cover cold permission, hung resume, release/cancellation, late streams/results, interruption, silent/stalled audio, retry, drafts and preview cancellation. Browser checks exercise desktop/phone tap and hold gestures using a fake microphone and fully intercepted requests; mobile layout checks include narrow screens and both themes. Server checks validate permissions and bounded diagnostics. Physical iPhone/Safari hardware is not available in this environment.

Assets: app.css 0.8.36, cabinet.css 2.20.0, voice.js 1.4.0, app.js 0.13.0. Database schema remains 0016_crm_card_preference. Reload the page after deployment to activate the new voice lifecycle.
