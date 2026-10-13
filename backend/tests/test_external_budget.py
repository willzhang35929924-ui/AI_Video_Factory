import json
import tempfile
import unittest
from pathlib import Path

from app.integrations.external_budget import ExternalBudgetExceeded, reserve


class ExternalAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "external_budget"
        self.directory.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, identifier, limit, policy=None):
        value = {"id": identifier, "status": "APPROVED", "limits": {"vision": limit}}
        if policy:
            value["limit_policy"] = policy
        (self.directory / "active.json").write_text(json.dumps(value))

    def test_explicit_unlimited_approval_has_no_internal_count_gate(self):
        self.approve("new-unlimited", None, "UNLIMITED_USER_AUTHORIZED")
        for index in range(8):
            reserve(self.root, "vision", f"unit-only-{index}")
        saved = json.loads((self.directory / "new-unlimited.json").read_text())
        self.assertEqual(len(saved["requests"]), 8)
        self.assertEqual({row["status"] for row in saved["requests"]}, {"RESERVED"})

    def test_unknown_request_cannot_be_repeated_after_changing_authorization(self):
        self.approve("old-limited", 1)
        reserve(self.root, "vision", "same-paid-request")
        self.approve("new-unlimited", None, "UNLIMITED_USER_AUTHORIZED")
        with self.assertRaisesRegex(ExternalBudgetExceeded, "UNKNOWN_EXTERNAL_REQUEST"):
            reserve(self.root, "vision", "same-paid-request")
        self.assertFalse((self.directory / "new-unlimited.json").exists())
        self.assertEqual(len(json.loads((self.directory / "old-limited.json").read_text())["requests"]), 1)

    def test_bounded_approval_and_unauthorized_null_still_fail_closed(self):
        self.approve("old-limited", 1)
        reserve(self.root, "vision", "first")
        with self.assertRaisesRegex(ExternalBudgetExceeded, "EXTERNAL_BUDGET_EXHAUSTED"):
            reserve(self.root, "vision", "second")
        self.approve("invalid-unlimited", None)
        with self.assertRaisesRegex(ExternalBudgetExceeded, "EXTERNAL_AUTHORIZATION_REQUIRED"):
            reserve(self.root, "vision", "second")
        with self.assertRaisesRegex(ExternalBudgetExceeded, "EXTERNAL_AUTHORIZATION_REQUIRED"):
            reserve(self.root, "background", "second")
