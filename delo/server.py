"""Small dependency-free HTTP adapter, for synthetic demonstration only."""
import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from .model import Store, DomainError, KINDS, STATUSES, ROLES

WEB = Path(__file__).parent / 'web'
MAX_BODY = 65536


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DomainError('Повторяющееся поле JSON')
        result[key] = value
    return result


def make_server(store, host='127.0.0.1', port=8080):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'DeloDemo/1.0'
        sys_version = ''

        def log_message(self, fmt, *args):
            logging.info('%s %s', self.address_string(), fmt % args)

        def send(self, code, body, mime='application/json; charset=utf-8', extra=None):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; "
                             "style-src 'self'; img-src 'self' data:; frame-ancestors *")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def user(self):
            raw = self.headers.get('X-Demo-User', '')
            if not raw.isdecimal() or len(raw) > 9 or int(raw) < 1:
                raise DomainError('Выберите учебного пользователя', 403)
            return int(raw)

        def filters(self, query):
            raw = parse_qs(query, keep_blank_values=True)
            if any(len(v) != 1 for v in raw.values()):
                raise DomainError('Повторяющийся параметр фильтра')
            return {k:v[0] for k,v in raw.items()}

        def body(self):
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc.lower() != self.headers.get('Host', '').lower():
                raise DomainError('Запрос изменения с другого сайта запрещён', 403)
            if self.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
                raise DomainError('Для записи требуется application/json', 415)
            try:
                size = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                raise DomainError('Некорректная длина запроса') from None
            if size < 0:
                raise DomainError('Не указана длина запроса')
            if size > MAX_BODY:
                raise DomainError('Запрос больше 64 КиБ', 413)
            try:
                data = json.loads(self.rfile.read(size).decode('utf-8'),
                    object_pairs_hook=unique_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(DomainError('Недопустимое число JSON')))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise DomainError('Некорректный JSON') from None
            if not isinstance(data, dict):
                raise DomainError('Тело JSON должно быть объектом')
            return data

        def dispatch(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if self.command in ('GET', 'HEAD'):
                if path == '/api/health':
                    self.send(200, {'status':'ok', 'mode':'synthetic-demo', 'version':'1.0.0'})
                elif path == '/api/meta':
                    self.send(200, {'users':store.users(), 'kinds':KINDS, 'statuses':STATUSES,
                                    'roles':ROLES, 'demo':True})
                elif path == '/api/documents':
                    self.send(200, store.list(self.user(), self.filters(parsed.query)))
                elif path == '/api/export.csv':
                    self.send(200, store.export_csv(self.user(), self.filters(parsed.query)),
                        'text/csv; charset=utf-8', {'Content-Disposition':'attachment; filename="documents.csv"'})
                elif re.fullmatch(r'/api/documents/[1-9]\d*', path):
                    self.send(200, store.get(self.user(), int(path.rsplit('/',1)[1])))
                elif path in ('/', '/index.html', '/app.js', '/style.css'):
                    name = 'index.html' if path == '/' else path[1:]
                    mime = {'index.html':'text/html', 'app.js':'text/javascript', 'style.css':'text/css'}[name]
                    self.send(200, (WEB/name).read_bytes(), mime+'; charset=utf-8')
                else:
                    raise DomainError('Ресурс не найден', 404)
            elif self.command == 'POST' and path == '/api/documents':
                actor = self.user()
                self.send(201, {'document':store.create(actor, self.body())})
            elif self.command == 'PATCH' and re.fullmatch(r'/api/documents/[1-9]\d*', path):
                actor = self.user()
                self.send(200, {'document':store.edit(actor, int(path.rsplit('/',1)[1]), self.body())})
            elif self.command == 'POST' and re.fullmatch(r'/api/documents/[1-9]\d*/(assign|complete|archive)', path):
                actor = self.user()
                doc_id, action = path.split('/')[3:5]
                method = getattr(store, action)
                self.send(200, {'document':method(actor, int(doc_id), self.body())})
            else:
                raise DomainError('Ресурс или действие не найдены', 404)

        def handle_request(self):
            try:
                self.dispatch()
            except DomainError as e:
                self.send(e.status, {'error':str(e)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logging.exception('Internal demonstration server error')
                self.send(500, {'error':'Внутренняя ошибка. Проверьте журнал сервера'})

        do_GET = do_HEAD = do_POST = do_PATCH = handle_request

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description='ИС «Делопроизводство»: только учебная демонстрация')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--db', default='data/demo.sqlite3')
    parser.add_argument('--demo', action='store_true', required=True,
                        help='Явное согласие на режим выбора учебного пользователя без аутентификации')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    store = Store(args.db)
    store.initialize(demo=True)
    seeded = store.seed_demo()
    server = make_server(store, args.host, args.port)
    logging.info('SYNTHETIC DEMO ONLY. No authentication. Never use real documents.')
    logging.info('Listening on %s:%d; demo fixtures created=%s', args.host, server.server_port, seeded)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
