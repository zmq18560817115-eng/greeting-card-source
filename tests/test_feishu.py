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

    def test_send_card_uses_idempotency_and_clickable_image(self):
        with patch.object(feishu, "_post", return_value={"data": {"message_id": "om_1"}}) as post:
            feishu.send_card("ou_employee", "贺卡", "点击查看", "img_1", uuid="unique")
        payload = post.call_args.args[1]
        self.assertEqual(payload["receive_id"], "ou_employee")
        self.assertEqual(payload["uuid"], "unique")
        self.assertEqual(post.call_args.kwargs["params"], {"receive_id_type": "open_id"})
        card = json.loads(payload["content"])
        self.assertTrue(next(item for item in card["elements"] if item["tag"] == "img")["preview"])

    def test_explicit_rejection_vs_unknown_http_error(self):
        for status, definite in [(400, True), (403, True), (408, False), (500, False)]:
            response = Mock(status_code=status)
            response.json.return_value = {"code": 123, "msg": "error"}
            with self.assertRaises(feishu.FeishuError) as caught:
                feishu._check(response)
            self.assertEqual(caught.exception.definitive, definite)


if __name__ == "__main__":
    unittest.main()
