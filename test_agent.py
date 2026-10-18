"""Offline safety checks for agent workspace enforcement."""
import os
import tempfile
import unittest
from types import SimpleNamespace

import agent


class AgentWorkspaceTests(unittest.TestCase):
    def test_workspace_path_blocks_escape(self):
        with tempfile.TemporaryDirectory() as workspace:
            inside = agent.workspace_path(workspace, "nested/file.txt")
            self.assertTrue(inside.startswith(os.path.realpath(workspace)))
            with self.assertRaisesRegex(ValueError, "outside workspace"):
                agent.workspace_path(workspace, "../outside.txt")

    def test_guard_normalizes_file_paths(self):
        with tempfile.TemporaryDirectory() as workspace:
            cfg = SimpleNamespace(workspace=workspace, allow_outside_workspace=False)
            guarded = agent.guard_tool_args(cfg, "read_file", {"path": "README.md"})
            self.assertEqual(guarded["path"], os.path.join(os.path.realpath(workspace), "README.md"))

    def test_remove_env_key_and_list_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_env = os.path.join(tmp_dir, ".env")
            with open(test_env, "w", encoding="utf-8") as f:
                f.write("TEST_PROVIDER_KEY=secret_123\nANOTHER_KEY=val456\n")
            
            # Monkeypatch __file__ location
            orig_file = agent.__file__
            try:
                agent.__file__ = os.path.join(tmp_dir, "agent.py")
                os.environ["TEST_PROVIDER_KEY"] = "secret_123"
                self.assertIn("TEST_PROVIDER_KEY", agent.list_saved_env_keys())
                
                removed = agent.remove_env_key("TEST_PROVIDER_KEY")
                self.assertTrue(removed)
                self.assertNotIn("TEST_PROVIDER_KEY", os.environ)
                self.assertNotIn("TEST_PROVIDER_KEY", agent.list_saved_env_keys())
                self.assertIn("ANOTHER_KEY", agent.list_saved_env_keys())
            finally:
                agent.__file__ = orig_file

    def test_cursor_animation_helpers(self):
        import io
        import sys
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            agent.AnimatedCursor.emit_token("hello")
            agent.AnimatedCursor.finalize_stream()
            self.assertIn("hello", buf.getvalue())
        finally:
            sys.stdout = old_stdout


if __name__ == "__main__":
    unittest.main()
