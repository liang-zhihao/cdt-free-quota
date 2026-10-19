import ast
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.request
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from deployment_config import fc_environment, load_config, render_guard


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "deployment.json"
        self.config = dict(REGION_ID="ap-southeast-1", ZONE="ap-southeast-1d",
                           ECS_INSTANCE_ID="instance-example", ECS_NAME='example "node"',
                           EIP="203.0.113.1", BW_ID="bandwidth-example", OSS_BUCKET="bucket-example",
                           PUSH_TOKEN="fixture-not-a-token")
        self.source = (ROOT / "scripts/cdtguard.py").read_text()
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.config))

    def test_regions_and_existing_state_reader(self):
        for region, zone in (("cn-hongkong", "cn-hongkong-c"),
                             ("ap-southeast-1", "ap-southeast-1d")):
            self.config.update(REGION_ID=region, ZONE=zone)
            self.save()
            config = load_config(self.path)
            rendered = render_guard(self.source, config)
            guard = Path(self.tmp.name) / "guard.py"
            guard.write_text(rendered)
            reader = runpy.run_path(str(ROOT / "scripts/check-state.py"))
            reader["guard_const"].__globals__["GUARD"] = str(guard)
            for name, expected in (("OSS_EP", "oss-" + region + ".aliyuncs.com"),
                                   ("OSS_BUCKET", config["OSS_BUCKET"]), ("OSS_KEY", "cdtguard/state.json")):
                self.assertEqual(reader["guard_const"](name), expected)
            tree = ast.parse(rendered)
            values = {n.targets[0].id: n.value for n in tree.body if isinstance(n, ast.Assign)
                      and isinstance(n.targets[0], ast.Name)}
            self.assertEqual(ast.literal_eval(values["REGION"]), region)
            self.assertEqual(ast.literal_eval(values["BW_EP"]), "metrics." + region + ".aliyuncs.com")
            self.assertEqual(ast.literal_eval(values["INST_NAME"]), config["ECS_NAME"])
            self.assertIsNone(ast.literal_eval(values["THRESHOLD_ENV"]))
            self.assertEqual(fc_environment(config)["OSS_ENDPOINT"], ast.literal_eval(values["OSS_EP"]))

    def test_reject_incomplete_and_mismatched_config(self):
        for patch in ({"ZONE": "cn-hongkong-c"}, {"PUSH_TOKEN": ""},
                      {"ECS_INSTANCE_ID": "<YOUR_INSTANCE_ID>"}, {"REGOIN_ID": "typo"},
                      {"TRAFFIC_THRESHOLD_GB": 201}, {"TRAFFIC_THRESHOLD_GB": 0},
                      {"TRAFFIC_THRESHOLD_GB": float("nan")}, {"TRAFFIC_THRESHOLD_GB": True}):
            with self.subTest(patch=patch):
                self.path.write_text(json.dumps(dict(self.config, **patch)))
                with self.assertRaises(ValueError):
                    load_config(self.path)

    def test_threshold_and_source_drift(self):
        self.config["TRAFFIC_THRESHOLD_GB"] = 185
        rendered = render_guard(self.source, self.config)
        self.assertIn('THRESHOLD_ENV = "185"', rendered)
        self.assertEqual(fc_environment(self.config)["TRAFFIC_THRESHOLD_GB"], "185")
        with self.assertRaises(ValueError):
            render_guard(self.source.replace('BW_EP =', 'OLD_BW_EP ='), self.config)

    def test_guard_dry_run_and_no_upload_on_invalid_input(self):
        command = ["sh", str(ROOT / "scripts/deploy-cdtguard.sh"), str(ROOT / "scripts/cdtguard.py"),
                   "unused-host", "--config", str(self.path), "--dry"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(self.config["PUSH_TOKEN"], result.stdout + result.stderr)
        self.config["ZONE"] = "cn-hongkong-c"
        self.save()
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未上传", result.stderr)

    def run_fc(self, extra, configured=True, instance_found=True, expected_exit=0):
        captured = {}
        existing = dict(ALIYUN_AK="fixture-access", ALIYUN_SK="fixture-secret", EXTRA="keep",
                        ECS_INSTANCE_ID="old-instance", OSS_BUCKET="old-bucket", PUSH_TOKEN="old-token",
                        REGION_ID="cn-hongkong")
        current = types.SimpleNamespace(environment_variables=existing, code_size=1, handler="index.handler",
                                        runtime="nodejs18", timeout=60, memory_size=128)
        source = (ROOT / "scripts/fc-keeper/index.js").read_text()
        import re
        version = re.search("const VER = '([^']+)'", source).group(1)

        class FakeClient:
            def __init__(self, config):
                captured["endpoint"] = config.endpoint

            def get_function(self, *args):
                return types.SimpleNamespace(body=current)

            def update_function(self, name, request):
                captured["updated"] = request.body.environment_variables
                current.environment_variables = dict(request.body.environment_variables)
                return types.SimpleNamespace(body=types.SimpleNamespace(request_id="example"))

            def invoke_function(self, *args):
                captured["invoked"] = True
                return types.SimpleNamespace(body=json.dumps(dict(ver=version, evIsLiveness=True)).encode())

        modules = {}
        for package in ("alibabacloud_fc20230330", "alibabacloud_tea_openapi"):
            modules[package] = types.ModuleType(package)
            models = types.ModuleType(package + ".models")
            for name in ("Config", "GetFunctionRequest", "UpdateFunctionInput", "InputCodeLocation",
                         "UpdateFunctionRequest", "InvokeFunctionRequest"):
                setattr(models, name, types.SimpleNamespace)
            modules[package + ".models"] = models
            modules[package].models = models
        client = types.ModuleType("alibabacloud_fc20230330.client")
        client.Client = FakeClient
        modules[client.__name__] = client
        argv = [str(ROOT / "scripts/deploy-fc.py"), str(ROOT / "scripts/fc-keeper/index.js")]
        if configured:
            argv += ["--config", str(self.path)]
        argv += extra
        credentials = dict(ALIYUN_AK="fixture-access", ALIYUN_SK="fixture-secret", ALIYUN_UID="example",
                           ECS_INSTANCE_ID="old-instance", REGION_ID="cn-hongkong")
        instances = [{"Status": "Running"}] if instance_found else []
        response = io.BytesIO(json.dumps({"Instances": {"Instance": instances}}).encode())
        output = io.StringIO()
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, credentials, clear=True), \
                mock.patch.object(sys, "argv", argv), mock.patch("time.sleep"), \
                mock.patch.object(urllib.request, "urlopen", return_value=response), \
                mock.patch("subprocess.run", return_value=types.SimpleNamespace(returncode=0)) as verify, \
                contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_result:
                runpy.run_path(argv[0], run_name="__main__")
            self.assertEqual(exit_result.exception.code, expected_exit, output.getvalue())
            captured["verify"] = verify.call_args
        self.assertNotIn(self.config["PUSH_TOKEN"], output.getvalue())
        return captured

    def test_fc_merge_and_verification_region(self):
        result = self.run_fc([])
        self.assertEqual(result["endpoint"], "example.ap-southeast-1.fc.aliyuncs.com")
        self.assertEqual(result["updated"]["EXTRA"], "keep")
        self.assertEqual(result["updated"]["ALIYUN_SK"], "fixture-secret")
        self.assertEqual(result["updated"]["OSS_ENDPOINT"], "oss-ap-southeast-1.aliyuncs.com")
        self.assertEqual(result["verify"].kwargs["env"]["REGION_ID"], "ap-southeast-1")
        self.assertEqual(result["verify"].kwargs["env"]["ECS_INSTANCE_ID"], self.config["ECS_INSTANCE_ID"])

    def test_fc_dry_does_not_write_or_invoke(self):
        result = self.run_fc(["--dry"])
        self.assertNotIn("updated", result)
        self.assertNotIn("invoked", result)
        self.assertIsNone(result["verify"])

    def test_fc_legacy_arguments_keep_environment(self):
        result = self.run_fc([], configured=False)
        self.assertEqual(result["endpoint"], "example.cn-hongkong.fc.aliyuncs.com")
        self.assertEqual(result["updated"]["ECS_INSTANCE_ID"], "old-instance")
        self.assertNotIn("OSS_ENDPOINT", result["updated"])

    def test_fc_missing_target_is_not_verified(self):
        result = self.run_fc([], instance_found=False, expected_exit=1)
        self.assertNotIn("invoked", result)
        result = self.run_fc(["--allow-unverified"], instance_found=False)
        self.assertNotIn("invoked", result)

    def test_eventbridge_example_expands_region(self):
        text = (ROOT / "scripts/provision/README.md").read_text()
        start = text.index('aliyun fc CreateTrigger', text.index('# eventbridge 型'))
        command = text[start:text.index('```', start)]
        for region in ("cn-hongkong", "ap-southeast-1"):
            shell = "aliyun() { while [ \"$1\" != --body ]; do shift; done; shift; printf '%s' \"$1\"; }\n" + command
            result = subprocess.run(["sh"], input=shell, text=True, capture_output=True,
                                    env=dict(os.environ, REGION=region, ACCOUNT_UID="example"))
            self.assertEqual(result.returncode, 0, result.stderr)
            body = json.loads(result.stdout)
            self.assertEqual(body["sourceArn"], "acs:eventbridge:" + region + ":example:eventbus/default")

    def test_name_checker_exports_and_uncalled_guard(self):
        checker = str(ROOT / "ci/check-names.py")
        module = Path(self.tmp.name) / "module.py"
        for prefix, expected in (("__all__ = ['exported']\n", 0), ("", 1)):
            module.write_text(prefix + "def exported():\n    return 1\n")
            result = subprocess.run([sys.executable, checker, str(module)], capture_output=True)
            self.assertEqual(result.returncode, expected)


if __name__ == "__main__":
    unittest.main()
