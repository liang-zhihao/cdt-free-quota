import copy
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from proxy_config import generate


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-pkeyopt', 'ec_param_enc:named_curve',
                        '-nodes', '-keyout', str(self.root / 'key.pem'), '-out', str(self.root / 'cert.pem'),
                        '-days', '1', '-subj', '/CN=proxy.example.com', '-addext', 'subjectAltName=DNS:proxy.example.com'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.root / 'password.txt').write_text('temporary-test-password')
        self.config = dict(server='127.0.0.1', server_name='proxy.example.com',
                           password_file='password.txt', certificate_file='cert.pem',
                           port=free_port(socket.SOCK_DGRAM), mac_port=free_port())
        self.input = self.root / 'config.json'
        self.input.write_text(json.dumps(self.config))
        self.base = self.root / 'base.json'
        self.base.write_text(json.dumps({'inbounds': [], 'outbounds': [{'type': 'direct', 'tag': 'direct'}],
                                        'route': {'final': 'direct'}, 'log': {'level': 'debug'}}))

    def test_merge_permissions_and_mobile_independence(self):
        original = json.loads(self.base.read_text())
        original['inbounds'].append({'type': 'mixed', 'listen': '127.0.0.1', 'listen_port': 18000, 'tag': 'existing'})
        self.base.write_text(json.dumps(original))
        self.config['mac_interface'] = 'en0'
        self.input.write_text(json.dumps(self.config))
        output = self.root / 'output'
        generate(self.input, output, self.base)
        server = json.loads((output / 'server.json').read_text())
        self.assertEqual(server['inbounds'][0], original['inbounds'][0])
        self.assertEqual(server['route'], original['route'])
        self.assertEqual(json.loads((output / 'mac-client.json').read_text())['outbounds'][0]['bind_interface'], 'en0')
        clash = json.loads((output / 'clash.yaml').read_text())
        self.assertNotIn('interface-name', clash)
        self.assertFalse(clash['proxies'][0]['skip-cert-verify'])
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        for path in output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            generate(self.input, output, self.base)
        self.assertEqual(json.loads((output / 'server.json').read_text()), server)

    def test_deployment_region_and_vless_fallback(self):
        import uuid
        fallback = {"outbounds": [{"type": "vless", "server": "203.0.113.1", "server_port": 443,
                    "uuid": str(uuid.uuid4()), "flow": "xtls-rprx-vision",
                    "tls": {"enabled": True, "server_name": "example.com",
                            "reality": {"enabled": True, "public_key": "fixture-public-key", "short_id": "fixture-id"}}}]}
        (self.root / "vless.json").write_text(json.dumps(fallback))
        self.config["vless_client_file"] = "vless.json"
        self.input.write_text(json.dumps(self.config))
        for region, label in (("cn-hongkong", "HongKong"), ("ap-southeast-1", "Singapore")):
            output = self.root / region
            generate(self.input, output, deployment={"REGION_ID": region, "EIP": "203.0.113.1"})
            clash = json.loads((output / "clash.yaml").read_text())
            self.assertEqual([p["name"] for p in clash["proxies"]], [label + "-HY2", label + "-VLESS"])
            self.assertEqual(clash["proxies"][0]["server"], "203.0.113.1")
            self.assertEqual(clash["proxies"][1]["uuid"], fallback["outbounds"][0]["uuid"])
            self.assertTrue((output / "server.json").exists())

    def test_invalid_config_does_not_create_output(self):
        for value in (0, float('nan'), True):
            self.config['down_mbps'] = value
            self.input.write_text(json.dumps(self.config))
            with self.assertRaises(ValueError):
                generate(self.input, self.root / 'invalid')
            self.assertFalse((self.root / 'invalid').exists())

    def test_port_collision(self):
        self.base.write_text(json.dumps({'inbounds': [{'type': 'hysteria2', 'tag': 'other', 'listen_port': self.config['port']}]}))
        with self.assertRaises(ValueError):
            generate(self.input, self.root / 'conflict', self.base)
        self.assertFalse((self.root / 'conflict').exists())

    def test_local_runtime(self):
        sing_box = os.environ.get('SING_BOX')
        mihomo = os.environ.get('MIHOMO')
        if not sing_box or not mihomo:
            self.skipTest('设置 SING_BOX 和 MIHOMO 可执行文件路径后运行真实内核回环验证')
        output = self.root / 'runtime'
        generate(self.input, output, self.base)
        server = json.loads((output / 'server.json').read_text())
        server['inbounds'][0]['listen'] = '127.0.0.1'
        server['inbounds'][0]['tls']['certificate_path'] = str(self.root / 'cert.pem')
        server['inbounds'][0]['tls']['key_path'] = str(self.root / 'key.pem')
        (output / 'server.json').write_text(json.dumps(server))
        mac_config = json.loads((output / 'mac-client.json').read_text())
        mac_config['log']['level'] = 'debug'
        (output / 'mac-client.json').write_text(json.dumps(mac_config))
        clash = json.loads((output / 'clash.yaml').read_text())
        clash['mixed-port'] = free_port()
        clash['dns']['enable'] = False
        clash['rules'] = ['MATCH,PROXY']
        (output / 'clash.yaml').write_text(json.dumps(clash))
        for name in ('server.json', 'mac-client.json'):
            result = subprocess.run([sing_box, 'check', '-c', str(output / name)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        subprocess.run([mihomo, '-t', '-d', str(self.root), '-f', str(output / 'clash.yaml')], check=True, capture_output=True)

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "15")
                self.end_headers()
                self.wfile.write(b'extension-smoke')

            def log_message(self, *args):
                pass

        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        processes = []
        logs = []

        def start(command):
            log = (self.root / ('process-' + str(len(logs)) + '.log')).open('wb')
            logs.append(log)
            process = subprocess.Popen(command, stdout=log, stderr=log)
            processes.append(process)
            return process

        def wait_port(port, process):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail('测试内核提前退出')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        return
                except OSError:
                    time.sleep(.05)
            self.fail('本机客户端监听超时')

        def request(port):
            return subprocess.run(['curl', '--silent', '--show-error', '--fail', '--max-time', '5', '--noproxy', '',
                                   '-x', 'http://127.0.0.1:' + str(port),
                                   'http://127.0.0.1:' + str(httpd.server_port)], capture_output=True)

        try:
            start([sing_box, 'run', '-c', str(output / 'server.json')])
            mac = start([sing_box, 'run', '-c', str(output / 'mac-client.json')])
            mobile = start([mihomo, '-d', str(self.root), '-f', str(output / 'clash.yaml')])
            wait_port(self.config['mac_port'], mac)
            wait_port(clash['mixed-port'], mobile)
            for port in (self.config['mac_port'], clash['mixed-port']):
                result = request(port)
                self.assertEqual(result.returncode, 0, str(port) + ': ' + result.stderr.decode() + "\n" + "\n".join(p.read_text() for p in self.root.glob("process-*.log")))
                self.assertEqual(result.stdout, b'extension-smoke')
            # 实际验证证书固定有效，错误指纹必须拒绝连接。
            bad = copy.deepcopy(clash)
            bad['mixed-port'] = free_port()
            bad['proxies'][0]['fingerprint'] = '00' * 32
            bad_path = output / 'bad.yaml'
            bad_path.write_text(json.dumps(bad))
            bad_client = start([mihomo, '-d', str(self.root), '-f', str(bad_path)])
            wait_port(bad['mixed-port'], bad_client)
            self.assertNotEqual(request(bad['mixed-port']).returncode, 0)
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for log in logs:
                log.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
