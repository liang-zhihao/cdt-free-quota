#!/usr/bin/env python3
"""生成 sing-box 与 Clash Meta 配置；只读输入，只写新的本地输出目录。"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import ssl
import sys
from deployment_config import load_config

__all__ = ["generate"]


def generate(config_path, output_dir, server_config=None, deployment=None):
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("配置必须是 JSON 对象")
    if deployment is not None:
        config["server"] = deployment["EIP"]
    required = ("server", "server_name", "password_file", "certificate_file")
    allowed = set(required) | {"port", "up_mbps", "down_mbps", "mac_interface", "mac_port", "vless_client_file"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("配置键不受支持")
    for key in required:
        if not isinstance(config.get(key), str) or not config[key].strip() or "<YOUR_" in config[key]:
            raise ValueError("请填写 " + key)
    for key, default in (("port", 443), ("mac_port", 17890)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError(key + " 必须是有效端口")
        config[key] = value
    for key, default in (("up_mbps", 20), ("down_mbps", 100)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(key + " 必须是有限正数")
        config[key] = value
    interface = config.get("mac_interface", "")
    if not isinstance(interface, str):
        raise ValueError("mac_interface 必须是字符串")
    password = (config_path.parent / config["password_file"]).read_text(encoding="utf-8").strip()
    if not password or "<YOUR_" in password:
        raise ValueError("密码文件为空或仍为占位符")
    certificate = (config_path.parent / config["certificate_file"]).read_text(encoding="utf-8")
    der = ssl.PEM_cert_to_DER_cert(certificate)
    # Mihomo 使用完整证书指纹固定服务端身份，不能用公钥摘要替代。
    fingerprint = hashlib.sha256(der).hexdigest()
    inbound = {
        "type": "hysteria2", "tag": "cdt-hy2", "listen": "0.0.0.0", "listen_port": config["port"],
        "up_mbps": config["down_mbps"], "down_mbps": config["up_mbps"],
        "users": [{"name": "client", "password": password}],
        "tls": {"enabled": True, "certificate_path": "/etc/sing-box/hy2-cert.pem",
                "key_path": "/etc/sing-box/hy2-key.pem", "alpn": ["h3"]},
    }
    outbound = {
        "type": "hysteria2", "tag": "hy2", "server": config["server"], "server_port": config["port"],
        "up_mbps": config["up_mbps"], "down_mbps": config["down_mbps"], "password": password,
        "tls": {"enabled": True, "server_name": config["server_name"], "certificate": certificate, "alpn": ["h3"]},
    }
    if interface:
        outbound["bind_interface"] = interface
    mac = {"log": {"level": "warn"},
           "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": config["mac_port"]}],
           "outbounds": [outbound], "route": {"final": "hy2"}}
    label = {"cn-hongkong": "HongKong", "ap-southeast-1": "Singapore"}.get(
        (deployment or {}).get("REGION_ID"), "CDT")
    node = {"name": label + "-HY2", "type": "hysteria2", "server": config["server"], "port": config["port"],
            "password": password, "up": str(config["up_mbps"]) + " Mbps", "down": str(config["down_mbps"]) + " Mbps",
            "sni": config["server_name"], "skip-cert-verify": False, "fingerprint": fingerprint, "alpn": ["h3"]}
    domestic_dns = ["https://dns.alidns.com/dns-query#DIRECT"]
    rule_base = "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/"
    providers = {
        name: {"type": "http", "behavior": behavior, "format": "mrs", "proxy": "PROXY",
               "path": "./rules/" + name + ".mrs", "url": rule_base + location + "/cn.mrs",
               "interval": 86400}
        for name, behavior, location in (("cn_domain", "domain", "geosite"), ("cn_ip", "ipcidr", "geoip"))
    }
    clash = {"mixed-port": 7890, "allow-lan": False, "mode": "rule", "log-level": "warning",
             "dns": {"enable": True, "enhanced-mode": "fake-ip",
                     "default-nameserver": ["223.5.5.5", "119.29.29.29"],
                     "nameserver-policy": {"rule-set:cn_domain": domestic_dns},
                     "direct-nameserver": domestic_dns,
                     "nameserver": ["https://cloudflare-dns.com/dns-query#PROXY"]},
             "proxies": [node], "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": [node["name"]]}],
             "rule-providers": providers,
             "rules": ["IP-CIDR,127.0.0.0/8,DIRECT,no-resolve", "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
                       "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve", "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
                       "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve", "IP-CIDR6,::1/128,DIRECT,no-resolve",
                       "IP-CIDR6,fc00::/7,DIRECT,no-resolve", "IP-CIDR6,fe80::/10,DIRECT,no-resolve",
                       "RULE-SET,cn_domain,DIRECT", "RULE-SET,cn_ip,DIRECT", "MATCH,PROXY"]}
    if config.get("vless_client_file"):
        existing_client = json.loads((config_path.parent / config["vless_client_file"]).read_text(encoding="utf-8"))
        candidates = [o for o in existing_client.get("outbounds", []) if o.get("type") == "vless"]
        if len(candidates) != 1:
            raise ValueError("回退客户端必须包含一个 VLESS 出站")
        vless = candidates[0]
        reality = vless["tls"]["reality"]
        if vless["server"] != config["server"] or not reality.get("enabled"):
            raise ValueError("回退节点必须使用当前部署的同一出口和 REALITY")
        fallback = {"name": label + "-VLESS", "type": "vless", "server": vless["server"],
                    "port": vless["server_port"], "uuid": vless["uuid"], "udp": True,
                    "tls": True, "servername": vless["tls"]["server_name"],
                    "network": "tcp", "flow": vless.get("flow", ""),
                    "client-fingerprint": vless["tls"].get("utls", {}).get("fingerprint", "chrome"),
                    "reality-opts": {"public-key": reality["public_key"], "short-id": reality["short_id"]}}
        clash["proxies"].append(fallback)
        clash["proxy-groups"][0]["proxies"].append(fallback["name"])
    server = (json.loads(Path(server_config).read_text(encoding="utf-8")) if server_config else
              {"log": {"level": "warn"}, "inbounds": [], "outbounds": [{"type": "direct", "tag": "direct"}],
               "route": {"final": "direct"}})
    inbounds = server.setdefault("inbounds", [])
    if not isinstance(inbounds, list):
        raise ValueError("服务端 inbounds 必须是数组")
    matches = [i for i, item in enumerate(inbounds) if item.get("tag") == inbound["tag"]]
    if len(matches) > 1:
        raise ValueError("服务端存在重复 Hysteria2 入口")
    for item in inbounds:
        if item.get("tag") != inbound["tag"] and item.get("type") in ("hysteria", "hysteria2", "tuic") and item.get("listen_port") == config["port"]:
            raise ValueError("已有 QUIC 入口使用该端口，请调整 port")
    if matches:
        inbounds[matches[0]] = copy.deepcopy(inbound)
    else:
        inbounds.append(copy.deepcopy(inbound))
    files = {"server-inbound.json": inbound, "server.json": server, "mac-client.json": mac, "clash.yaml": clash}
    rendered = {name: json.dumps(data, ensure_ascii=False, indent=2) + "\n" for name, data in files.items()}
    output = Path(output_dir)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        for name, content in rendered.items():
            path = output / name
            with path.open("x", encoding="utf-8") as stream:
                path.chmod(0o600)
                stream.write(content)
    except Exception:
        shutil.rmtree(output)
        raise
    return sorted(rendered)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True, help="香港或新加坡部署 JSON")
    parser.add_argument("--config", required=True, help="代理凭据路径与速率设置 JSON")
    parser.add_argument("--output-dir", required=True, help="必须是尚不存在的目录")
    parser.add_argument("--server-config", help="现有服务端 JSON；提供时保留已有入口和路由")
    args = parser.parse_args()
    try:
        names = generate(args.config, args.output_dir, args.server_config, load_config(args.deployment))
    except (ValueError, OSError, TypeError, KeyError):
        sys.exit("生成失败：请检查输入、证书、端口与输出目录；已有文件保持原状")
    print("已生成：" + ", ".join(names))


if __name__ == "__main__":
    main()
