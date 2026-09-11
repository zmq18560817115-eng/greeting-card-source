"""Offline integration: automatic scheduling, one notification, and private poster links."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auto_schedule, db, delivery_config, employees, feishu, main, pipeline, poster_links, push, scheduler


class CompactDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target, key, value in ((db,'DB_PATH',self.root/'db.sqlite'),(pipeline,'CARD_DIR',self.root/'cards'),
                                  (main,'ADMIN_TOKEN','admin-test-only'),(push,'DRY_RUN',False),
                                  (push,'DELIVERY_MODE','compact_link'),(feishu,'FEISHU_APP_ID','cli_compact_test')):
            self.patch_object(target,key,value)
        self.mock('requests.sessions.Session.request', side_effect=AssertionError('No external network'))
        self.mock('app.db.now', return_value='2026-09-11 12:00:00')
        self.mock('app.push.now', return_value='2026-09-11 12:00:00')
        cfg={'canvas':{'width':200,'height':300},'vars':{},'templates':{
            key:{'ai':{'enabled':False},'layers':[]} for key in ('birthday','anniversary')}}
        self.mock('app.compose.load_config', return_value=cfg)
        self.upload=self.mock('app.feishu.upload_image', return_value='img_banner_test')
        self.send=self.mock('app.feishu.send_greeting', return_value='om_compact_test')
        self.notice=self.mock('app.feishu.send_notice', side_effect=AssertionError('No separate notice'))
        self.full=self.mock('app.feishu.send_full_card', side_effect=AssertionError('No automatic long poster'))
        db.init_db()
        delivery_config.save({'base_url':'http://192.168.1.20:8848','auto_schedule':True})
        self.emp=employees.save_employee({'name':'测试甲','department':'测试部门','birth_date':'09-11',
                                         'join_date':'2020-01-01','feishu_open_id':'ou_test_a'})['employee']
        self.future=employees.save_employee({'name':'测试乙','department':'测试部门','birth_date':'09-15',
                                            'feishu_open_id':'ou_test_b'})['employee']
        remote=[{'name':p['name'],'open_id':p['feishu_open_id'],'status':{'is_resigned':False}}
                for p in (self.emp,self.future)]
        self.mock('app.feishu.list_scope_users', return_value=remote)
        self.get_user=self.mock('app.feishu.get_user', side_effect=lambda oid:next(u for u in remote if u['open_id']==oid))
        self.client=TestClient(main.app)
        self.addCleanup(self.client.close)
        self.headers={'X-Admin-Token':'admin-test-only'}

    def mock(self, name, **kwargs):
        p=patch(name,**kwargs);result=p.start();self.addCleanup(p.stop);return result

    def patch_object(self,target,key,value):
        p=patch.object(target,key,value);p.start();self.addCleanup(p.stop)

    def event(self):
        return db.query_one('SELECT * FROM events WHERE employee_id=?',(self.emp['id'],))

    def token(self):
        return self.send.call_args.args[4].rsplit('/',1)[1]

    def test_dates_send_without_review_and_never_send_future_or_repeat(self):
        scheduler.push_job()
        first=self.event()
        self.assertEqual(first['status'],'pushed')
        self.assertEqual(first['confirmed_by'],'system')
        self.assertIsNone(first['reviewed_at'])
        self.assertEqual(first['delivery_mode'],'compact_link')
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[:3],('ou_test_a','测试甲','生日贺卡'))
        self.assertEqual(Path(self.upload.call_args.args[0]).name,'birthday_banner.png')
        self.assertEqual(db.query_one('SELECT status FROM events WHERE employee_id=?',(self.future['id'],))['status'],'confirmed')
        scheduler.push_job()
        self.send.assert_called_once()
        self.notice.assert_not_called();self.full.assert_not_called()

    def test_skipped_employee_is_never_auto_reinstated(self):
        auto_schedule.prepare()
        self.client.post(f"/api/events/{self.event()['id']}/skip",headers=self.headers)
        scheduler.push_job()
        self.assertEqual(self.event()['status'],'skipped')
        self.send.assert_not_called()

    def test_changed_employee_poster_regenerates_before_automatic_delivery(self):
        auto_schedule.prepare()
        old=self.event()['selected_card_id']
        employees.save_employee({'id':self.emp['id'],'department':'更正后的部门'})
        self.assertEqual(self.event()['status'],'needs_regeneration')
        scheduler.push_job()
        new=self.event()
        self.assertEqual(new['status'],'pushed')
        self.assertNotEqual(new['selected_card_id'],old)
        self.assertIn('更正后的部门',new['employee_snapshot'])

    def test_wrong_recipient_blocks_automatic_schedule(self):
        self.get_user.side_effect=lambda oid:{'name':'不匹配','open_id':oid,'status':{'is_resigned':False}}
        scheduler.push_job()
        self.assertEqual(self.event()['status'],'blocked')
        self.send.assert_not_called()

    def test_identity_change_during_banner_upload_prevents_send(self):
        auto_schedule.prepare()
        def upload(_):
            self.get_user.side_effect=lambda oid:{'name':'不匹配','open_id':oid,'status':{'is_resigned':False}}
            return 'img_banner'
        self.upload.side_effect=upload
        push.push_event(self.event()['id'])
        self.assertEqual(self.event()['status'],'blocked')
        self.send.assert_not_called()

    def test_timeout_is_unknown_and_never_automatically_retried(self):
        self.send.side_effect=TimeoutError('simulated timeout')
        scheduler.push_job()
        self.assertEqual(self.event()['status'],'delivery_unknown')
        scheduler.push_job()
        self.send.assert_called_once()
        self.assertEqual(self.client.get('/greeting/'+self.token()).status_code,200)

    def test_definite_rejection_reuses_same_uuid_and_link(self):
        self.send.side_effect=[feishu.FeishuError(400,'rejected',definitive=True),'om_second']
        scheduler.push_job();scheduler.push_job()
        self.assertEqual(self.event()['status'],'pushed')
        self.assertEqual(self.send.call_count,2)
        a,b=self.send.call_args_list
        self.assertEqual(a.args[4],b.args[4]);self.assertEqual(a.kwargs['uuid'],b.kwargs['uuid'])
        self.assertEqual(len(db.query('SELECT * FROM poster_links')),1)

    def test_link_opens_only_one_poster_without_admin_access(self):
        scheduler.push_job()
        url='/greeting/'+self.token()
        page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.headers['referrer-policy'],'no-referrer')
        self.assertIn('no-store',page.headers['cache-control'])
        self.assertIn('noindex',page.headers['x-robots-tag'])
        self.assertNotIn('admin-test-only',page.text)
        image=self.client.get(url+'/poster')
        self.assertEqual(image.status_code,200)
        self.assertTrue(image.content.startswith(b'\x89PNG'))
        for path in ('/api/employees','/files/cards/example.png','/api/delivery/config'):
            self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.client.get('/greeting/'+'a'*64+'/poster').status_code,410)
        self.assertEqual(self.client.get('/greeting/'+self.token()[:-1]+'x').status_code,410)

    def test_expiry_and_recipient_changes_revoke_access(self):
        scheduler.push_job();url='/greeting/'+self.token()
        expiry=db.query_one('SELECT expires FROM poster_links')['expires']
        with patch('app.poster_links.time.time',return_value=expiry):
            self.assertEqual(self.client.get(url).status_code,410)
        with patch.object(feishu,'FEISHU_APP_ID','cli_other'):
            self.assertEqual(self.client.get(url).status_code,410)
        db.execute('UPDATE employees SET active=0 WHERE id=?',(self.emp['id'],))
        self.assertEqual(self.client.get(url+'/poster').status_code,410)

    def test_missing_link_configuration_fails_before_message_request(self):
        db.execute("UPDATE kv SET value=? WHERE key='delivery_config'",(json.dumps({'auto_schedule':True,'base_url':''}),))
        scheduler.push_job()
        self.assertEqual(self.event()['status'],'failed')
        self.send.assert_not_called();self.upload.assert_not_called()

    def test_pause_prevents_generation_and_delivery_and_dry_run_never_sends(self):
        delivery_config.save({'base_url':'http://192.168.1.20:8848','auto_schedule':False})
        scheduler.push_job()
        with patch.object(pipeline,'run_weekly') as weekly:
            scheduler.weekly_job()
            weekly.assert_not_called()
        self.assertEqual(db.query('SELECT * FROM events'),[])
        delivery_config.save({'base_url':'http://192.168.1.20:8848','auto_schedule':True})
        with patch.object(push,'DRY_RUN',True): scheduler.push_job()
        self.assertEqual(self.event()['status'],'simulated')
        self.send.assert_not_called();self.upload.assert_not_called()

    def test_review_is_optional_record_and_requires_admin(self):
        auto_schedule.prepare();eid=self.event()['id']
        self.assertEqual(self.client.post(f'/api/events/{eid}/review').status_code,401)
        response=self.client.post(f'/api/events/{eid}/review',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertIsNotNone(self.event()['reviewed_at'])
        self.assertEqual(self.event()['status'],'confirmed')
        scheduler.push_job();self.send.assert_called_once()

    def test_old_unreviewed_date_expires_without_sending(self):
        auto_schedule.prepare()
        db.execute("UPDATE events SET status='ready',event_date='2025-09-11',trigger_at='2025-09-11 11:00:00' WHERE id=?",(self.event()['id'],))
        self.assertEqual(push.due_events(),[])
        self.assertEqual(self.event()['status'],'expired')
        self.send.assert_not_called()

    def test_intranet_config_validation_and_explicit_login(self):
        for url in ('http://127.0.0.1:8848','http://0.0.0.0:8848','http://localhost:8848','https://8.8.8.8',
                    'http://192.168.1.20:8848/path','http://user:password@192.168.1.20','javascript:alert(1)'):
            response=self.client.post('/api/delivery/config',headers=self.headers,json={'base_url':url,'auto_schedule':True})
            self.assertEqual(response.status_code,400,url)
        self.assertEqual(self.client.post('/api/auth/check').status_code,401)
        self.assertEqual(self.client.post('/api/auth/check',headers=self.headers).status_code,200)


class IntranetSetupTests(unittest.TestCase):
    def test_configuration_preserves_data_credentials_and_real_mode(self):
        from scripts.configure_intranet import configure
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            original='FEISHU_APP_SECRET=existing-test-secret\nDRY_RUN=false\nADMIN_TOKEN=existing-admin\nHOST=127.0.0.1\n'
            (root/'.env').write_text(original,encoding='utf-8')
            self.assertEqual(configure('192.168.1.20',8848,root),'http://192.168.1.20:8848')
            after=(root/'.env').read_text(encoding='utf-8')
            for line in ('FEISHU_APP_SECRET=existing-test-secret','DRY_RUN=false','ADMIN_TOKEN=existing-admin','HOST=0.0.0.0'):
                self.assertIn(line,after)
            self.assertEqual(next(root.glob('.env.before-lan.*')).read_text(encoding='utf-8'),original)
            with self.assertRaises(ValueError):configure('0.0.0.0',8848,root)


if __name__=='__main__': unittest.main()
