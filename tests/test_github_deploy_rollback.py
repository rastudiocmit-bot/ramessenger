"""Isolated rollback tests. Never call Docker or the live VPS."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import github_promote_vps as deploy

OLD = "center-messenger:github-old123456"
NEW = "center-messenger:github-new123456"

class RollbackTests(unittest.TestCase):
    def assert_compose_only_api(self, calls):
        self.assertTrue(calls)
        for argv, env in calls:
            self.assertEqual(argv[-1], "api")
            self.assertIn("--no-deps", argv)
            self.assertIn("--no-build", argv)
            self.assertEqual(argv[:4], ("docker", "compose", "-p", "center-messenger"))

    def test_success_only_new_api(self):
        calls = []
        def fake_cmd(*argv, **kwargs):
            calls.append((argv, kwargs["env"]["CM_IMAGE"]))
        with patch.object(deploy, "cmd", side_effect=fake_cmd), patch.object(deploy, "wait_ready", return_value=True) as ready:
            deploy.deploy_api_with_rollback(OLD, NEW, "/backup.dump")
        self.assertEqual([v for _, v in calls], [NEW])
        self.assert_compose_only_api([(a,None) for a,_ in calls])
        ready.assert_called_once_with(NEW)

    def test_failed_new_readiness_rolls_back(self):
        calls = []
        def fake_cmd(*argv, **kwargs):
            calls.append((argv, kwargs["env"]["CM_IMAGE"]))
        with patch.object(deploy, "cmd", side_effect=fake_cmd), patch.object(deploy, "wait_ready", side_effect=[False, True]) as ready:
            with self.assertRaisesRegex(ValueError, "failed readiness"):
                deploy.deploy_api_with_rollback(OLD, NEW, "/backup.dump")
        self.assertEqual([v for _, v in calls], [NEW, OLD])
        self.assert_compose_only_api([(a,None) for a,_ in calls])
        self.assertEqual(ready.call_count, 2)

    def test_recreation_error_still_rolls_back(self):
        calls = []
        def fake_cmd(*argv, **kwargs):
            image = kwargs["env"]["CM_IMAGE"]
            calls.append((argv, image))
            if image == NEW:
                raise RuntimeError("Docker create failed")
        with patch.object(deploy, "cmd", side_effect=fake_cmd), patch.object(deploy, "wait_ready", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Docker create failed"):
                deploy.deploy_api_with_rollback(OLD, NEW, "/backup.dump")
        self.assertEqual([v for _, v in calls], [NEW, OLD])
        self.assert_compose_only_api([(a,None) for a,_ in calls])

    def test_failed_rollback_is_not_reported_as_success(self):
        calls = []
        def fake_cmd(*argv, **kwargs):
            calls.append((argv, kwargs["env"]["CM_IMAGE"]))
        with patch.object(deploy, "cmd", side_effect=fake_cmd), patch.object(deploy, "wait_ready", side_effect=[False, False]):
            with self.assertRaisesRegex(RuntimeError, "rollback did not recover"):
                deploy.deploy_api_with_rollback(OLD, NEW, "/backup.dump")
        self.assertEqual([v for _, v in calls], [NEW, OLD])
        self.assert_compose_only_api([(a,None) for a,_ in calls])

    def test_rollback_command_error_is_reported(self):
        calls = []
        def fake_cmd(*argv, **kwargs):
            image = kwargs["env"]["CM_IMAGE"]
            calls.append((argv, image))
            if image == OLD:
                raise RuntimeError("rollback Docker error")
        with patch.object(deploy, "cmd", side_effect=fake_cmd), patch.object(deploy, "wait_ready", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "rollback did not recover"):
                deploy.deploy_api_with_rollback(OLD, NEW, "/backup.dump")
        self.assertEqual([v for _, v in calls], [NEW, OLD])

if __name__ == "__main__":
    unittest.main()
