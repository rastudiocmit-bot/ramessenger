"""One-off proxy access test. Never configures application transports or stores secrets."""
import base64
from dataclasses import dataclass, field
import http.client
import ipaddress
import socket
import ssl
from time import monotonic

from center_messenger.auth_security import AuthError

HOST = 'api.x.ai'
URLS = {f'https://{HOST}/v1/language-models', f'https://{HOST}/v1/api-key'}
ERRORS = {
    'proxy_auth': 'Прокси не принял логин или пароль.',
    'proxy_network': 'Сервер не смог установить соединение через прокси. Проверьте доступность адреса и порта.',
    'proxy_protocol': 'Не удалось согласовать протокол прокси. Выберите HTTP CONNECT или SOCKS5 вручную.',
    'proxy_denied': 'Прокси отказал в соединении с api.x.ai:443.',
    'proxy_tls': 'Не удалось подтвердить защищённое соединение с xAI через прокси. Проверка сертификата не отключалась.',
}


class ProxyFailure(OSError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    protocol: str = 'auto'


def parse(value, protocol='auto'):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 1024 or protocol not in ('auto', 'http', 'socks5'):
            raise ValueError()
        host, port, user, password = value.strip().split(':', 3)
        address = ipaddress.ip_address(host)
        if (address.version != 4 or not address.is_global or address.is_multicast
                or not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535
                or any(c in user + password for c in '\r\n\x00')
                or not 1 <= len(user.encode()) <= 255 or not 1 <= len(password.encode()) <= 255):
            raise ValueError()
        return Proxy(str(address), int(port), user, password, protocol)
    except (ValueError, TypeError):
        raise AuthError(422, 'Введите публичный IPv4:порт:логин:пароль. Поддерживаются HTTP CONNECT и SOCKS5.') from None


class Reply:
    def __init__(self, response, connection):
        self.response, self.connection, self.status = response, connection, response.status
    def read(self, limit):
        return self.response.read(limit)
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.response.close()
        self.connection.close()


class ProxyOpener:
    def __init__(self, config):
        self.config = config
        self.protocol = None if config.protocol == 'auto' else config.protocol
        self.tunnel = self.tls = False
        self.failure = None

    @staticmethod
    def remaining(deadline):
        left = deadline - monotonic()
        if left <= 0:
            raise TimeoutError()
        return left

    def receive(self, sock, size, deadline):
        value = bytearray()
        while len(value) < size:
            sock.settimeout(self.remaining(deadline))
            part = sock.recv(size - len(value))
            if not part:
                raise ProxyFailure('proxy_protocol')
            value.extend(part)
        return bytes(value)

    def socks(self, sock, deadline):
        sock.sendall(b'\x05\x02\x00\x02')
        version, method = self.receive(sock, 2, deadline)
        if version == 5 and method == 255:
            raise ProxyFailure('proxy_auth')
        if version != 5 or method not in (0, 2):
            raise ProxyFailure('proxy_protocol')
        if method == 2:
            user, password = self.config.username.encode(), self.config.password.encode()
            sock.sendall(b'\x01' + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if self.receive(sock, 2, deadline) != b'\x01\x00':
                raise ProxyFailure('proxy_auth')
        host = HOST.encode('ascii')
        sock.sendall(b'\x05\x01\x00\x03' + bytes([len(host)]) + host + (443).to_bytes(2, 'big'))
        version, code, reserved, kind = self.receive(sock, 4, deadline)
        if version != 5 or reserved != 0:
            raise ProxyFailure('proxy_protocol')
        if code:
            raise ProxyFailure('proxy_denied')
        size = 4 if kind == 1 else 16 if kind == 4 else self.receive(sock, 1, deadline)[0] if kind == 3 else 0
        if not size:
            raise ProxyFailure('proxy_protocol')
        self.receive(sock, size + 2, deadline)

    def http(self, sock, deadline):
        auth = base64.b64encode((self.config.username + ':' + self.config.password).encode()).decode('ascii')
        sock.sendall((f'CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n'
                      f'Proxy-Authorization: Basic {auth}\r\n\r\n').encode('ascii'))
        header = bytearray()
        while not header.endswith(b'\r\n\r\n') and len(header) < 8192:
            header.extend(self.receive(sock, 1, deadline))
        try:
            first = header.split(b'\r\n', 1)[0].split()
            if not header.endswith(b'\r\n\r\n') or first[0] not in (b'HTTP/1.0', b'HTTP/1.1'):
                raise ValueError()
            status = int(first[1])
        except (ValueError, IndexError):
            raise ProxyFailure('proxy_protocol') from None
        if status != 200:
            raise ProxyFailure('proxy_auth' if status == 407 else 'proxy_denied')

    def connect(self, protocol, timeout):
        deadline = monotonic() + timeout
        sock = socket.create_connection((self.config.host, self.config.port), timeout=timeout)
        try:
            (self.socks if protocol == 'socks5' else self.http)(sock, deadline)
            return sock, deadline
        except Exception:
            sock.close()
            raise

    def open(self, request, timeout=7):
        if request.full_url not in URLS or request.get_method() != 'GET' or request.data is not None:
            raise ValueError('Unsupported proxy diagnostic destination')
        sock = connection = None
        try:
            # Negotiate only the two supported protocols; never retry a provider request.
            choices = [self.protocol] if self.protocol else ['socks5', 'http']
            for index, protocol in enumerate(choices):
                try:
                    sock, deadline = self.connect(protocol, min(float(timeout), 7))
                    self.protocol, self.tunnel = protocol, True
                    break
                except (ProxyFailure, TimeoutError) as error:
                    if (index + 1 == len(choices) or isinstance(error, ProxyFailure)
                            and error.code != 'proxy_protocol'):
                        raise
            context = ssl.create_default_context()
            context.set_alpn_protocols(['http/1.1'])
            sock.settimeout(self.remaining(deadline))
            sock = context.wrap_socket(sock, server_hostname=HOST)
            self.tls = True
            connection = http.client.HTTPSConnection(HOST, 443, timeout=self.remaining(deadline), context=context)
            connection.sock = sock
            connection.request('GET', request.selector,
                headers={'Authorization': request.get_header('Authorization'), 'Accept': 'application/json'})
            return Reply(connection.getresponse(), connection)
        except (OSError, http.client.HTTPException) as error:
            self.failure = (error.code if isinstance(error, ProxyFailure) else 'proxy_tls'
                            if isinstance(error, ssl.SSLError) else 'proxy_network')
            if connection:
                connection.close()
            if sock:
                sock.close()
            raise ProxyFailure(self.failure) from None

    def public(self):
        return {'protocol': self.protocol, 'tunnel': self.tunnel, 'tls_verified': self.tls,
                'code': self.failure, 'message': ERRORS.get(self.failure, 'Соединение с xAI через прокси проверено с проверкой TLS-сертификата.')}
