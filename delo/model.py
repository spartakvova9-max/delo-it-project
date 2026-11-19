"""Transactional domain model for the educational document register.

No real authentication is claimed: the HTTP demo explicitly selects a user.
All authorization and state checks still live here, not in browser controls.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
import csv
import io
import json
import sqlite3

KINDS = {'incoming': 'Входящий', 'outgoing': 'Исходящий', 'internal': 'Внутренний'}
PREFIXES = {'incoming': 'ВХ', 'outgoing': 'ИСХ', 'internal': 'ВН'}
STATUSES = {'registered': 'Зарегистрирован', 'in_progress': 'В работе',
            'completed': 'Исполнен', 'archived': 'Архив'}
ROLES = {'registrar': 'Регистратор', 'manager': 'Руководитель',
         'executor': 'Исполнитель', 'auditor': 'Аудитор'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, display_name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('registrar','manager','executor','auditor')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
CREATE TABLE IF NOT EXISTS counters (
 kind TEXT NOT NULL, year INTEGER NOT NULL, value INTEGER NOT NULL CHECK(value>0),
 PRIMARY KEY(kind,year));
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind IN ('incoming','outgoing','internal')),
 title TEXT NOT NULL, correspondent TEXT NOT NULL,
 registered_on TEXT NOT NULL, due_on TEXT NOT NULL,
 assignee_id INTEGER REFERENCES users(id),
 status TEXT NOT NULL CHECK(status IN ('registered','in_progress','completed','archived')),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), result TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
 actor_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL,
 details TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_documents_dates ON documents(registered_on,due_on);
CREATE INDEX IF NOT EXISTS ix_events_document ON events(document_id,id);
"""

class DomainError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f'{label}: заполните текстовое поле')
    value = value.strip()
    if len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise DomainError(f'{label}: недопустимый текст или длина больше {maximum}')
    return value


def integer(value, label):
    if type(value) is not int or value < 1:
        raise DomainError(f'{label}: требуется положительное целое число')
    return value


def iso_date(value, label):
    if not isinstance(value, str) or len(value) != 10:
        raise DomainError(f'{label}: требуется дата ГГГГ-ММ-ДД')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise DomainError(f'{label}: некорректная дата') from None
    if parsed.isoformat() != value:
        raise DomainError(f'{label}: требуется дата ГГГГ-ММ-ДД')
    return value


def fields(data, required, optional=()):
    if not isinstance(data, dict) or set(required) - data.keys():
        raise DomainError('Отсутствуют обязательные поля запроса')
    if data.keys() - set(required) - set(optional):
        raise DomainError('Неизвестное поле запроса')


