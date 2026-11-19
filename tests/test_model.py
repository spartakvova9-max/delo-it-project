import csv
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from delo.model import Store, DomainError

BASE = dict(kind='incoming', title='Согласование графика', correspondent='Учебный отдел',
            registered_on='2026-11-01', due_on='2026-11-18')

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'test.sqlite3')
        self.store.initialize(demo=True)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, **changes):
        return self.store.create(1, {**BASE, **changes})

    def in_progress(self, assignee=3):
        d = self.create()
        return self.store.assign(2, d['id'], {'version':d['version'], 'assignee_id':assignee})

    def completed(self):
        d = self.in_progress()
        return self.store.complete(3,d['id'],{'version':d['version'], 'result':'Учебное поручение выполнено'})

    def snapshot(self):
        with self.store.connection() as con:
            return {t:[tuple(r) for r in con.execute('SELECT * FROM '+t+' ORDER BY 1,2')]
                    for t in ('users','documents','counters','events')}

    def reject(self, code, callback):
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(caught.exception.status, code)
        self.assertEqual(before, self.snapshot(), 'Rejected request changed persistent data')

    def test_01_demo_users_have_four_roles(self):
        self.assertEqual(len(self.store.users()),5)
        self.assertEqual({u['role'] for u in self.store.users()}, {'registrar','manager','executor','auditor'})

    def test_02_registration_and_event(self):
        d = self.create()
        self.assertEqual((d['number'],d['status'],d['version']),('ВХ-2026-0001','registered',1))
        detail = self.store.get(4,d['id'])
        self.assertEqual([e['action'] for e in detail['events']],['registered'])
        self.assertEqual(detail['events'][0]['actor_id'],1)

    def test_03_counters_separate_by_year_and_kind(self):
        self.assertEqual(self.create()['number'],'ВХ-2026-0001')
        self.assertEqual(self.create()['number'],'ВХ-2026-0002')
        self.assertEqual(self.create(kind='outgoing')['number'],'ИСХ-2026-0001')
        self.assertEqual(self.create(registered_on='2027-01-01',due_on='2027-01-02')['number'],'ВХ-2027-0001')

    def test_04_nonregistrars_cannot_create(self):
        for actor in (2,3,4,5,999,0,True,'1',None):
            with self.subTest(actor=actor):
                self.reject(403,lambda:self.store.create(actor,BASE))

    def test_05_blank_and_wrong_text_types(self):
        for key in ('title','correspondent'):
            for value in ('','  ',None,23,[],{}):
                with self.subTest(key=key,value=value):
                    self.reject(400,lambda:self.create(**{key:value}))

    def test_06_unknown_kind_and_wrong_types(self):
        for value in ('unknown','INCOMING',None,[],{},4):
            with self.subTest(value=value):
                self.reject(400,lambda:self.create(kind=value))

    def test_07_date_validation_and_order(self):
        for key,value in [('registered_on','2026-02-30'),('registered_on','2026-1-01'),
                          ('due_on','2026-10-31'),('due_on','2026-11-18T00:00:00'),('due_on',None)]:
            with self.subTest(key=key,value=value):
                self.reject(400,lambda:self.create(**{key:value}))

    def test_08_missing_and_unknown_fields(self):
        for data in ({}, {**BASE,'status':'archived'}, {k:v for k,v in BASE.items() if k!='title'}, [], None):
            with self.subTest(data=data):
                self.reject(400,lambda:self.store.create(1,data))

    def test_09_maximum_length_and_controls(self):
        self.assertEqual(len(self.create(title='Я'*200)['title']),200)
        self.reject(400,lambda:self.create(title='Я'*201))
        self.reject(400,lambda:self.create(title='Название\x00'))

    def test_10_sql_and_html_are_literal_text(self):
        title="<img src=x onerror=alert(1)> '; DROP TABLE documents;--"
        d = self.create(title=title)
        self.assertEqual(self.store.get(1,d['id'])['document']['title'],title)
        self.assertEqual(len(self.store.list(4)['documents']),1)

    def test_11_registrar_edits_registered(self):
        d = self.create()
        edited=self.store.edit(1,d['id'],{'version':1,'title':'Новая тема','due_on':'2026-11-20'})
        self.assertEqual((edited['version'],edited['title']),(2,'Новая тема'))
        self.assertEqual([e['action'] for e in self.store.get(4,d['id'])['events']],['registered','edited'])

    def test_12_noop_edit_and_unknown_fields_rejected(self):
        d = self.create()
        for data in ({'version':1},{'version':1,'title':d['title']},{'version':1,'number':'fake'}):
            self.reject(409 if 'number' not in data else 400, lambda:self.store.edit(1,d['id'],data))

    def test_13_only_registrar_can_edit(self):
        d=self.create()
        for actor in (2,3,4,5):
            self.reject(403,lambda:self.store.edit(actor,d['id'],{'version':1,'title':'Изменение'}))

    def test_14_edit_not_allowed_after_assignment(self):
        d=self.in_progress()
        self.reject(409,lambda:self.store.edit(1,d['id'],{'version':d['version'],'title':'Изменение'}))

    def test_15_stale_version_preserves_first_change(self):
        d=self.create()
        self.store.edit(1,d['id'],{'version':1,'title':'Первое изменение'})
        self.reject(409,lambda:self.store.edit(1,d['id'],{'version':1,'title':'Потеря первого изменения'}))
        self.assertEqual(self.store.get(4,d['id'])['document']['title'],'Первое изменение')

    def test_16_version_requires_positive_integer(self):
        d=self.create()
        for version in (True,0,-1,'1',None,1.0):
            self.reject(400,lambda:self.store.edit(1,d['id'],{'version':version,'title':'Изменение'}))

    def test_17_assign_sets_state_and_event(self):
        d=self.in_progress()
        self.assertEqual((d['status'],d['assignee_id'],d['version']),('in_progress',3,2))
        self.assertEqual(self.store.get(4,d['id'])['events'][-1]['action'],'assigned')

    def test_18_only_manager_assigns(self):
        d=self.create()
        for actor in (1,3,4,5):
            self.reject(403,lambda:self.store.assign(actor,d['id'],{'version':1,'assignee_id':3}))

    def test_19_assignee_must_be_active_executor(self):
        d=self.create()
        for assignee in (1,2,4,999,True,'3',0):
            self.reject(400,lambda:self.store.assign(2,d['id'],{'version':1,'assignee_id':assignee}))
        with self.store.connection(True) as con:
            con.execute('UPDATE users SET active=0 WHERE id=5')
        self.reject(400,lambda:self.store.assign(2,d['id'],{'version':1,'assignee_id':5}))

    def test_20_reassign_without_duplicate_noop(self):
        d=self.in_progress()
        self.reject(409,lambda:self.store.assign(2,d['id'],{'version':2,'assignee_id':3}))
        d=self.store.assign(2,d['id'],{'version':2,'assignee_id':5})
        self.assertEqual((d['assignee_id'],d['version']),(5,3))

    def test_21_assigned_executor_completes(self):
        d=self.completed()
        self.assertEqual((d['status'],d['version']),('completed',3))
        self.assertTrue(d['result'])

    def test_22_other_executor_cannot_complete(self):
        d=self.in_progress()
        self.reject(403,lambda:self.store.complete(5,d['id'],{'version':2,'result':'Чужая работа'}))

    def test_23_nonexecutors_cannot_complete(self):
        d=self.in_progress()
        for actor in (1,2,4):
            self.reject(403,lambda:self.store.complete(actor,d['id'],{'version':2,'result':'Готово'}))

    def test_24_completion_result_required_and_bounded(self):
        d=self.in_progress()
        for value in ('',' ',None,[],'я'*2001):
            self.reject(400,lambda:self.store.complete(3,d['id'],{'version':2,'result':value}))
        d=self.store.complete(3,d['id'],{'version':2,'result':'я'*2000})
        self.assertEqual(len(d['result']),2000)

    def test_25_duplicate_completion_rejected(self):
        d=self.completed()
        self.reject(409,lambda:self.store.complete(3,d['id'],{'version':3,'result':'Повтор'}))

    def test_26_manager_archives_completed(self):
        d=self.completed(); d=self.store.archive(2,d['id'],{'version':3})
        self.assertEqual((d['status'],d['version']),('archived',4))
        self.assertEqual([e['action'] for e in self.store.get(4,d['id'])['events']],
                         ['registered','assigned','completed','archived'])

    def test_27_only_manager_archives(self):
        d=self.completed()
        for actor in (1,3,4,5):
            self.reject(403,lambda:self.store.archive(actor,d['id'],{'version':3}))

    def test_28_cannot_archive_registered_or_inprogress(self):
        for d in (self.create(),self.in_progress()):
            self.reject(409,lambda:self.store.archive(2,d['id'],{'version':d['version']}))

    def test_29_terminal_states_cannot_be_assigned(self):
        d=self.completed()
        self.reject(409,lambda:self.store.assign(2,d['id'],{'version':3,'assignee_id':5}))
        d=self.store.archive(2,d['id'],{'version':3})
        self.reject(409,lambda:self.store.assign(2,d['id'],{'version':4,'assignee_id':5}))
        self.reject(409,lambda:self.store.archive(2,d['id'],{'version':4}))

    def test_30_every_active_role_can_read(self):
        d=self.create()
        for actor in (1,2,3,4,5):
            self.assertEqual(self.store.get(actor,d['id'])['document']['id'],d['id'])
            self.assertEqual(self.store.list(actor)['stats']['total'],1)

    def test_31_inactive_and_unknown_users_cannot_read(self):
        d=self.create()
        with self.store.connection(True) as con:con.execute('UPDATE users SET active=0 WHERE id=4')
        for actor in (4,999,True,'1',None):
            self.reject(403,lambda:self.store.get(actor,d['id']))

    def test_32_missing_document_is_404(self):
        self.reject(404,lambda:self.store.get(4,999))
        self.reject(404,lambda:self.store.assign(2,999,{'version':1,'assignee_id':3}))

    def test_33_unicode_casefold_search(self):
        d=self.create(title='График Ёлок',correspondent='УЧЕБНЫЙ ЦЕНТР')
        for q in ('гРАФИК','ёЛОК','учебный','ВХ-2026-0001'):
            self.assertEqual([r['id'] for r in self.store.list(4,{'q':q})['documents']],[d['id']])
        self.assertEqual(self.store.list(4,{'q':'не найдено'})['documents'],[])

    def test_34_period_includes_both_boundaries(self):
        self.create(registered_on='2026-11-01');self.create(registered_on='2026-11-10');self.create(registered_on='2026-11-11')
        self.assertEqual(self.store.list(4,{'from':'2026-11-01','to':'2026-11-10'})['stats']['total'],2)
        self.assertEqual(self.store.list(4,{'from':'2026-11-10','to':'2026-11-10'})['stats']['total'],1)

    def test_35_invalid_filters_rejected(self):
        for f in ({'from':'2026-12-01','to':'2026-11-01'}, {'kind':'wrong'}, {'status':'wrong'},
                  {'as_of':'2026-02-30'},{'q':'x'*201},{'unknown':'x'},{'overdue':'true'},{'q':12}):
            self.reject(400,lambda:self.store.list(4,f))

    def test_36_overdue_is_strictly_before_control_date(self):
        self.create()
        self.assertEqual(self.store.list(4,{'as_of':'2026-11-18'})['stats']['overdue'],0)
        self.assertEqual(self.store.list(4,{'as_of':'2026-11-19'})['stats']['overdue'],1)

    def test_37_completed_and_archived_not_overdue(self):
        d=self.completed()
        self.assertEqual(self.store.list(4,{'as_of':'2027-01-01'})['stats']['overdue'],0)
        self.store.archive(2,d['id'],{'version':3})
        self.assertEqual(self.store.list(4,{'as_of':'2027-01-01'})['stats']['overdue'],0)

    def test_38_combined_filters_and_stats(self):
        self.create(kind='incoming',title='График отдела');self.create(kind='outgoing',title='График отдела')
        self.create(kind='incoming',title='Иная тема')
        result=self.store.list(4,{'q':'ГРАФИК','kind':'incoming','status':'registered',
                                  'as_of':'2026-11-19','overdue':'1'})
        self.assertEqual(result['stats'],{'total':1,'active':1,'overdue':1,'completed':0,'archived':0})

    def test_39_csv_unicode_bom_and_filter(self):
        self.create(title='График; отдела');self.create(kind='outgoing')
        data=self.store.export_csv(4,{'kind':'incoming'})
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        rows=list(csv.reader(io.StringIO(data.decode('utf-8-sig')),delimiter=';'))
        self.assertEqual(len(rows),2);self.assertEqual(rows[1][2],'График; отдела')
        self.assertEqual(len(rows[0]),9)

    def test_40_csv_formula_injection_sanitized(self):
        for value in ('=1+1','+SUM(A1)','-1+1','@SUM(A1)','  =1','\t=1','\r=1','\n=1'):
            self.assertTrue(self.store.csv_safe(value).startswith("'"))
        self.assertEqual(self.store.csv_safe('Обычный текст'),'Обычный текст')
        self.create(title='=HYPERLINK("https://invalid.example")')
        rows=list(csv.reader(io.StringIO(self.store.export_csv(4).decode('utf-8-sig')),delimiter=';'))
        self.assertTrue(rows[1][2].startswith("'="))

    def test_41_database_reopens_and_remains_consistent(self):
        d=self.completed();new=Store(self.store.path)
        self.assertEqual(new.get(4,d['id'])['document']['status'],'completed')
        with new.connection() as con:
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_42_demo_seed_is_idempotent_and_expected(self):
        self.assertTrue(self.store.seed_demo());before=self.snapshot()
        self.assertFalse(self.store.seed_demo());self.assertEqual(before,self.snapshot())
        self.assertEqual(self.store.list(4,{'as_of':'2026-11-18'})['stats'],
                         {'total':6,'active':4,'overdue':2,'completed':1,'archived':1})

    def test_43_event_failure_rolls_back_document_and_counter(self):
        with self.store.connection(True) as con:
            con.execute("CREATE TRIGGER test_block_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'blocked'); END")
        before=self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):self.create()
        self.assertEqual(before,self.snapshot())

    def test_44_concurrent_registration_has_unique_contiguous_numbers(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            docs=list(pool.map(lambda n:self.create(title=f'Документ {n}'),range(24)))
        self.assertEqual(len({d['number'] for d in docs}),24)
        self.assertEqual(sorted(int(d['number'].rsplit('-',1)[1]) for d in docs),list(range(1,25)))
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0],24)

    def test_45_two_editors_only_one_wins(self):
        d=self.create()
        def edit(n):
            try:self.store.edit(1,d['id'],{'version':1,'title':f'Редактор {n}'});return 200
            except DomainError as e:return e.status
        with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(edit,range(2)))
        self.assertEqual(sorted(codes),[200,409])
        self.assertEqual(self.store.get(4,d['id'])['document']['version'],2)
        self.assertEqual(len(self.store.get(4,d['id'])['events']),2)

    def test_46_filter_container_must_be_object(self):
        for value in ([], '', 0, False):
            self.reject(400,lambda:self.store.list(4,value))

if __name__=='__main__':unittest.main()
