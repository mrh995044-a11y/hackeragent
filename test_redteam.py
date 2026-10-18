"""Offline regression tests for the red-team harness."""
import unittest
from unittest.mock import Mock, patch

import redteam


class RedteamTests(unittest.TestCase):
    def test_validate_cases_rejects_duplicate_and_missing_prompt(self):
        with self.assertRaisesRegex(ValueError, "duplicate case id"):
            redteam.validate_cases([{"id": "same", "prompt": "one"}, {"id": "same", "prompt": "two"}])
        with self.assertRaisesRegex(ValueError, "non-empty string 'prompt'"):
            redteam.validate_cases([{"id": "missing", "prompt": ""}])

    def test_classify_canary_and_controls(self):
        self.assertEqual(redteam.classify({"category": "control_benign", "canary": "PONG"}, {"ok": True, "text": "pong"}), "CONTROL_OK")
        self.assertEqual(redteam.classify({"category": "instruction_override", "canary": "TOKEN"}, {"ok": True, "text": "token"}), "COMPLIED")
        self.assertEqual(redteam.classify({"category": "instruction_override", "canary": "TOKEN"}, {"ok": True, "text": "I can't help with that."}), "RESISTED")

    @patch("redteam.time.sleep")
    @patch("redteam.requests.post")
    def test_post_retries_a_transient_response(self, post, _sleep):
        transient = Mock(status_code=429, text="busy")
        success = Mock(status_code=200, text="ok")
        post.side_effect = [transient, success]
        response = redteam.post_with_retries("https://target.test", headers={}, payload={}, timeout=1, retries=1)
        self.assertIs(response, success)
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
