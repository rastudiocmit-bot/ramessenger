"""One bounded telephone AI step, with durable receipts before paid requests."""
from datetime import timedelta
import hashlib
import json
import logging

from sqlalchemy import text
from center_messenger import ai_gateways, ai_mimo, ai_grok, telephony_archive
from center_messenger.ai_voice import CATALOG, provider_of, model_cost, visible
from center_messenger.ai_yandex import YandexError, audio_cost
from center_messenger.auth_security import AuthError
from center_messenger.telephony_agent import TelephonyAgent
from center_messenger.telephony_ai_store import Store
from center_messenger.telephony_archive_import import ArchiveImport
from center_messenger.telephony_media import RecordingCache, MediaError, decode_part, PART_SECONDS, RATE
from center_messenger.novofon_client import AuditError

LOG=logging.getLogger(__name__)


def digest(value):return hashlib.sha256(value if isinstance(value,bytes) else value.encode()).hexdigest()


def chunks(value,size=12000):
    parts=[]
    while value:
        end=min(size,len(value))
        if end<len(value):
            boundary=max(value.rfind('\n',size//2,end),value.rfind(' ',size//2,end))
            if boundary>0:end=boundary+1
        parts.append(value[:end]);value=value[end:]
    return parts or ['']


def transcript(operations):
    lines=[]
    for op in operations:
        if not op['result']:continue
        _,part,channel=op['operation'].split(':')
        start=int(part)*PART_SECONDS
        lines.append(f'[{start//60:02d}:{start%60:02d}] Канал {int(channel)+1} (роль не подтверждена): {op["result"]}')
    return '\n'.join(lines)


class TelephoneWorker:
    def __init__(self,accounts,vault,*,store=None,cache=None,agent=None):
        self.store=store or Store(accounts.engine)
        self.vault=vault
        self.agent=agent or TelephonyAgent(accounts,vault)
        self.cache=cache or RecordingCache()
        self.archive=ArchiveImport(self.store,vault)

    def run_next(self):
        self.cache.expire()
        job=self.store.claim()
        if not job:return False
        try:
            if job['run'] is None:self.archive.run(job)
            else:self.process(job)
        except AuditError as error:
            self.archive_error(job,error.code,error.details)
        except (MediaError,AuthError) as error:
            if job['run'] is None:self.archive_error(job,'archive_wait',{})
            elif isinstance(error,AuthError) and error.status==429:
                self.store.update(job,phase='budget_wait',error_code='ai_budget_wait',retry_at=self.store.clock()+timedelta(minutes=5))
            elif isinstance(error,MediaError) and error.code=='recording_changed':
                self.store.update(job,phase='needs_review',error_code=error.code)
            else:
                code=error.code if isinstance(error,MediaError) else 'processing_not_ready'
                self.store.update(job,phase='error',error_code=code)
                if isinstance(error,AuthError) and error.status in (403,409):self.store.pause_automatic(job,code)
        except YandexError:
            # Network timeout, malformed response and remote errors can follow
            # provider acceptance. No blind retry or alternate paid account.
            self.store.uncertain(job)
            self.store.pause_automatic(job,'provider_result_unknown')
        except Exception as error:
            LOG.warning('Telephone AI interrupted exception=%s',type(error).__name__)
            if job['run'] is not None:self.store.uncertain(job,'processing_interrupted')
            else:self.archive_error(job,'archive_interrupted',{})
        finally:
            self.store.release(job,3)
        return True

    def archive_error(self,job,code,details):
        if job['run'] is not None:return
        delay=details.get('retry_after',120)
        if type(delay) is not int or delay<1:delay=120
        with self.store.tenant(job['workspace']) as db:
            if self.store.owned(db,job):
                db.execute(text("UPDATE telephony_archive_control SET last_error_code=:e,retry_at=now()+(:d*interval '1 second') WHERE workspace_id=:w"),{'w':job['workspace'],'e':code,'d':min(86400,delay)})

    def credentials(self,job):
        saved=job['run']['profile'];provider=provider_of(saved['model'])
        if saved.get('contract')!='telephone-ai-v1' or saved['model'] not in CATALOG:
            raise AuthError(409,'Версия задания требует проверки.')
        selected={provider,saved['recognition_provider']}
        with self.store.tenant(job['workspace']) as db:
            row=self.agent._settings(db,job['workspace'])
            creds=self.agent._credentials(db,job['workspace'],yandex=False,selected=selected)
            if any(not creds.get(key) for key in selected):raise AuthError(409,'Подключите выбранные ИИ-шлюзы.')
            config=ai_gateways.config(row)
            keys={p:row.get('key_ciphertext' if p=='yandex' else p+'_key_ciphertext') for p in selected}
            if 'grok' in selected:keys['grok']=ai_grok.connection_stamp(row)
            stamp=digest(json.dumps({'keys':keys,'folder':row.get('folder_id'),
                'routes':{p:config.get(p,{}) | {'checks':None} for p in selected if p in ai_gateways.PROVIDERS}},sort_keys=True))
        if job['run']['gateway_hash'] and job['run']['gateway_hash']!=stamp:
            raise AuthError(409,'Подключения изменились во время обработки.')
        if not job['run']['gateway_hash']:self.store.update(job,gateway_hash=stamp)
        return creds

    def process(self,job):
        run=job['run'];creds=self.credentials(job)
        if run['phase']=='summarizing' or run['transcript'] is not None:
            return self.summarize(job,creds)
        with self.store.tenant(job['workspace']) as db:
            call=db.execute(text('SELECT c.*,s.connected FROM telephony_calls c JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id WHERE c.workspace_id=:w AND c.id=:c'),{'w':job['workspace'],'c':run['call_id']}).mappings().one_or_none()
        if not call or not call['connected']:raise AuthError(409,'Подключение звонка недоступно.')
        if not call['recording_ciphertext']:
            expired=self.store.clock()-run['created_at']>=timedelta(hours=24)
            self.store.update(job,phase='error' if run['historical'] or expired else 'waiting_recording',
                error_code='recording_missing',retry_at=self.store.clock()+timedelta(minutes=5))
            return
        url=self.vault.open_recording(job['workspace'],call['connection_id'],run['call_id'],call['recording_ciphertext'])
        if not self.store.update(job,phase='fetching'):return
        media=self.cache.get(run['id'],url,run['audio_hash'])
        self.store.update(job,audio_hash=media['hash'],channels=media['channels'],phase='recognizing',error_code=None,retry_at=None)
        streams=decode_part(media['path'],media['channels'],run['part'])
        if not streams:
            raw=transcript(self.store.operations(job,'asr:'))
            self.store.update(job,transcript=raw,phase='summarizing')
            self.cache.close()
            return
        existing={row['operation']:row for row in self.store.operations(job,f'asr:{run["part"]:05d}:')}
        for channel,audio in enumerate(streams):
            operation=f'asr:{run["part"]:05d}:{channel}'
            if operation in existing:continue
            input_hash=digest(audio)
            if self.store.reuse_asr(job,operation,input_hash) is not None:return
            seconds=len(audio)/(RATE*2)
            padded=audio+bytes(max(0,RATE//10*2-len(audio)))
            provider=run['profile']['recognition_provider']
            estimate=audio_cost(padded) if provider=='yandex' else ai_mimo.audio_cost(len(padded)/(RATE*2),plan=provider=='mimo_plan')
            silence=not any(audio)
            receipt=self.store.receipt(job,operation,input_hash,0 if silence else estimate,seconds)
            if receipt is None:return
            if receipt.get('new'):
                if silence:raw=''
                elif provider=='yandex':raw=self.agent.client.recognize(padded,api_key=creds['yandex'],profanity=False)
                else:raw=self.agent.mimo.recognize(padded,**ai_gateways.options(creds,provider),plan=provider=='mimo_plan',language='ru')['text']
                usable=isinstance(raw,str) and len(raw)<=20000
                self.store.complete(job,operation,raw[:20000] if isinstance(raw,str) else '',cost=0 if silence else estimate,seconds=seconds,usable=usable)
            return
        self.store.update(job,part=run['part']+1)

    def summarize(self,job,creds):
        raw=job['run']['transcript'] or ''
        if not raw.strip():
            self.store.update(job,phase='needs_review',error_code='no_speech_recognized')
            return
        text_value=raw
        for level in range(10):
            parts=chunks(text_value)
            final=len(parts)==1
            prefix='summary:final:' if final else f'summary:{level:02d}:'
            previous={row['operation']:row for row in self.store.operations(job,prefix)}
            for index,part in enumerate(parts):
                operation=prefix+f'{index:05d}'
                if operation not in previous:
                    return self.describe(job,creds,operation,part,final)
            if final:
                self.store.update(job,phase='complete',summary=previous[prefix+'00000']['result'],error_code=None,retry_at=None)
                return
            text_value='\n\n'.join(previous[prefix+f'{index:05d}']['result'] for index in range(len(parts)))
        self.store.update(job,phase='needs_review',error_code='summary_too_large')

    def describe(self,job,creds,operation,content,final):
        saved=job['run']['profile'];provider=provider_of(saved['model'])
        prompt=saved['prompt']+'\n\n'+saved['fixed_rules']
        if not final:
            prompt+='\nЭто промежуточный фрагмент длинного разговора. Сохрани все важные факты, отрицания, суммы и условия; не делай общий вывод по всему разговору. Достаточно до 6000 символов.'
        input_hash=digest(json.dumps([content,prompt,saved['model'],saved['temperature']],ensure_ascii=False))
        estimate=model_cost(saved['model'],len(content.encode())+len(prompt.encode())+2048,3000)
        receipt=self.store.receipt(job,operation,input_hash,estimate)
        if receipt is None or not receipt.get('new'):return
        options={'api_key':creds[provider],'folder_id':creds['folder']}
        if provider=='grok':options['proxy']=creds.get('grok_proxy')
        client=self.agent.grok if provider=='grok' else self.agent.client if provider=='yandex' else self.agent.deepseek if provider=='deepseek' else self.agent.mimo
        if provider in ai_gateways.PROVIDERS:
            options|=ai_gateways.options(creds,provider)
            options['protocol']=creds['gateways'].get(provider,{}).get('protocol','chat')
        answer=client.complete(content,prompt=prompt,model=saved['model'],temperature=saved['temperature'],rules=False,max_tokens=3000,**options)
        result=visible(answer['text']);tokens=(answer['input_tokens'],answer['output_tokens'])
        cost=model_cost(saved['model'],*tokens)
        # Persist paid output first, even when the answer needs operator review.
        usable=not answer['truncated'] and bool(result.strip()) and len(result)<=6000
        self.store.complete(job,operation,result[:6000],cost=cost,tokens=tokens,kind='call_summary',usable=usable)
