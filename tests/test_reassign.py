import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'applicant_id': 'A-700', 'case_type': 'work', 'received_day': 0, 'deadline_days': 30, 'response_day': 0, 'representation_active': True, 'required_documents': ['passport', 'contract']}

MARY = Actor("mary", "intake_officer")
BOSS = Actor("boss", "supervisor")
OFFICER_B = Actor("officer-b", "case_officer")
OFFICER_C = Actor("officer-c", "case_officer")
LEGAL = Actor("rep", "legal_rep")
ADMIN = Actor("root", "admin")


class ReassignTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference="IMM-70001", actor=MARY):
        return self.service.create(actor, reference, CREATE_DATA)

    def test_creator_becomes_owner(self):
        record = self._create()
        self.assertEqual(record["owner_id"], "mary")

    def test_reassign_changes_owner_and_bumps_version(self):
        record = self._create()
        reassigned = self.service.reassign(BOSS, record["id"], record["version"], "officer-b", "同事休假")
        self.assertEqual(reassigned["owner_id"], "officer-b")
        self.assertEqual(reassigned["version"], record["version"] + 1)

        timeline = self.service.timeline(MARY, record["id"])
        event = timeline[-1]
        self.assertEqual(event["action"], "reassigned")
        self.assertEqual(event["version"], 2)
        self.assertEqual(event["actor_id"], "boss")
        self.assertEqual(event["details"]["previous_owner_id"], "mary")
        self.assertEqual(event["details"]["new_owner_id"], "officer-b")
        self.assertEqual(event["details"]["operator_id"], "boss")

    def test_former_owner_blocked_on_gated_actions_new_owner_can_continue(self):
        record = self._create()
        record = self.service.reassign(BOSS, record["id"], record["version"], "officer-b")

        # 旧负责人提交材料被拒绝
        with self.assertRaises(PermissionDenied) as caught:
            self.service.act(MARY, record["id"], record["version"], "submit", {"documents": ["passport", "contract"]})
        self.assertIn("案件已改派", str(caught.exception))

        # 新负责人继续处理
        record = self.service.act(OFFICER_B, record["id"], record["version"], "submit", {"documents": ["passport", "contract"]})
        self.assertEqual(record["state"], "submitted")

        # 旧负责人发补件要求被拒绝
        with self.assertRaises(PermissionDenied):
            self.service.act(MARY, record["id"], record["version"], "request_evidence", {"allowed_days": 10, "evidence_request": "补充税单"})
        record = self.service.act(OFFICER_B, record["id"], record["version"], "request_evidence", {"evidence_request_day": 5, "allowed_days": 10, "evidence_request": "补充税单"})
        self.assertEqual(record["state"], "evidence_requested")

        record = self.service.act(LEGAL, record["id"], record["version"], "respond", {"response_day": 10, "documents": ["tax"]})
        self.assertEqual(record["state"], "response_received")

        # 旧负责人作决定被拒绝（即便状态不允许该动作，也优先提示改派）
        with self.assertRaises(PermissionDenied):
            self.service.act(MARY, record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "ok"})
        record = self.service.act(OFFICER_B, record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "材料充分"})
        self.assertEqual(record["state"], "decided")

        # 旧负责人归档被拒绝（主管角色检查也应让位于改派提示）
        with self.assertRaises(PermissionDenied):
            self.service.act(MARY, record["id"], record["version"], "close", {"closure_note": "结案"})
        record = self.service.act(BOSS, record["id"], record["version"], "close", {"closure_note": "结案"})
        self.assertEqual(record["state"], "closed")

    def test_blocking_follows_identity_not_role(self):
        # 主管建案后成为负责人，被改派后再尝试发补件要求同样被拒绝
        record = self.service.create(ADMIN, "IMM-70002", CREATE_DATA)
        record = self.service.reassign(BOSS, record["id"], record["version"], "officer-b")
        with self.assertRaises(PermissionDenied) as caught:
            self.service.act(ADMIN, record["id"], record["version"], "request_evidence", {"allowed_days": 5, "evidence_request": "x"})
        self.assertIn("案件已改派", str(caught.exception))

    def test_second_reassign_blocks_first_new_owner(self):
        record = self._create("IMM-70003")
        record = self.service.reassign(BOSS, record["id"], record["version"], "officer-b")
        record = self.service.reassign(BOSS, record["id"], record["version"], "officer-c")
        with self.assertRaises(PermissionDenied):
            self.service.act(MARY, record["id"], record["version"], "submit", {"documents": ["passport", "contract"]})
        with self.assertRaises(PermissionDenied):
            self.service.act(OFFICER_B, record["id"], record["version"], "submit", {"documents": ["passport", "contract"]})
        record = self.service.act(OFFICER_C, record["id"], record["version"], "submit", {"documents": ["passport", "contract"]})
        self.assertEqual(record["state"], "submitted")

    def test_case_officer_cannot_reassign(self):
        record = self._create()
        with self.assertRaises(PermissionDenied):
            self.service.reassign(OFFICER_B, record["id"], record["version"], "officer-c")

    def test_reassign_requires_current_version(self):
        record = self._create()
        with self.assertRaises(Conflict):
            self.service.reassign(BOSS, record["id"], record["version"] - 1, "officer-b")

    def test_reassign_to_same_owner_rejected(self):
        record = self._create()
        with self.assertRaises(ValidationError):
            self.service.reassign(BOSS, record["id"], record["version"], "mary")

    def test_closed_case_cannot_be_reassigned(self):
        record = self.service.create(ADMIN, "IMM-70004", CREATE_DATA)
        record = self.service.reassign(BOSS, record["id"], record["version"], "officer-b")
        record = self.service.act(OFFICER_B, record["id"], record["version"], "submit", {"documents": ["passport", "contract"]})
        record = self.service.act(OFFICER_B, record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "ok"})
        record = self.service.act(BOSS, record["id"], record["version"], "close", {"closure_note": "结案"})
        with self.assertRaises(Conflict):
            self.service.reassign(BOSS, record["id"], record["version"], "officer-c")


if __name__ == "__main__":
    unittest.main()
