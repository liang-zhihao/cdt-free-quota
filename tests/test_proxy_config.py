import copy
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.parse

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
            self.assertEqual(clash['rules'][-3:], ['RULE-SET,cn_domain,DIRECT',
                                                  'RULE-SET,cn_ip,DIRECT', 'MATCH,PROXY'])
            for cidr in ('::1/128', 'fc00::/7', 'fe80::/10'):
                self.assertIn('IP-CIDR6,' + cidr + ',DIRECT,no-resolve', clash['rules'][:-3])
            for name, behavior, location in (('cn_domain', 'domain', 'geosite'), ('cn_ip', 'ipcidr', 'geoip')):
                provider = clash['rule-providers'][name]
                self.assertEqual(provider['behavior'], behavior)
                self.assertEqual(provider['format'], 'mrs')
                self.assertEqual(provider['proxy'], 'PROXY')
                self.assertEqual(provider['interval'], 86400)
                self.assertTrue(provider['url'].endswith('/geo/' + location + '/cn.mrs'))
                self.assertEqual(provider['path'], './rules/' + name + '.mrs')
            self.assertEqual(clash['dns']['nameserver-policy'],
                             {'rule-set:cn_domain': ['https://dns.alidns.com/dns-query#DIRECT']})
            self.assertEqual(clash['dns']['direct-nameserver'], clash['dns']['nameserver-policy']['rule-set:cn_domain'])
            self.assertEqual(clash['dns']['nameserver'], ['https://cloudflare-dns.com/dns-query#PROXY'])
            self.assertEqual(clash['dns']['default-nameserver'], ['223.5.5.5', '119.29.29.29'])
            mac = json.loads((output / 'mac-client.json').read_text())
            self.assertEqual(mac['route'], {'final': 'hy2'})
            self.assertNotIn('dns', mac)

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
        clash['dns'] = {'enable': False}
        clash['rules'] = ['MATCH,PROXY']
        # 此用例只检查传输与证书；完整分流由 test_clash_routing_runtime 验证。
        clash.pop('rule-providers')
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

    def test_clash_routing_runtime(self):
        mihomo = os.environ.get('MIHOMO')
        if not mihomo:
            self.skipTest('设置 MIHOMO 后验证分流、DNS 与规则缓存')
        output = self.root / 'routing'
        generate(self.input, output)
        config = json.loads((output / 'clash.yaml').read_text())
        data = {}
        for name, behavior, payload in (('cn_domain', 'domain', 'cn.test\n'),
                                         ('cn_ip', 'ipcidr', '127.0.0.1/32\n')):
            source = self.root / (name + '.txt')
            target = self.root / (name + '.mrs')
            source.write_text(payload)
            subprocess.run([mihomo, 'convert-ruleset', behavior, 'text', str(source), str(target)],
                           check=True, capture_output=True)
            data['/' + name + '.mrs'] = target.read_bytes()

        dns_queries = {'domestic': [], 'foreign': []}

        class DNS(socketserver.BaseRequestHandler):
            def handle(self):
                packet, sock = self.request
                offset, labels = 12, []
                while packet[offset]:
                    size = packet[offset]
                    labels.append(packet[offset + 1:offset + size + 1].decode())
                    offset += size + 1
                name = '.'.join(labels)
                dns_queries[self.server.label].append(name)
                end = offset + 5
                qtype = struct.unpack('!H', packet[offset + 1:offset + 3])[0]
                count = int(qtype == 1)
                answer = b''
                if count:
                    address = '127.0.0.1' if name in ('cn.test', 'cn-ip.test') else '198.51.100.1'
                    answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 30, 4) + socket.inet_aton(address)
                sock.sendto(packet[:2] + struct.pack('!HHHHH', 0x8180, 1, count, 0, 0) + packet[12:end] + answer,
                            self.client_address)

        def serve(server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def close():
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.addCleanup(close)
            return server

        dns_addresses = {}
        for label in dns_queries:
            server = socketserver.ThreadingUDPServer(('127.0.0.1', 0), DNS)
            server.label = label
            serve(server)
            dns_addresses[label] = 'udp://127.0.0.1:' + str(server.server_address[1])

        failed = set()
        downloads = []

        class HTTP(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_CONNECT(self):
                self.send_response(200)
                self.end_headers()

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path in data:
                    downloads.append(path)
                    content = data[path]
                    status = 503 if path in failed else 200
                else:
                    content = b'proxy' if self.server.is_proxy else b'direct'
                    status = 200
                self.send_response(status)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        target = serve(http.server.ThreadingHTTPServer(('127.0.0.1', 0), HTTP))
        target.is_proxy = False
        proxy = serve(http.server.ThreadingHTTPServer(('127.0.0.1', 0), HTTP))
        proxy.is_proxy = True
        config['proxies'] = [{'name': config['proxies'][0]['name'], 'type': 'http',
                              'server': '127.0.0.1', 'port': proxy.server_port}]
        config['mixed-port'] = free_port()
        config['external-controller'] = '127.0.0.1:' + str(free_port())
        config['log-level'] = 'info'
        # 替换外部数据和解析器端点，保持生成的规则顺序、DNS 策略和 provider 下载出站。
        for name, provider in config['rule-providers'].items():
            provider['url'] = 'http://rules.test/' + name + '.mrs'
        config['dns']['nameserver-policy']['rule-set:cn_domain'] = [dns_addresses['domestic']]
        config['dns']['direct-nameserver'] = [dns_addresses['domestic']]
        config['dns']['nameserver'] = [dns_addresses['foreign']]
        config['dns']['default-nameserver'] = [dns_addresses['domestic']]
        config['dns']['ipv6'] = False
        path = output / 'clash.yaml'
        path.write_text(json.dumps(config))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def api(route, method='GET'):
            request = urllib.request.Request('http://' + config['external-controller'] + route, method=method)
            with opener.open(request, timeout=5) as response:
                return json.loads(response.read() or b'null')

        def start(home):
            log = (home / 'mihomo.log').open('w')
            process = subprocess.Popen([mihomo, '-d', str(home), '-f', str(path)], stdout=log, stderr=log)

            def stop():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                log.close()
            self.addCleanup(stop)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll(), (home / 'mihomo.log').read_text())
                try:
                    api('/version')
                    return stop
                except (OSError, urllib.error.URLError):
                    time.sleep(.05)
            self.fail('Mihomo 控制端口未就绪')

        def request(host):
            result = subprocess.run(['curl', '--silent', '--show-error', '--fail', '--max-time', '5',
                                     '--noproxy', '', '-x', 'http://127.0.0.1:' + str(config['mixed-port']),
                                     'http://' + host + ':' + str(target.server_port)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode() + (output / 'mihomo.log').read_text())
            return result.stdout

        stop = start(output)
        for name in config['rule-providers']:
            try:
                api('/providers/rules/' + name, 'PUT')
            except urllib.error.HTTPError as error:
                self.fail(error.read().decode() + (output / 'mihomo.log').read_text())
        self.assertEqual(set(downloads), set(data))
        self.assertEqual(request('cn.test'), b'direct')
        self.assertIn('cn.test', dns_queries['domestic'])
        self.assertNotIn('cn.test', dns_queries['foreign'])
        self.assertEqual(request('cn-ip.test'), b'direct')
        self.assertEqual(request('foreign.test'), b'proxy')
        self.assertIn('foreign.test', dns_queries['foreign'])
        self.assertNotIn('foreign.test', dns_queries['domestic'])
        self.assertEqual(request('127.0.0.1'), b'direct')
        log = (output / 'mihomo.log').read_text()
        self.assertIn('RuleSet(cn_domain)', log)
        self.assertIn('RuleSet(cn_ip)', log)
        self.assertIn('Match', log)

        failed.update(data)
        for name in config['rule-providers']:
            with self.assertRaises(urllib.error.HTTPError):
                api('/providers/rules/' + name, 'PUT')
        self.assertEqual(request('cn.test'), b'direct')
        stop()
        stop = start(output)
        self.assertEqual(request('cn.test'), b'direct')
        stop()
        empty = self.root / 'no-cache'
        empty.mkdir()
        stop = start(empty)
        providers = api('/providers/rules')['providers']
        self.assertEqual(providers['cn_domain']['ruleCount'], 0)
        self.assertEqual(providers['cn_ip']['ruleCount'], 0)
        self.assertEqual(request('cn.test'), b'proxy')
        stop()


if __name__ == '__main__':
    unittest.main()
