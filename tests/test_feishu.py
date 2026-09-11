import json
import unittest
from unittest.mock import Mock, patch
from app import feishu


class FeishuContractTests(unittest.TestCase):
    def test_get_user_requests_explicit_id_types(self):
        with patch.object(feishu, "_get", return_value={"data": {"user": {"open_id": "ou_a"}}}) as get:
            self.assertEqual(feishu.get_user("ou_a")["open_id"], "ou_a")
        self.assertEqual(get.call_args.args[1], {"user_id_type": "open_id", "department_id_type": "open_department_id"})

    def test_authorized_direct_users_work_without_root_department_access(self):
        def get(path, params):
            if path.endswith("/scopes"):
                return {"data": {"user_ids": ["ou_one"], "has_more": False}}
            self.assertEqual(path, "/open-apis/contact/v3/users/ou_one")
            return {"data": {"user": {"open_id": "ou_one", "name": "员工"}}}
        with patch.object(feishu, "_get", side_effect=get):
            self.assertEqual(len(feishu.list_users()), 1)

    def test_scope_pagination_and_department_expansion(self):
        def get(path, params):
            if path.endswith("/scopes"):
                if params.get("page_token"):
                    return {"data": {"user_ids": ["ou_direct"], "has_more": False}}
                return {"data": {"department_ids": ["od_parent"], "has_more": True, "page_token": "p2"}}
            if path.endswith("/children"):
                self.assertIn("/od_parent/children", path)
                return {"data": {"items": [{"open_department_id": "od_child"}]}}
            if path.endswith("/find_by_department"):
                oid = "ou_child" if params["department_id"] == "od_child" else "ou_parent"
                return {"data": {"items": [{"open_id": oid}]}}
            return {"data": {"user": {"open_id": "ou_direct"}}}
        with patch.object(feishu, "_get", side_effect=get):
            self.assertEqual({u["open_id"] for u in feishu.list_scope_users()}, {"ou_direct", "ou_child", "ou_parent"})

    def test_repeated_page_token_is_rejected(self):
        with patch.object(feishu, "_get", return_value={"data": {"has_more": True, "page_token": "same"}}):
            with self.assertRaises(feishu.FeishuError):
                feishu.list_scope_users()

    def test_group_members_use_open_id_and_paginate(self):
        def get(path, params):
            if path.endswith('/scopes'):
                return {'data': {'group_ids': ['g1']}}
            if path.endswith('/member/simplelist'):
                self.assertEqual(params['member_id_type'], 'open_id')
                if params['member_type'] == 'department':
                    return {'data': {'memberlist': []}}
                self.assertEqual(params['member_type'], 'user')
                last = bool(params.get('page_token'))
                return {'data': {'memberlist': [{'member_id': 'ou_b' if last else 'ou_a',
                    'member_type': 'user', 'member_id_type': 'open_id'}],
                    'has_more': not last, 'page_token': 'second'}}
            return {'data': {'user': {'open_id': path.rsplit('/', 1)[-1], 'name': '员工'}}}
        with patch.object(feishu, '_get', side_effect=get):
            self.assertEqual({u['open_id'] for u in feishu.list_scope_users()}, {'ou_a', 'ou_b'})

    def test_departments_inside_authorized_group_are_expanded(self):
        def get(path, params):
            if path.endswith('/scopes'):
                return {'data': {'group_ids':['g1']}}
            if path.endswith('/member/simplelist'):
                return {'data': {'memberlist': [] if params['member_type'] == 'user' else [
                    {'member_type':'department', 'member_id_type':'open_id', 'member_id':'od_group'}]}}
            if path.endswith('/children'):
                self.assertIn('/od_group/children', path)
                return {'data': {'items':[{'open_department_id':'od_child'}]}}
            return {'data': {'items':[{'open_id':'ou_'+params['department_id'], 'name':params['department_id']}]}}
        with patch.object(feishu, '_get', side_effect=get):
            self.assertEqual({u['open_id'] for u in feishu.list_scope_users()}, {'ou_od_group','ou_od_child'})

    def test_unreadable_group_never_returns_partial_department_directory(self):
        def get(path, params):
            if path.endswith('/scopes'):
                return {'data': {'department_ids': ['od_a'], 'group_ids': ['g1']}}
            raise feishu.FeishuError(99991672, 'contact:group:readonly')
        with patch.object(feishu, '_get', side_effect=get):
            with self.assertRaisesRegex(feishu.FeishuError, '停止自动对应和推送'):
                feishu.list_scope_users()

    def test_group_members_without_id_type_resolve_users_and_departments(self):
        for type_fields in ({}, {'member_id_type': None}):
            for department_id in ('od-group', 'od_group'):
                def get(path, params):
                    if path.endswith('/scopes'):
                        return {'data': {'group_ids': ['g1']}}
                    if path.endswith('/member/simplelist'):
                        self.assertEqual(params['member_id_type'], 'open_id')
                        kind = params['member_type']
                        member_id = 'ou_direct' if kind == 'user' else department_id
                        return {'data': {'memberlist': [{
                            'member_type': kind, 'member_id': member_id, **type_fields}]}}
                    if path.endswith('/children'):
                        self.assertEqual(params['department_id_type'], 'open_department_id')
                        self.assertIn('/' + department_id + '/children', path)
                        return {'data': {'items': [{'open_department_id': 'od-child'}]}}
                    if path.endswith('/find_by_department'):
                        self.assertEqual(params['user_id_type'], 'open_id')
                        self.assertEqual(params['department_id_type'], 'open_department_id')
                        oid = 'ou_child' if params['department_id'] == 'od-child' else 'ou_parent'
                        return {'data': {'items': [{'open_id': oid, 'name': oid}]}}
                    self.assertEqual(path, '/open-apis/contact/v3/users/ou_direct')
                    return {'data': {'user': {'open_id': 'ou_direct', 'name': 'direct'}}}

                with self.subTest(type_fields=type_fields, department_id=department_id), \
                        patch.object(feishu, '_get', side_effect=get):
                    self.assertEqual({u['open_id'] for u in feishu.list_scope_users()},
                                     {'ou_direct', 'ou_parent', 'ou_child'})

    def test_group_id_inference_does_not_override_invalid_members(self):
        cases = [
            ('user', {'member_type': 'user', 'member_id': 'ou_a', 'member_id_type': 'user_id'}),
            ('department', {'member_type': 'department', 'member_id': 'od-a', 'member_id_type': 'department_id'}),
            ('user', {'member_type': 'user', 'member_id': 'ou_a', 'member_id_type': ''}),
            ('user', {'member_type': 'user', 'member_id': 'od-a'}),
            ('user', {'member_type': 'department', 'member_id': 'ou_a'}),
            ('user', {'member_type': 'user', 'member_id': 'user123'}),
            ('department', {'member_type': 'department', 'member_id': 'department123'}),
            ('user', {'member_type': 'user', 'member_id': ' '}),
            ('user', {'member_type': 'user', 'member_id': 123}),
            ('user', {'member_type': 'user'}),
            ('user', None),
        ]
        for kind, member in cases:
            def get(path, params):
                if path.endswith('/scopes'):
                    return {'data': {'group_ids': ['g1']}}
                self.assertTrue(path.endswith('/member/simplelist'))
                return {'data': {'memberlist': [member] if params['member_type'] == kind else []}}

            with self.subTest(kind=kind, member=member), patch.object(feishu, '_get', side_effect=get):
                with self.assertRaisesRegex(feishu.FeishuError, '停止自动对应和推送'):
                    feishu.list_scope_users()

    def test_conflicting_name_for_one_open_id_is_not_overwritten(self):
        def get(path, params):
            if path.endswith('/scopes'):
                return {'data': {'department_ids': ['od_a', 'od_b']}}
            if path.endswith('/children'):
                return {'data': {}}
            return {'data': {'items': [{'open_id': 'ou_one', 'name': params['department_id']}]}}
        with patch.object(feishu, '_get', side_effect=get):
            with self.assertRaisesRegex(feishu.FeishuError, '冲突的姓名'):
                feishu.list_scope_users()

    def test_missing_or_wrong_group_id_type_is_rejected(self):
        for member in ({'member_id': 'user123', 'member_type': 'user', 'member_id_type': 'user_id'},
                       {'member_type': 'user', 'member_id_type': 'open_id'}):
            with self.subTest(member=member), patch.object(feishu, '_get', side_effect=[
                    {'data': {'group_ids': ['g1']}}, {'data': {'memberlist': [member]}}]):
                with self.assertRaises(feishu.FeishuError):
                    feishu.list_scope_users()

    def test_send_card_is_compact_and_opens_original_image(self):
        with patch.object(feishu, "_post", return_value={"data": {"message_id": "om_1"}}) as post:
            message_id = feishu.send_card("ou_employee", "贺卡", "点击查看", "img_1", uuid="unique")
        self.assertEqual(message_id, "om_1")
        post.assert_called_once()
        payload = post.call_args.args[1]
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(payload["receive_id"], "ou_employee")
        self.assertEqual(payload["uuid"], "unique")
        self.assertEqual(post.call_args.kwargs["params"], {"receive_id_type": "open_id"})
        card = json.loads(payload["content"])
        self.assertFalse(card["config"]["wide_screen_mode"])
        # 根级 img 会撑开整张长海报；div.extra 则是飞书固定尺寸的小图。
        self.assertNotIn("img", [item["tag"] for item in card["elements"]])
        content = next(item for item in card["elements"] if item["tag"] == "div")
        self.assertEqual(content["text"]["content"], "点击查看")
        thumbnail = content["extra"]
        self.assertEqual(thumbnail["tag"], "img")
        self.assertEqual(thumbnail["img_key"], "img_1")
        self.assertTrue(thumbnail["preview"])
        self.assertIn("点击查看", thumbnail["alt"]["content"])

    def test_explicit_rejection_vs_unknown_http_error(self):
        for status, definite in [(400, True), (403, True), (408, False), (500, False)]:
            response = Mock(status_code=status)
            response.json.return_value = {"code": 123, "msg": "error"}
            with self.assertRaises(feishu.FeishuError) as caught:
                feishu._check(response)
            self.assertEqual(caught.exception.definitive, definite)

    def test_automatic_notice_has_no_image_or_confirmation_action(self):
        with patch.object(feishu, "_post", return_value={"data": {"message_id": "om_notice"}}) as post:
            self.assertEqual(feishu.send_notice("ou_employee", "生日贺卡", "notice_uuid"), "om_notice")
        payload = post.call_args.args[1]
        card = json.loads(payload["content"])
        self.assertEqual(payload["receive_id"], "ou_employee")
        self.assertEqual(payload["uuid"], "notice_uuid")
        self.assertFalse(card["config"]["wide_screen_mode"])
        self.assertEqual([element["tag"] for element in card["elements"]], ["div"])
        self.assertNotIn("extra", card["elements"][0])
        self.assertNotIn("点击", payload["content"])
        self.assertNotIn("领取", payload["content"])

    def test_full_card_contains_original_poster(self):
        with patch.object(feishu, "_post", return_value={"data": {"message_id": "om_full"}}) as post:
            self.assertEqual(feishu.send_full_card("ou_employee", "生日贺卡", "img_original", "full_uuid"), "om_full")
        payload = post.call_args.args[1]
        card = json.loads(payload["content"])
        self.assertEqual(payload["uuid"], "full_uuid")
        self.assertEqual(payload["receive_id"], "ou_employee")
        self.assertTrue(card["config"]["wide_screen_mode"])
        self.assertEqual(card["elements"][0]["img_key"], "img_original")
        self.assertEqual(card["elements"][0]["mode"], "fit_horizontal")
        self.assertTrue(card["elements"][0]["preview"])

    def test_compact_greeting_text_banner_and_blank_area_share_one_link(self):
        with patch.object(feishu, '_post', return_value={'data':{'message_id':'om_compact'}}) as post:
            feishu.send_greeting('ou_recipient', '<姓名>', '生日贺卡', 'img_banner',
                                'http://192.168.1.20:8848/greeting/opaque', 'stable_uuid')
        payload=post.call_args.args[1]
        card=json.loads(payload['content'])
        self.assertEqual(payload['receive_id'],'ou_recipient')
        self.assertEqual(payload['uuid'],'stable_uuid')
        self.assertFalse(card['config']['wide_screen_mode'])
        self.assertFalse(card['config']['enable_forward'])
        self.assertEqual(card['card_link']['url'],'http://192.168.1.20:8848/greeting/opaque')
        self.assertEqual(card['elements'][0]['text'],{'tag':'plain_text','content':'<姓名>，你有一张生日贺卡，请查收！'})
        self.assertEqual(card['elements'][1]['img_key'],'img_banner')
        self.assertFalse(card['elements'][1]['preview'])
        self.assertEqual(len(card['elements']),2)

    def test_missing_receipt_is_not_treated_as_success(self):
        for message_id in (None, "", " "):
            with self.subTest(message_id=message_id), patch.object(feishu, "_post", return_value={"data": {"message_id": message_id}}):
                with self.assertRaisesRegex(ValueError, "发送结果待确认"):
                    feishu.send_notice("ou_employee", "生日贺卡", "notice_uuid")


if __name__ == "__main__":
    unittest.main()
