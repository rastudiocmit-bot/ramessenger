"""Offline, read-only inspection of native CRM attachment markup.

python -m center_messenger.attachment_probe --origin https://example.s20.online < page.html
No network requests, writes, message submission or raw URL output.
"""
import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from center_messenger.alfa_max_browser import _message_time

MAX_HTML_BYTES = 2_000_000
MARKERS = '.ion-android-attach,.ion-paperclip,.fa-paperclip'


def storage_url(value, *, origin):
    """Conservative allowlist for the native storage layout observed in CRM."""
    base = urlsplit(origin)
    if base.scheme != 'https' or not base.hostname or base.username or base.password:
        raise ValueError('invalid_origin')
    if any(char in value for char in ('\\', '\r', '\n', '\t')):
        return None
    address = urljoin(origin, value.strip())
    parts = urlsplit(address)
    path = unquote(parts.path)
    if (parts.scheme != 'https' or parts.username or parts.password
            or parts.netloc.lower() != base.netloc.lower() or parts.fragment
            or not path.startswith('/uploads/storage/')
            or any(segment in {'.', '..'} for segment in path.split('/'))
            or '\\' in path or '%' in path):
        return None
    return address


def recent_message(occurred_at, *, now=None):
    """Fail closed on missing/future times; the exact 72-hour boundary is valid."""
    now = now or datetime.now(UTC)
    return (isinstance(occurred_at, datetime) and occurred_at.tzinfo is not None
            and now - timedelta(hours=72) <= occurred_at <= now)


def extract_attachment_candidates(html, *, origin):
    """Return marked native storage links; never treat all message links as files.

    Candidates are unverified references, not permission to fetch arbitrary URLs.
    Downloader must independently validate DNS, redirects, content and size.
    """
    if len(html.encode('utf-8')) > MAX_HTML_BYTES:
        raise ValueError('html_size_limit')
    base = urlsplit(origin)
    if base.scheme != 'https' or not base.hostname or base.username or base.password:
        raise ValueError('invalid_origin')
    soup = BeautifulSoup(html, 'html.parser')
    candidates = []
    for message in soup.select('.feed-element, tbody tr[data-key]'):
        cells = message.find_all('td', recursive=False)
        if message.name == 'tr':
            # Only the message cell, never customer links or the sender avatar.
            content = cells[3] if len(cells) >= 5 else message
            occurred_at = _message_time(cells[4]) if len(cells) >= 5 else None
        else:
            content = message.select_one('.m-t-sm') or message
            stamp = message.select_one('small.text-muted')
            occurred_at = _message_time(stamp) if stamp else None
        seen = set()
        for anchor in content.select('a[href]'):
            if not anchor.select_one(MARKERS):
                continue
            address = storage_url(anchor.get('href', ''), origin=origin)
            if not address:
                continue
            if address in seen:
                continue
            seen.add(address)
            candidates.append({'url': address,
                               'name': ' '.join(anchor.get_text(' ', strip=True).split())[:200],
                               'source_layout': 'feed' if 'feed-element' in message.get('class', []) else 'journal',
                               'occurred_at': occurred_at,
                               'verified': False})
    return candidates


def inspect_markup(html, *, origin):
    candidates = extract_attachment_candidates(html, origin=origin)
    # Never print message text, names, source IDs, CSRF, cookies or signed URLs.
    return {'status': 'markup_only', 'attachment_candidates': len(candidates),
            'layouts': sorted({item['source_layout'] for item in candidates}),
            'download_verified': False, 'delivery_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_HTML_BYTES + 1)
    if len(raw) > MAX_HTML_BYTES:
        parser.exit(1, 'html_size_limit\n')
    try:
        result = inspect_markup(raw.decode('utf-8'), origin=args.origin)
    except (ValueError, UnicodeError):
        parser.exit(1, 'invalid_input\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
