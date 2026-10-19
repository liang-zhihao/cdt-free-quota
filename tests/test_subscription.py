import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from subscription import Subscription


class FakeOSS(Subscription):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.object = None
        self.public = False
        self.fail_upload = False
        self.location = "oss-" + self.region
        self.block = "false"
        self.bucket_acl = "private"
        self.tagged = True
        self.list_status = 403

    def api(self, action, *arguments):
        self.calls.append((action, arguments))
        if action == "get-bucket-info":
            return {"Bucket": {"Location": self.location, "AccessControlList": {"Grant": self.bucket_acl},
                               "BlockPublicAccess": self.block}}
        if action == "get-bucket-tags":
            return {"TagSet": {"Tag": [{"Key": "cdt-role", "Value": "subscription"}] if self.tagged else []}}
        if action == "put-object":
            if self.fail_upload:
                raise RuntimeError("模拟上传中断")
            import urllib.parse
            path = urllib.parse.urlparse(arguments[arguments.index("--body") + 1]).path
            self.object = Path(urllib.parse.unquote(path)).read_bytes()
            self.public = True
        if action == "put-object-acl":
            self.public = False
        return {}

    def fetch(self, url):
        if url == self.endpoint:
            return self.list_status, b""
        return (200, self.object) if self.public else (403, b"")


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "clash.yaml"
        self.config.write_text(json.dumps({"proxies": [{"server": "203.0.113.1"}],
                                           "proxy-groups": [{"name": "PROXY"}], "rules": ["MATCH,PROXY"]}))
        self.deployment = {"REGION_ID": "ap-southeast-1", "OSS_BUCKET": "guard-example", "EIP": "203.0.113.1"}
        self.state = self.root / "subscription.json"
        self.oss = FakeOSS(self.deployment, "sub-example", "example", self.state)

    def test_publish_update_check_revoke_both_regions(self):
        for region in ("cn-hongkong", "ap-southeast-1"):
            state = self.root / (region + ".json")
            oss = FakeOSS(dict(self.deployment, REGION_ID=region), "sub-example", "example", state)
            oss.publish(self.config)
            first = json.loads(state.read_text())
            self.assertIn(".oss-" + region + ".aliyuncs.com/", first["url"])
            self.assertEqual(first["status"], "published")
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.config.write_text(self.config.read_text() + "\n")
            oss.publish(self.config)
            self.assertEqual(oss.state["url"], first["url"])
            oss.check(self.config)
            oss.revoke()
            self.assertFalse(oss.public)
            self.assertEqual(json.loads(state.read_text())["status"], "revoked")
            oss.publish(self.config)
            self.assertEqual(oss.state["url"], first["url"])
            restored = FakeOSS(dict(self.deployment, REGION_ID=region), "sub-example", "example", state)
            self.assertEqual(restored.state["url"], first["url"])

    def test_dry_has_no_writes_or_state(self):
        self.oss.publish(self.config, dry=True)
        self.assertFalse(self.state.exists())
        self.assertTrue(all(call[0].startswith("get-") for call in self.oss.calls))

    def test_reject_guard_bucket_and_wrong_region_state(self):
        with self.assertRaises(ValueError):
            FakeOSS(self.deployment, "guard-example", "example", self.state)
        self.oss.publish(self.config)
        with self.assertRaises(ValueError):
            FakeOSS(dict(self.deployment, REGION_ID="cn-hongkong"), "sub-example", "example", self.state)

    def test_bucket_preconditions_prevent_upload(self):
        for field, value in (("location", "oss-cn-hongkong"), ("block", "true"),
                             ("bucket_acl", "public-read"), ("tagged", False)):
            oss = FakeOSS(self.deployment, "sub-example", "example", self.state)
            setattr(oss, field, value)
            with self.assertRaises(ValueError):
                oss.publish(self.config)
            self.assertFalse(any(call[0] == "put-object" for call in oss.calls))
            self.assertFalse(self.state.exists())

    def test_interrupted_upload_reuses_saved_url(self):
        self.oss.fail_upload = True
        with self.assertRaises(RuntimeError):
            self.oss.publish(self.config)
        original = json.loads(self.state.read_text())["url"]
        self.assertEqual(json.loads(self.state.read_text())["status"], "pending")
        retry = FakeOSS(self.deployment, "sub-example", "example", self.state)
        retry.publish(self.config)
        self.assertEqual(retry.state["url"], original)

    def test_wrong_content_and_public_directory_fail(self):
        self.oss.publish(self.config)
        self.oss.object = b"wrong-content"
        with self.assertRaises(RuntimeError):
            self.oss.check(self.config)
        self.oss.object = self.config.read_bytes()
        self.oss.list_status = 200
        with self.assertRaises(RuntimeError):
            self.oss.check(self.config)

    def test_cli_serialization_and_error_redaction(self):
        oss = Subscription(self.deployment, "sub-example", "example", self.state)
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"Bucket": {}}', '')) as run:
            self.assertEqual(oss.api("get-bucket-info"), {"Bucket": {}})
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ["aliyun", "ossutil", "api", "get-bucket-info"])
            self.assertEqual(command[command.index("--region") + 1], "ap-southeast-1")
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, '', 'private-object-path')):
            with self.assertRaises(RuntimeError) as error:
                oss.api("get-bucket-info")
            self.assertNotIn('private-object-path', str(error.exception))


if __name__ == "__main__":
    unittest.main()
