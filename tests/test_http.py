import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from delo.model import Store
from delo.server import make_server

BASE=dict(kind='incoming',title='Учебный документ',correspondent='Учебный отдел',
          registered_on='2026-11-18',due_on='2026-11-25')

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'http.sqlite3');self.store.initialize(demo=True)
        self.server=make_server(self.store,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.02},daemon=True)
        self.thread.start();self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3);self.tmp.cleanup()

    def call(self,path,method='GET',data=None,actor='1',raw=None,headers=None):
        h={} if actor is None else {'X-Demo-User':str(actor)}
        body=raw
        if data is not None:
            body=json.dumps(data,ensure_ascii=False).encode();h['Content-Type']='application/json'
        h.update(headers or {})
        req=urllib.request.Request(self.base+path,method=method,data=body,headers=h)
        try:r=urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as error:r=error
        with r:
            b=r.read();return r.status,dict(r.headers),b

    def json(self,*args,**kwargs):
        code,headers,body=self.call(*args,**kwargs)
        return code,json.loads(body)

    def create(self):
        code,body=self.json('/api/documents',method='POST',data=BASE)
        self.assertEqual(code,201)
        return body['document']

    def test_01_static_assets_and_embedding(self):
        for path,mime in [('/','text/html'),('/app.js','text/javascript'),('/style.css','text/css')]:
            code,h,b=self.call(path,actor=None)
            self.assertEqual(code,200);self.assertIn(mime,h['Content-Type']);self.assertTrue(b)
            self.assertNotIn('X-Frame-Options',h)
            self.assertIn('nosniff',h['X-Content-Type-Options'])
        self.assertIn('не является входом',self.call('/')[2].decode())

    def test_02_health_and_explicit_demo_metadata(self):
        code,b=self.json('/api/health',actor=None)
        self.assertEqual((code,b['mode']),(200,'synthetic-demo'))
        code,b=self.json('/api/meta',actor=None)
        self.assertTrue(b['demo']);self.assertEqual(len(b['users']),5)

    def test_03_no_header_is_forbidden_for_documents(self):
        code,b=self.json('/api/documents',actor=None)
        self.assertEqual(code,403);self.assertIn('error',b)

    def test_04_invalid_and_unknown_user_headers(self):
        for actor in ('true','0','-1','999','1.0','999999999999999'):
            self.assertEqual(self.json('/api/documents',actor=actor)[0],403)

    def test_05_create_then_read(self):
        d=self.create();code,b=self.json('/api/documents/'+str(d['id']),actor='4')
        self.assertEqual(code,200);self.assertEqual(b['document']['title'],BASE['title'])
        self.assertEqual(len(b['events']),1)

    def test_06_wrong_role_cannot_create(self):
        for actor in ('2','3','4','5'):
            self.assertEqual(self.json('/api/documents',method='POST',data=BASE,actor=actor)[0],403)
        self.assertEqual(self.store.list(4)['stats']['total'],0)

    def test_07_content_type_required(self):
        code,b=self.json('/api/documents',method='POST',raw=b'{}',headers={'Content-Type':'text/plain'})
        self.assertEqual(code,415)

    def test_08_malformed_json_and_utf8(self):
        for raw in (b'{bad}',b'{"title":"\xff"}'):
            self.assertEqual(self.json('/api/documents',method='POST',raw=raw,
                             headers={'Content-Type':'application/json'})[0],400)

    def test_09_duplicate_json_keys_rejected(self):
        code,b=self.json('/api/documents',method='POST',raw=b'{"kind":"incoming","kind":"internal"}',
                         headers={'Content-Type':'application/json'})
        self.assertEqual(code,400);self.assertIn('Повторяющееся',b['error'])

    def test_10_nonobject_and_nonfinite_json_rejected(self):
        for raw in (b'[]',b'null',b'{"version":NaN}',b'{"version":Infinity}'):
            self.assertEqual(self.json('/api/documents',method='POST',raw=raw,
                             headers={'Content-Type':'application/json'})[0],400)

    def test_11_oversized_body_rejected(self):
        self.assertEqual(self.json('/api/documents',method='POST',raw=b'x'*65537,
                         headers={'Content-Type':'application/json'})[0],413)
        self.assertEqual(self.store.list(4)['stats']['total'],0)

    def test_12_source_database_and_git_are_not_served(self):
        for path in ('/.git/config','/data/demo.sqlite3','/delo/model.py','/../README.md','/not-found'):
            code,b=self.json(path)
            self.assertEqual(code,404);self.assertIn('error',b)

    def test_13_cross_origin_write_forbidden(self):
        code,b=self.json('/api/documents',method='POST',data=BASE,headers={'Origin':'https://untrusted.invalid'})
        self.assertEqual(code,403);self.assertEqual(self.store.list(4)['stats']['total'],0)

    def test_14_preview_host_same_origin_allowed(self):
        code,b=self.json('/api/documents',method='POST',data=BASE,
            headers={'Host':'demo.e2b.app','Origin':'https://demo.e2b.app'})
        self.assertEqual(code,201)

    def test_15_optimistic_patch_conflict_is_409(self):
        d=self.create();p='/api/documents/'+str(d['id'])
        self.assertEqual(self.json(p,method='PATCH',data={'version':1,'title':'Первое изменение'})[0],200)
        code,b=self.json(p,method='PATCH',data={'version':1,'title':'Затёртое изменение'})
        self.assertEqual(code,409);self.assertIn('уже изменена',b['error'])
        self.assertEqual(self.store.get(4,d['id'])['document']['title'],'Первое изменение')

    def test_16_complete_lifecycle_via_http(self):
        d=self.create();p='/api/documents/'+str(d['id'])
        self.assertEqual(self.json(p+'/assign',method='POST',actor='2',data={'version':1,'assignee_id':3})[0],200)
        self.assertEqual(self.json(p+'/complete',method='POST',actor='3',data={'version':2,'result':'Выполнено'})[0],200)
        self.assertEqual(self.json(p+'/archive',method='POST',actor='2',data={'version':3})[0],200)
        code,b=self.json(p,actor='4');self.assertEqual(code,200)
        self.assertEqual(b['document']['status'],'archived');self.assertEqual(len(b['events']),4)

    def test_17_filtered_csv_download(self):
        self.create();self.store.create(1,{**BASE,'kind':'outgoing'})
        code,h,b=self.call('/api/export.csv?kind=incoming',actor='4')
        self.assertEqual(code,200);self.assertIn('attachment',h['Content-Disposition'])
        self.assertTrue(b.startswith(b'\xef\xbb\xbf'));self.assertIn('ВХ-2026',b.decode('utf-8-sig'))
        self.assertNotIn('ИСХ-2026',b.decode('utf-8-sig'))

    def test_18_duplicate_filter_and_bad_period(self):
        for path in ('/api/documents?kind=incoming&kind=outgoing',
                     '/api/documents?from=2026-12-01&to=2026-11-01','/api/documents?unknown=1'):
            self.assertEqual(self.json(path)[0],400)

    def test_19_document_and_action_not_found(self):
        self.assertEqual(self.json('/api/documents/999',actor='4')[0],404)
        self.assertEqual(self.json('/api/documents/999/destroy',method='POST',data={})[0],404)

    def test_20_head_has_no_body(self):
        code,h,b=self.call('/',method='HEAD',actor=None)
        self.assertEqual(code,200);self.assertEqual(b,b'');self.assertGreater(int(h['Content-Length']),100)

if __name__=='__main__':unittest.main()
