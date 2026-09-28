import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, CaseReassigned, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {
    'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100,
    'deadline_days': 30, 'response_day': 110, 'representation_active': True,
    'required_documents': ['passport', 'sponsor_letter'],
}
SUBMIT = {'documents': ['passport', 'sponsor_letter']}
EVIDENCE = {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补充收入证明'}


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.intake = Actor("creator", "intake_officer")
        self.admin = Actor("boss", "admin")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self):
        return self.service.create(self.intake, "IMM-29001", CREATE_DATA)

    def test_creator_becomes_owner(self):
        record = self._create()
        self.assertEqual(record["owner_id"], "creator")

    def test_admin_can_reassign_and_timeline_records_it(self):
        record = self._create()
        record = self.service.reassign(self.admin, record["id"], record["version"], "alice")
        self.assertEqual(record["owner_id"], "alice")
        self.assertEqual(record["version"], 2)
        timeline = self.service.timeline(self.admin, record["id"])
        event = next(e for e in timeline if e["action"] == "reassign")
        self.assertEqual(event["details"]["from_owner"], "creator")
        self.assertEqual(event["details"]["to_owner"], "alice")
        self.assertEqual(event["details"]["operator"], "boss")

    def test_supervisor_can_reassign(self):
        record = self._create()
        supervisor = Actor("sup", "supervisor")
        record = self.service.reassign(supervisor, record["id"], record["version"], "alice")
        self.assertEqual(record["owner_id"], "alice")

    def test_officer_cannot_reassign(self):
        record = self._create()
        with self.assertRaises(PermissionDenied):
            self.service.reassign(Actor("alice", "case_officer"), record["id"], record["version"], "bob")

    def test_reassign_requires_current_version(self):
        record = self._create()
        with self.assertRaises(Conflict):
            self.service.reassign(self.admin, record["id"], record["version"] - 1, "alice")

    def test_reassign_to_same_owner_rejected(self):
        record = self._create()
        with self.assertRaises(ValidationError):
            self.service.reassign(self.admin, record["id"], record["version"], "creator")

    def test_reassign_closed_case_rejected(self):
        record = self._create()
        alice = Actor("alice", "case_officer")
        record = self.service.reassign(self.admin, record["id"], record["version"], "alice")
        record = self.service.act(alice, record["id"], record["version"], "submit", SUBMIT)
        record = self.service.act(alice, record["id"], record["version"], "decide",
                                  {"decision": "granted", "decision_reason": "材料充分"})
        record = self.service.act(Actor("sup", "supervisor"), record["id"], record["version"], "close",
                                  {"closure_note": "归档"})
        with self.assertRaises(ValidationError):
            self.service.reassign(self.admin, record["id"], record["version"], "bob")

    def test_old_owner_blocked_after_reassign(self):
        record = self._create()
        alice = Actor("alice", "case_officer")
        record = self.service.reassign(self.admin, record["id"], record["version"], "alice")
        record = self.service.act(alice, record["id"], record["version"], "submit", SUBMIT)
        record = self.service.reassign(self.admin, record["id"], record["version"], "bob")
        # 旧负责人 alice 继续发补件要求，即使版本正确也必须被拒绝并提示案件已改派。
        with self.assertRaises(CaseReassigned) as ctx:
            self.service.act(alice, record["id"], record["version"], "request_evidence", EVIDENCE)
        self.assertIn("案件已改派", str(ctx.exception))
        # 新负责人 bob 可以继续处理。
        bob = Actor("bob", "case_officer")
        record = self.service.act(bob, record["id"], record["version"], "request_evidence", EVIDENCE)
        self.assertEqual(record["state"], "evidence_requested")

    def test_old_owner_submit_decide_close_blocked(self):
        record = self._create()
        alice = Actor("alice", "case_officer")
        record = self.service.reassign(self.admin, record["id"], record["version"], "alice")
        record = self.service.reassign(self.admin, record["id"], record["version"], "bob")
        for action, data in (
            ("submit", SUBMIT),
            ("request_evidence", EVIDENCE),
            ("decide", {"decision": "granted", "decision_reason": "x"}),
        ):
            with self.assertRaises(CaseReassigned):
                self.service.act(alice, record["id"], record["version"], action, data)


if __name__ == "__main__":
    unittest.main()
