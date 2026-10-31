"""Bounded authenticated audio ranges. Provider bearer URLs never reach browsers."""
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
from threading import BoundedSemaphore
from urllib.parse import urljoin, urlsplit

from center_messenger.auth_security import AuthError
from center_messenger.novofon_client import MEDIA_HOSTS, audio_kind

CHUNK=4*1024*1024
MAX_FILE=256*1024*1024
SLOTS=BoundedSemaphore(2)


def control_key(config):
    try:
        path=config.telephony_worker_key_file
        value=path.read_text().strip() if path and not path.is_symlink() else ''
        if not re.fullmatch('[a-f0-9]{64}',value):raise ValueError()
        return value
    except (OSError,ValueError):
        raise AuthError(503,'Сервис аудио телефонии пока не подключён.') from None


def requested_range(value):
    if not value:return 0,CHUNK-1
    match=re.fullmatch(r'bytes=(\d{1,12})-(\d{0,12})',value)
    if not match:raise AuthError(416,'Неподдерживаемый диапазон записи.')
    start=int(match[1]);end=int(match[2]) if match[2] else start+CHUNK-1
    if start>=MAX_FILE or end<start:raise AuthError(416,'Неподдерживаемый диапазон записи.')
    return start,min(end,start+CHUNK-1,MAX_FILE-1)


def location(url,resolver=socket.getaddrinfo):
    try:
        parsed=urlsplit(url)
        if (parsed.scheme!='https' or parsed.hostname not in MEDIA_HOSTS or parsed.port not in (None,443)
                or parsed.username or parsed.password or parsed.fragment or len(url)>4096
                or any(ord(c)<32 or c=='\\' for c in url)):raise ValueError()
        addresses=resolver(parsed.hostname,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ValueError()
        return parsed,addresses[0][4][0]
    except (ValueError,TypeError,OSError):
        raise AuthError(502,'Новофон вернул неподдерживаемый адрес аудио.') from None


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,address):
        super().__init__(host,timeout=12,context=ssl.create_default_context())
        self.address=address

    def connect(self):
        sock=socket.create_connection((self.address,443),self.timeout)
        try:self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except Exception:
            sock.close();raise


def fetch(url,range_header=None,*,resolver=socket.getaddrinfo,connection_factory=PinnedHTTPS):
    start,end=requested_range(range_header)
    if not SLOTS.acquire(blocking=False):raise AuthError(429,'Все каналы прослушивания заняты. Повторите позже.',retry_after=3)
    try:
        for hop in range(4):
            parsed,address=location(url,resolver)
            conn=connection_factory(parsed.hostname,address)
            try:
                conn.request('GET',parsed.path+('?' + parsed.query if parsed.query else ''),headers={
                    'Range':f'bytes={start}-{end}','Accept':'audio/*,application/octet-stream',
                    'User-Agent':'CenterMessenger-Telephony/1','Accept-Encoding':'identity'})
                response=conn.getresponse()
                if response.status in (301,302,303,307,308) and hop<3:
                    target=response.getheader('Location')
                    if not target:raise AuthError(502,'Запись пока недоступна.')
                    url=urljoin(url,target);continue
                mime=response.getheader('Content-Type','').split(';')[0].lower()
                if response.status not in (200,206) or mime in ('text/html','application/json'):
                    raise AuthError(502,'Запись пока недоступна. Повторите позже.')
                size=response.getheader('Content-Length','')
                if not size.isdecimal() or not 1<=int(size)<=CHUNK:
                    raise AuthError(502,'Новофон не поддержал порционное чтение этой записи.')
                if response.status==206:
                    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.getheader('Content-Range',''))
                    if not match:raise AuthError(502,'Неполный ответ аудиосервиса.')
                    first,last,total=map(int,match.groups())
                    if first!=start or last>end or last-first+1!=int(size) or not last<total<=MAX_FILE:
                        raise AuthError(502,'Неполный ответ аудиосервиса.')
                else:
                    if start:raise AuthError(502,'Новофон не поддержал перемотку этой записи.')
                    first,last,total=0,int(size)-1,int(size)
                body=response.read(int(size)+1)
                if len(body)!=int(size):raise AuthError(502,'Загрузка аудио прервалась. Повторите позже.')
                kind=audio_kind(body) if start==0 else None
                # Safari first asks for bytes=0-1 to discover range support.
                # A two-byte fragment cannot carry a complete container header;
                # accept only an explicitly declared audio type in that case.
                if start==0 and len(body)<12 and mime.startswith('audio/'):
                    kind='short_audio_fragment'
                if start==0 and not kind:raise AuthError(502,'Не удалось подтвердить формат аудиозаписи.')
                if mime not in ('audio/wav','audio/x-wav','audio/mpeg','audio/mp4','audio/ogg','audio/flac','audio/aac','audio/webm'):
                    mime={'wav':'audio/wav','ogg':'audio/ogg','mp3_or_aac':'audio/mpeg','flac':'audio/flac','mp4_container':'audio/mp4','webm_container':'audio/webm'}.get(kind,'application/octet-stream')
                return body,{'Content-Type':mime,'Content-Length':str(len(body)),
                    'Content-Range':f'bytes {first}-{last}/{total}','Accept-Ranges':'bytes',
                    'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'},206
            finally:conn.close()
        raise AuthError(502,'Слишком много перенаправлений аудио.')
    except (OSError,http.client.HTTPException):
        raise AuthError(502,'Не удалось загрузить аудио. Повторите позже.') from None
    finally:SLOTS.release()


class WorkerAudio:
    def __init__(self,config):self.config=config

    def read(self,workspace,call,range_header):
        requested_range(range_header)
        conn=http.client.HTTPConnection('telephony-worker',8002,timeout=30)
        try:
            headers={'X-CM-Telephony-Key':control_key(self.config),'Content-Type':'application/json'}
            if range_header:headers['Range']=range_header
            conn.request('POST','/recording',json.dumps({'workspace':str(workspace),'call':str(call)}),headers)
            response=conn.getresponse()
            body=response.read(CHUNK+1)
            if response.status!=206 or len(body)>CHUNK:
                raise AuthError(response.status if response.status in (404,416,429,503) else 502,
                                'Запись пока недоступна. Повторите позже.')
            allowed=('Content-Type','Content-Length','Content-Range','Accept-Ranges')
            return body,{key:response.getheader(key) for key in allowed if response.getheader(key)},206
        except (OSError,http.client.HTTPException):
            raise AuthError(503,'Сервис аудио телефонии временно недоступен.') from None
        finally:conn.close()
