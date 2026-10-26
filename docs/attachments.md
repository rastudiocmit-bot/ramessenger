# Native AlfaCRM MAX attachments: staged implementation

## Stage 1 — capability inspection (live verification pending)

Confirmed from supplied compose HTML: input `MaxComposeBulkForm[attachment][]`,
multiple selection, multipart FormData, at most 10 files and 8,388,608 bytes total.
The separate file-size check is disabled (`maxFileSize = 0`); the hidden 2 MB
label is not an active limit. Server acceptance and delivery remain unverified.

Confirmed from supplied service-message feed HTML: an anchor containing
`ion-android-attach` points to an original under `/uploads/storage/` on the CRM
origin. The `img-circle` image is an avatar, not an attachment. This sample is
not evidence of inbound-client markup or download authorization requirements.

`center_messenger.attachment_probe` inspects saved HTML offline. It recognizes
marked storage links in feed items and journal rows. It performs no requests,
DB writes or submissions, and prints no names, URLs, CSRF or message text.
It is deliberately not integrated into synchronization yet. Unrecognized
external storage must be investigated, not silently allowed.

Usage with saved message markup (not compose-form markup):

```sh
python -m center_messenger.attachment_probe --origin https://YOUR-CRM-HOST < message.html
```

A zero result means no recognized candidates in that markup, not that the
transport cannot receive attachments. `markup_only` never proves downloading.

### Live server command

The opt-in module `center_messenger.attachment_audit` uses the existing encrypted
browser session from the DB. It opens a separate Chromium context and allows
only explicitly requested GET navigation/fetches. Automatic images, scripts,
POSTs, other resources and redirects are blocked. DB transactions are READ ONLY
and released before browser work; messages, cursors and stored sessions are not
updated. Original sample bytes are consumed in Chromium and discarded.

After the source containing this module is deployed:

```sh
systemctl start ramessenger-local-build.service &&
docker exec center-messenger-api-1 /opt/venv/bin/python -m center_messenger.attachment_audit --progress
```

No `-it`, API keys, passwords or file paths are needed. With one configured
workspace it is selected automatically; with several, use `--workspace UUID`.
Defaults: all available branches, both directions, at most five pages per stream,
four file attempts and approximately 180 seconds plus the last in-flight request.
The probe's 16 MiB per-file resource ceiling is NOT a discovered incoming limit.

For a specific customer card and its compose controls, add `--branch BRANCH_ID
--customer CUSTOMER_ID`. This performs GET only: it never attaches/uploads/sends
anything. Feed inspection covers only the initial card HTML; an AJAX-only feed
requires a further verified native read route, not an assumption of no files.

`--direction receive` restricts the test to incoming messages. `--pages 1..20`
and `--downloads 1..10` adjust the bounded sample. No journal date ordering is
assumed, and no entire-history rescan or cursor reset is performed.

The report omits customer names, message texts, source URLs, cookies and CSRF.
- `image_download_verified` means a complete HTTP 200 response with a recognized
  image signature was consumed; it does not prove image decoding or delivery.
- `document_download_verified` currently requires a PDF signature. ZIP containers
  are reported as ZIP, never claimed to be valid Office documents.
- `customer_inspection.compose` reports controls and constants in native scripts;
  it does not prove server acceptance, formats or delivery.
- `delivery_verified` remains false: sending is outside this read-only command.
- Exit 0: image and PDF samples verified without stream/download errors. Exit 2:
  partial/inconclusive, including no eligible samples. Exit 1: setup failure.
- `bounded_sample` never means that every journal or attachment has been checked.

### Image age policy

Automatically fetch images only when their source message is at most 72 hours
old, including the exact boundary. Use source timestamps, interpreted as Moscow
time if no zone is supplied, not file creation/import time. Missing/future times
are not treated as new. Recheck age immediately before requesting the original.
The diagnostic samples only recent messages for every file type to keep scope
bounded; this does not impose a product-level 72-hour limit on documents.

Never backfill old images or download their thumbnails. Do not delete an image
already saved when it becomes older than 72 hours. Old message text, history
cursors and read/handled state remain unchanged. Subsequent implementation must
repair only eligible recent image references, without a full history replay.

### Remaining live checks

- Read inbound image and document markup in both journal and customer feed.
- Download original using existing server Chromium session; verify status,
  redirects, content type, byte count and absence of a login HTML response.
- Test outbound image/document and boundaries only in a designated test chat.
- Verify file-only sending and reconcile the accepted submission with history.

No authenticated VPS browser session is available in the development workspace.
Do not claim these live checks passed or send to arbitrary clients.

## Subsequent stages

2. Persistent metadata and protected disk storage; recoverable download queue.
3. New inbound attachments and targeted recovery of image references at most
   72 hours old; never old-image backfill, text/time changes or history replay.
4. Outbound picker/drop support and native browser upload; enforce 10/8 MiB on
   client and server; reconcile unknown results before retrying.
5. Lazy thumbnails, mobile/desktop image viewer and authorized document download.
6. Per-user service-image visibility setting; preserve originals and associations.
7. Integration validation, backup/restore, workspace isolation and deployment.

Incoming size limits must be determined independently. Shared content hashes
may deduplicate disk bytes, never distinct message associations.
