"""Evidence-based family follow-ups. No network, paid inference or CRM writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import re
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

KINDS = {
    'missed': 'Связаться после пропущенного звонка',
    'callback': 'Проверить обещание перезвонить',
    'followup': 'Проверить обещание уточнить и ответить',
    'trial': 'Уточнить результат обращения о пробном занятии',
    'group': 'Подобрать или уточнить группу',
    'schedule': 'Обсудить удобное расписание',
    'payments': 'Разобрать вопрос об оплате или отработке',
    'retention': 'Проверить причину возможного ухода',
    'review': 'Проверить неполный результат разбора',
    'campaign': 'Связаться по очереди обзвона',
    'repeat': 'Повторные входящие — проверить результат',
}
STAGES = {'contact': 'Обращение', 'offered': 'Предложена группа', 'trial_booked': 'Запись на пробное',
          'trial_attended': 'Пробное посещено', 'subscription': 'Куплен абонемент', 'renewed': 'Продление'}
STATUSES = {'open': 'Нужно действие', 'working': 'В работе', 'waiting': 'Ожидаем ответ',
            'resolved': 'Доведено до результата', 'dismissed': 'Не требует действия', 'observed': 'Нет открытых вопросов'}
CAMPAIGNS = {'interest': 'Интерес без записи', 'waitlist': 'Лист ожидания', 'trial': 'После пробного',
             'renewal': 'Продление', 'return': 'Возвращение', 'new_group': 'Новая программа', 'schedule': 'Изменение расписания'}
PATTERNS = {
    'callback': r'\b(?:перезвоню|перезвоним|вам перезвонят|обеща\w* перезвонить)\b',
    'followup': r'\b(?:уточню|уточним|узнаю|узнаем|проверю|проверим|передам|передадим|отвечу|ответим)\b',
    'trial': r'\bпробн\w* (?:занят|урок)|\bна пробн\w*\b',
    'group': r'\b(?:есть ли места|свободные места|подобрать группу|лист ожидания|записаться на|интересует курс)\b',
    'schedule': r'\b(?:неудобн\w*|не подходит расписание|другое время|другой день|только по субботам|не успеваем)\b',
    'payments': r'\b(?:списал\w*|списыва\w*|отработ\w*|непонятн\w* оплат\w*|ошиб\w* в оплат\w*)\b',
    'retention': r'\b(?:прекрат\w* занятия|перестан\w* ходить|больше не будем|не будем (?:ходить|продлевать)|хотим уйти|реб[её]нку скучно|реб[её]нку сложно|разочарова\w*|недовольн\w*)\b',
}
COURSES = {'robotics': ('Робототехника', r'робототех|роботополис'), 'drawing': ('Рисование', r'рисован|художествен'),
           'coding': ('Программирование', r'программирован|пайтон|python|unity|scratch|godot'),
           'languages': ('Языки', r'английск|японск|корейск|иностранн\w* язык'),
           'summer': ('Летние программы', r'летн\w* (?:клуб|лагер|программ)|каникул')}
COMPILED = {key: re.compile(value, re.I) for key, value in PATTERNS.items()}
MARKER = re.compile(r'\[(\d{2,5}):(\d{2})\]\s*Канал (\d+) \(роль не подтверждена\):\s*')


def now_utc(): return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else value


def moment(value):
    if isinstance(value, datetime): return value
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def family_id(workspace, account, phone, call=None):
    # Hidden numbers are separate incidents, never one fictitious family.
    return str(uuid5(UUID(str(workspace)), f'phone-care:{account}:{phone or "hidden:" + str(call)}'))


def evidence_id(call, kind, quote):
    return hashlib.sha256(f'{call}:{kind}:{quote}'.encode()).hexdigest()[:32]


def due_hint(text, at, zone='Europe/Moscow'):
    try: local = moment(at).astimezone(ZoneInfo(zone))
    except (ValueError, KeyError): return None
    if re.search(r'завтра до обеда', text, re.I):
        return (local + timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0).isoformat()
    match = re.search(r'завтра (?:в|до) (\d{1,2})(?::(\d{2}))?\b', text, re.I)
    if match and int(match[1]) < 24 and int(match[2] or 0) < 60:
        return (local + timedelta(days=1)).replace(hour=int(match[1]), minute=int(match[2] or 0), second=0, microsecond=0).isoformat()
    return None


def analyze(call, transcript='', *, run=None, truncated=False, zone='Europe/Moscow'):
    """Proposals only. Speaker identities and agreement are never inferred."""
    signals, interests = [], []
    def signal(kind, quote='', offset=0, channel=None):
        signals.append({'id': evidence_id(call['id'], kind, quote), 'kind': kind, 'title': KINDS[kind],
                        'call_id': str(call['id']), 'run_id': str(run) if run else None, 'at': iso(call['started_at']),
                        'quote': quote, 'offset_seconds': offset, 'channel': channel, 'state': 'proposed',
                        'suggested_due_at': due_hint(quote, call['started_at'], zone)})
    if call.get('direction') == 'in' and call.get('missed'): signal('missed')
    markers = list(MARKER.finditer(transcript))
    pieces = [(0, None, transcript)] if not markers else [
        (int(m[1])*60 + int(m[2]), int(m[3]), transcript[m.end():markers[i+1].start() if i+1 < len(markers) else len(transcript)])
        for i, m in enumerate(markers)]
    for offset, channel, content in pieces:
        for sentence in re.split(r'(?<=[.!?])\s+|\n+', content):
            for kind, pattern in COMPILED.items():
                match = pattern.search(sentence)
                if not match or any(x['kind'] == kind for x in signals): continue
                if kind in ('callback', 'followup') and re.search(r'\bне\s+$', sentence[:match.start()], re.I): continue
                start = max(0, match.start()-100)
                quote = sentence[start:start+400].strip()
                if quote: signal(kind, quote, offset, channel)
            for key, (label, pattern) in COURSES.items():
                if key not in interests and re.search(pattern, sentence, re.I): interests.append(key)
    if truncated: signal('review')
    return {'signals': signals, 'interests': interests, 'complete': not truncated}


def merge_case(previous, call, analysis, *, workspace, now=None):
    now = now or now_utc()
    result = deepcopy(previous) if previous else {
        'id': family_id(workspace, call['account_id'], call.get('phone'), call['id']), 'version': 0,
        'account_id': call['account_id'], 'phone': call.get('phone'), 'source_call_id': str(call['id']),
        'status': 'observed', 'stage': 'contact', 'reached_stages': ['contact'], 'journey': 'unknown', 'assignee_id': None, 'due_at': None,
        'label': '', 'signals': [], 'calls': [], 'interests': [], 'campaigns': [], 'events': [],
        'created_at': iso(now), 'resolved_at': None,
    }
    summary = {key: iso(call.get(key)) for key in ('id', 'started_at', 'finished_at', 'direction', 'missed', 'talk_seconds', 'total_seconds')}
    summary['id'] = str(call['id']); summary['recording_available'] = bool(call.get('recording_ciphertext'))
    calls = [x for x in result['calls'] if x['id'] != summary['id']] + [summary]
    result['calls'] = sorted(calls, key=lambda x: (x['started_at'], x['id']), reverse=True)[:50]
    result['history_truncated'] = result.get('history_truncated', False) or len(calls) > 50
    # Reprocessing replaces unconfirmed proposals from this call; confirmed task evidence survives.
    old = {x['id']: x for x in result['signals']}
    kept = [x for x in result['signals'] if x['call_id'] != summary['id'] or x.get('task_id') or x.get('state') == 'dismissed']
    known = {x['id'] for x in kept}
    for source in analysis['signals']:
        if source['id'] in known: continue
        source = source | {k: old[source['id']][k] for k in ('state', 'task_id', 'resolved_by') if source['id'] in old and k in old[source['id']]}
        if source['id'] not in old and moment(source['at']) < now-timedelta(days=7): source['state']='historical'
        if result.get('resolved_at') and moment(source['at']) <= moment(result['resolved_at']): source['state'] = 'reviewed'
        kept.append(source)
    # An actual answered call after a missed one establishes contact, not resolution of other questions.
    answered = [x for x in result['calls'] if not x['missed'] and (x.get('talk_seconds') or 0) > 0]
    for source in kept:
        if source['kind'] == 'missed' and source['state'] == 'proposed':
            later = next((x for x in answered if x['started_at'] > source['at']), None)
            if later: source.update(state='contacted', resolved_by=later['id'], contacted_at=later['started_at'])
    incoming = [x for x in result['calls'] if x['direction']=='in' and moment(x['started_at']) >= moment(result['calls'][0]['started_at'])-timedelta(days=7)]
    if len(incoming) >= 3:
        latest = incoming[0]; key = evidence_id(latest['id'],'repeat','')
        if not any(x['id']==key for x in kept):
            for x in kept:
                if x['kind']=='repeat' and x['state']=='proposed': x['state']='reviewed'
            repeated_state='historical' if moment(latest['started_at'])<now-timedelta(days=7) else 'proposed' if not result.get('resolved_at') or moment(latest['started_at'])>moment(result['resolved_at']) else 'reviewed'
            kept.append({'id':key,'kind':'repeat','title':KINDS['repeat'],'call_id':latest['id'],'run_id':None,
                'at':latest['started_at'],'quote':'','offset_seconds':0,'channel':None,'state':repeated_state,'suggested_due_at':None})
    result['evidence_truncated'] = result.get('evidence_truncated', False) or len(kept) > 100
    result['signals'] = sorted(kept, key=lambda x: (x['at'], x['id']), reverse=True)[:100]
    result['interests'] = sorted(set(result['interests']) | set(analysis['interests']))
    pending = [x for x in result['signals'] if x['state'] == 'proposed']
    if pending and result['status'] in ('observed', 'resolved', 'dismissed'): result['status'] = 'open'
    elif not pending and result['status'] == 'open' and not any(x.get('task_id') for x in result['signals']): result['status'] = 'observed'
    result['first_call_at'] = min(result.get('first_call_at') or summary['started_at'], summary['started_at'])
    result['last_call_at'] = max(result.get('last_call_at') or summary['started_at'], summary['started_at'])
    result['updated_at'] = iso(now); result['version'] += 1
    return result


def case_summary(case):
    keys = ('id', 'version', 'phone', 'label', 'status', 'stage', 'reached_stages', 'journey', 'assignee_id', 'due_at', 'interests', 'created_at', 'work_at',
            'campaigns', 'updated_at', 'first_call_at', 'last_call_at', 'branch', 'source')
    return {key: case.get(key) for key in keys} | {
        'pending': sum(x['state'] == 'proposed' for x in case.get('signals', [])),
        'reasons': sorted({x['kind'] for x in case.get('signals', []) if x['state'] == 'proposed'}),
        'recovered_missed': sum(x['kind']=='missed' and x['state']=='contacted' for x in case.get('signals', [])),
    }


def metrics(cases, *, now=None):
    now = now or now_utc()
    reasons, demand = {}, {}
    for case in cases:
        for key in case.get('reasons', []): reasons[key] = reasons.get(key, 0)+1
        for key in case.get('interests', []): demand[key] = demand.get(key, 0)+1
    # Conversion stages are explicit staff confirmations, never guesses from CRM status.
    cohort = [c for c in cases if c.get('journey') == 'new']
    stages = {stage: sum(stage in c.get('reached_stages', ['contact']) for c in cohort) for stage in STAGES}
    return {'total': len(cases), 'open': sum(c.get('status') in ('open','working','waiting') for c in cases),
            'recovered_missed': sum(c.get('recovered_missed',0) for c in cases),
            'overdue': sum(c.get('status') in ('open','working','waiting') and bool(c.get('due_at')) and moment(c['due_at']) < now for c in cases),
            'reasons': reasons, 'demand': demand, 'confirmed_stages': stages, 'new_journey_count': len(cohort),
            'unclassified': sum(c.get('journey') == 'unknown' for c in cases), 'computed_at': iso(now)}
