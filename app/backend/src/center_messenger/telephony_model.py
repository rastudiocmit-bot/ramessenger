"""Provider call facts. Never message sends, recipient selection or AI commands."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid5
from zoneinfo import ZoneInfo

from center_messenger.crm_directory import phone_number
from center_messenger.novofon_client import AuditError, identifier, recording_url

FIELDS = ['id','communication_id','start_time','finish_time','direction','is_lost',
          'contact_phone_number','talk_duration','total_duration','employees',
          'call_records','wav_call_records','full_record_file_link']


def timestamp(value, zone, optional=False):
    if value is None and optional:
        return None
    try:
        result = datetime.fromisoformat(value)
        return (result.replace(tzinfo=ZoneInfo(zone)) if result.tzinfo is None else result).astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise AuditError('calls_schema') from None


def normalize(row, *, workspace, account, connection, zone):
    source = identifier(row.get('id'))
    if not source or row.get('direction') not in ('in','out') or type(row.get('is_lost')) is not bool:
        raise AuditError('calls_schema')
    start = timestamp(row.get('start_time'), zone)
    finish = timestamp(row.get('finish_time'), zone, True)
    if finish and finish < start:
        raise AuditError('calls_schema')
    durations = []
    for name in ('talk_duration','total_duration'):
        value = row.get(name)
        if value is not None and (type(value) is not int or not 0 <= value <= 604800):
            raise AuditError('calls_schema')
        durations.append(value)
    employees = row.get('employees') or []
    if not isinstance(employees,list) or len(employees)>100 or any(not isinstance(e,dict) for e in employees):
        raise AuditError('calls_schema')
    names = sorted({e['employee_full_name'][:200] for e in employees
                    if isinstance(e.get('employee_full_name'),str) and e['employee_full_name']})[:20]
    record = recording_url(row)
    result = {'id':uuid5(workspace,f'novofon:{account}:{source}'), 'provider':'novofon',
              'account_id':account,'source_id':source,'connection_id':connection,
              'phone':phone_number(row.get('contact_phone_number')), 'direction':row['direction'],
              'started_at':start,'finished_at':finish,'talk_seconds':durations[0],
              'total_seconds':durations[1],'missed':row['is_lost'],'employees':names}
    result['fingerprint'] = hashlib.sha256(json.dumps(result,
        default=str,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result, record


def call_view(row, *, audio_available=True):
    return {'id':str(row['id']), 'source':'novofon','occurred_at':row['started_at'].isoformat(),
            'finished_at':row['finished_at'].isoformat() if row['finished_at'] else None,
            'direction':row['direction'],'missed':row['missed'],'talk_seconds':row['talk_seconds'],
            'total_seconds':row['total_seconds'],'employees':row['employees'],
            'recording_available':bool(audio_available and row['recording_ciphertext']),
            'version':row['version'],'transcription_status':'not_requested'}