class Store:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connection(self, write=False):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA busy_timeout=15000')
        try:
            if write:
                con.execute('BEGIN IMMEDIATE')
            yield con
            if write:
                con.commit()
        except Exception:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def initialize(self, demo=False):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript(SCHEMA)
        if demo:
            with self.connection(True) as con:
                con.executemany('INSERT OR IGNORE INTO users(id,display_name,role) VALUES(?,?,?)', [
                    (1, 'Учебный регистратор', 'registrar'),
                    (2, 'Учебный руководитель', 'manager'),
                    (3, 'Учебный исполнитель А', 'executor'),
                    (4, 'Учебный аудитор', 'auditor'),
                    (5, 'Учебный исполнитель Б', 'executor')])

    @staticmethod
    def actor(con, user_id, role=None):
        if type(user_id) is not int or user_id < 1:
            raise DomainError('Выберите действующего учебного пользователя', 403)
        user = con.execute('SELECT * FROM users WHERE id=? AND active=1', (user_id,)).fetchone()
        if not user or (role is not None and user['role'] != role):
            raise DomainError('Недостаточно прав для этого действия', 403)
        return user

    @staticmethod
    def document(con, doc_id):
        integer(doc_id, 'Идентификатор документа')
        row = con.execute('SELECT d.*,u.display_name AS assignee_name FROM documents d '
                          'LEFT JOIN users u ON u.id=d.assignee_id WHERE d.id=?', (doc_id,)).fetchone()
        if row is None:
            raise DomainError('Документ не найден', 404)
        return dict(row)

    @staticmethod
    def version(doc, incoming):
        integer(incoming, 'Версия')
        if incoming != doc['version']:
            raise DomainError('Карточка уже изменена. Обновите данные и повторите действие', 409)

    @staticmethod
    def event(con, doc_id, actor_id, action, details):
        con.execute('INSERT INTO events(document_id,actor_id,action,details,created_at) '
                    'VALUES(?,?,?,?,?)', (doc_id, actor_id, action,
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(timespec='seconds')))

    def users(self):
        with self.connection() as con:
            return [dict(r) for r in con.execute(
                'SELECT id,display_name,role FROM users WHERE active=1 ORDER BY id')]

    def create(self, actor_id, data):
        with self.connection(True) as con:
            self.actor(con, actor_id, 'registrar')
            fields(data, ('kind', 'title', 'correspondent', 'registered_on', 'due_on'))
            kind = data['kind']
            if not isinstance(kind, str) or kind not in KINDS:
                raise DomainError('Неизвестный вид документа')
            title = text(data['title'], 'Тема')
            correspondent = text(data['correspondent'], 'Корреспондент')
            registered = iso_date(data['registered_on'], 'Дата регистрации')
            due = iso_date(data['due_on'], 'Срок исполнения')
            if due < registered:
                raise DomainError('Срок исполнения не может быть раньше регистрации')
            year = int(registered[:4])
            con.execute('INSERT INTO counters(kind,year,value) VALUES(?,?,1) '
                        'ON CONFLICT(kind,year) DO UPDATE SET value=value+1', (kind, year))
            counter = con.execute('SELECT value FROM counters WHERE kind=? AND year=?',
                                  (kind, year)).fetchone()[0]
            number = f'{PREFIXES[kind]}-{year}-{counter:04d}'
            cursor = con.execute('INSERT INTO documents(number,kind,title,correspondent,registered_on,'
                'due_on,status) VALUES(?,?,?,?,?,?,?)',
                (number, kind, title, correspondent, registered, due, 'registered'))
            doc_id = cursor.lastrowid
            self.event(con, doc_id, actor_id, 'registered', {'number': number})
            return self.document(con, doc_id)

    def edit(self, actor_id, doc_id, data):
        with self.connection(True) as con:
            self.actor(con, actor_id, 'registrar')
            fields(data, ('version',), ('title', 'correspondent', 'due_on'))
            doc = self.document(con, doc_id)
            self.version(doc, data['version'])
            if doc['status'] != 'registered':
                raise DomainError('Исправление доступно только до назначения исполнителя', 409)
            changes = {}
            for key, label in [('title', 'Тема'), ('correspondent', 'Корреспондент')]:
                if key in data:
                    changes[key] = text(data[key], label)
            if 'due_on' in data:
                changes['due_on'] = iso_date(data['due_on'], 'Срок исполнения')
                if changes['due_on'] < doc['registered_on']:
                    raise DomainError('Срок исполнения не может быть раньше регистрации')
            changes = {k: v for k, v in changes.items() if doc[k] != v}
            if not changes:
                raise DomainError('Нет изменений для сохранения', 409)
            # Column names are selected above, never taken from untrusted JSON keys.
            assignments = ','.join(k+'=?' for k in changes)
            con.execute(f'UPDATE documents SET {assignments},version=version+1 WHERE id=?',
                        (*changes.values(), doc_id))
            self.event(con, doc_id, actor_id, 'edited', changes)
            return self.document(con, doc_id)

    def assign(self, actor_id, doc_id, data):
        with self.connection(True) as con:
            self.actor(con, actor_id, 'manager')
            fields(data, ('version', 'assignee_id'))
            doc = self.document(con, doc_id)
            self.version(doc, data['version'])
            if doc['status'] not in ('registered', 'in_progress'):
                raise DomainError('Назначение недоступно для исполненного или архивного документа', 409)
            assignee = integer(data['assignee_id'], 'Исполнитель')
            user = con.execute('SELECT * FROM users WHERE id=? AND active=1 AND role=?',
                               (assignee, 'executor')).fetchone()
            if user is None:
                raise DomainError('Выберите действующего пользователя с ролью «Исполнитель»')
            if doc['status'] == 'in_progress' and doc['assignee_id'] == assignee:
                raise DomainError('Этот исполнитель уже назначен', 409)
            con.execute('UPDATE documents SET assignee_id=?,status=?,version=version+1 WHERE id=?',
                        (assignee, 'in_progress', doc_id))
            self.event(con, doc_id, actor_id, 'assigned', {'assignee_id': assignee})
            return self.document(con, doc_id)

    def complete(self, actor_id, doc_id, data):
        with self.connection(True) as con:
            self.actor(con, actor_id, 'executor')
            fields(data, ('version', 'result'))
            doc = self.document(con, doc_id)
            self.version(doc, data['version'])
            if doc['assignee_id'] != actor_id:
                raise DomainError('Исполнять документ может только назначенный сотрудник', 403)
            if doc['status'] != 'in_progress':
                raise DomainError('Исполнение доступно только для документа в работе', 409)
            result = text(data['result'], 'Результат исполнения', 2000)
            con.execute('UPDATE documents SET status=?,result=?,version=version+1 WHERE id=?',
                        ('completed', result, doc_id))
            self.event(con, doc_id, actor_id, 'completed', {'result': result})
            return self.document(con, doc_id)

    def archive(self, actor_id, doc_id, data):
        with self.connection(True) as con:
            self.actor(con, actor_id, 'manager')
            fields(data, ('version',))
            doc = self.document(con, doc_id)
            self.version(doc, data['version'])
            if doc['status'] != 'completed':
                raise DomainError('В архив можно поместить только исполненный документ', 409)
            con.execute('UPDATE documents SET status=?,version=version+1 WHERE id=?',
                        ('archived', doc_id))
            self.event(con, doc_id, actor_id, 'archived', {})
            return self.document(con, doc_id)

    def get(self, actor_id, doc_id):
        with self.connection() as con:
            self.actor(con, actor_id)
            doc = self.document(con, doc_id)
            events = [dict(r) for r in con.execute(
                'SELECT e.*,u.display_name AS actor_name FROM events e '
                'JOIN users u ON u.id=e.actor_id WHERE document_id=? ORDER BY e.id', (doc_id,))]
            for event in events:
                event['details'] = json.loads(event['details'])
            return {'document': doc, 'events': events}

    def list(self, actor_id, filters=None):
        filters = {} if filters is None else filters
        fields(filters, (), ('q', 'kind', 'status', 'from', 'to', 'overdue', 'as_of'))
        for value in filters.values():
            if not isinstance(value, str) or len(value) > 200:
                raise DomainError('Некорректное значение фильтра')
        kind, status = filters.get('kind', ''), filters.get('status', '')
        if kind and kind not in KINDS:
            raise DomainError('Неизвестный вид документа')
        if status and status not in STATUSES:
            raise DomainError('Неизвестное состояние документа')
        start = iso_date(filters['from'], 'Начало периода') if filters.get('from') else ''
        end = iso_date(filters['to'], 'Конец периода') if filters.get('to') else ''
        if start and end and start > end:
            raise DomainError('Начало периода не может быть позже окончания')
        as_of = iso_date(filters['as_of'], 'Контрольная дата') if filters.get('as_of') else date.today().isoformat()
        if filters.get('overdue', '') not in ('', '0', '1'):
            raise DomainError('Некорректный фильтр просрочки')
        query = filters.get('q', '').strip().casefold()
        with self.connection() as con:
            self.actor(con, actor_id)
            rows = [dict(r) for r in con.execute('SELECT d.*,u.display_name AS assignee_name '
                'FROM documents d LEFT JOIN users u ON u.id=d.assignee_id '
                'ORDER BY d.registered_on DESC,d.id DESC')]
        selected = []
        for doc in rows:
            doc['is_overdue'] = doc['due_on'] < as_of and doc['status'] in ('registered', 'in_progress')
            haystack = ' '.join(str(doc[k] or '') for k in ('number', 'title', 'correspondent', 'assignee_name')).casefold()
            if query and query not in haystack: continue
            if kind and doc['kind'] != kind: continue
            if status and doc['status'] != status: continue
            if start and doc['registered_on'] < start: continue
            if end and doc['registered_on'] > end: continue
            if filters.get('overdue') == '1' and not doc['is_overdue']: continue
            selected.append(doc)
        stats = {'total': len(selected),
                 'active': sum(d['status'] in ('registered', 'in_progress') for d in selected),
                 'overdue': sum(d['is_overdue'] for d in selected),
                 'completed': sum(d['status'] == 'completed' for d in selected),
                 'archived': sum(d['status'] == 'archived' for d in selected)}
        return {'documents': selected, 'stats': stats, 'as_of': as_of}

    @staticmethod
    def csv_safe(value):
        value = str(value or '')
        if value[:1] in ('\t', '\r', '\n') or value.lstrip(' \t\r\n')[:1] in ('=', '+', '-', '@'):
            return "'" + value
        return value

    def export_csv(self, actor_id, filters=None):
        result = self.list(actor_id, filters)
        output = io.StringIO(newline='')
        writer = csv.writer(output, delimiter=';')
        writer.writerow(['Номер', 'Вид', 'Тема', 'Корреспондент', 'Регистрация',
                         'Срок', 'Состояние', 'Исполнитель', 'Результат'])
        for d in result['documents']:
            writer.writerow([self.csv_safe(v) for v in [d['number'], KINDS[d['kind']],
                d['title'], d['correspondent'], d['registered_on'], d['due_on'],
                STATUSES[d['status']], d['assignee_name'], d['result']]])
        return output.getvalue().encode('utf-8-sig')

    def seed_demo(self):
        with self.connection() as con:
            if con.execute('SELECT count(*) FROM documents').fetchone()[0]:
                return False
        examples = [
            ('incoming', 'Заявка на поставку канцелярии', 'ООО «Северный офис»', '2026-11-02', '2026-11-15', 0),
            ('internal', 'Подготовка графика отпусков', 'Отдел кадров', '2026-11-04', '2026-11-20', 1),
            ('outgoing', 'Ответ на обращение партнёра', 'АО «Ритм»', '2026-11-06', '2026-11-17', 2),
            ('incoming', 'Согласование сметы ремонта', 'ООО «Маяк»', '2026-11-09', '2026-11-18', 3),
            ('internal', 'Инвентаризация оборудования', 'ИТ-отдел', '2026-11-10', '2026-11-16', 4),
            ('outgoing', 'Подтверждение участия в семинаре', 'Учебный центр', '2026-11-12', '2026-11-25', 0)]
        for kind, title, correspondent, registered, due, stage in examples:
            doc = self.create(1, dict(kind=kind, title=title, correspondent=correspondent,
                                     registered_on=registered, due_on=due))
            if stage:
                assignee = 5 if stage == 2 else 3
                doc = self.assign(2, doc['id'], {'version':doc['version'], 'assignee_id':assignee})
                if stage >= 3:
                    doc = self.complete(assignee, doc['id'], {'version':doc['version'],
                        'result':'Учебная проверка завершена, результат согласован'})
                if stage == 4:
                    self.archive(2, doc['id'], {'version':doc['version']})
        return True
