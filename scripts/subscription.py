#!/usr/bin/env python3
"""发布、验证或撤销 Clash HTTPS 订阅；使用已准备好的同地域专用 OSS 桶。"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request

from deployment_config import load_config

__all__ = ["Subscription"]


class Subscription:
    def __init__(self, deployment, bucket, profile, state_path, aliyun="aliyun"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
            raise ValueError("订阅桶名格式无效")
        if bucket == deployment["OSS_BUCKET"]:
            raise ValueError("订阅桶必须与守护状态桶分开")
        self.deployment = deployment
        self.bucket = bucket
        self.region = deployment["REGION_ID"]
        self.profile = profile
        self.state_path = Path(state_path)
        self.aliyun = aliyun
        self.endpoint = "https://" + bucket + ".oss-" + self.region + ".aliyuncs.com/"
        self.state = None
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state.get("bucket") != bucket or self.state.get("region") != self.region:
                raise ValueError("本地订阅记录与桶或地域不匹配")
            key = self.state.get("key", "")
            if not re.fullmatch(r"subscriptions/[A-Za-z0-9_-]{43}/clash.yaml", key):
                raise ValueError("订阅对象路径无效")
            if self.state.get("url") != self.endpoint + key:
                raise ValueError("订阅地址与桶或对象路径不一致")

    def api(self, action, *arguments):
        command = [self.aliyun, "ossutil", "api", action, "--bucket", self.bucket,
                   "--profile", self.profile, "--region", self.region, "--output-format", "json",
                   "--retry-times", "0", *arguments]
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if result.returncode:
            # CLI 可能回显带秘密对象路径的 URL；错误只报告操作名。
            raise RuntimeError("阿里云 CLI 操作失败: " + action + "；请检查登录、权限和公共访问阻断设置")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def preflight(self, publishing=False):
        info = self.api("get-bucket-info")["Bucket"]
        if info["Location"] != "oss-" + self.region:
            raise ValueError("订阅桶必须与部署地域一致")
        if info["AccessControlList"]["Grant"] != "private":
            raise ValueError("订阅桶 ACL 必须为 private")
        tags = self.api("get-bucket-tags").get("TagSet", {}).get("Tag", [])
        if not any(t.get("Key") == "cdt-role" and t.get("Value") == "subscription" for t in tags):
            raise ValueError("目标桶缺少 cdt-role=subscription 标签")
        if publishing and str(info.get("BlockPublicAccess", "true")).lower() != "false":
            raise ValueError("专用桶仍阻断公共访问，请按教程检查；工具不会自动修改桶策略")

    def save(self):
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + ".new")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, self.state_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def fetch(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "ClashMetaForAndroid"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, b""

    def read_config(self, path):
        content = Path(path).read_bytes()
        config = json.loads(content)
        proxies = config.get("proxies", [])
        if not proxies or not config.get("proxy-groups") or not config.get("rules"):
            raise ValueError("请使用 proxy_config.py 生成的完整 Clash 配置")
        if any(p.get("server") != self.deployment["EIP"] for p in proxies):
            raise ValueError("Clash 节点与当前部署 EIP 不一致")
        return content

    def check(self, config_path):
        if not self.state:
            raise ValueError("找不到本地订阅记录")
        expected = self.read_config(config_path)
        status, content = self.fetch(self.state["url"])
        if status != 200 or content != expected:
            raise RuntimeError("匿名下载失败或订阅内容不一致")
        status, _ = self.fetch(self.endpoint)
        if status != 403:
            raise RuntimeError("桶目录匿名访问未返回 403")

    def publish(self, config_path, dry=False):
        self.read_config(config_path)
        self.preflight(publishing=True)
        if dry:
            return
        if self.state is None:
            key = "subscriptions/" + secrets.token_urlsafe(32) + "/clash.yaml"
            self.state = dict(bucket=self.bucket, region=self.region, key=key,
                              url=self.endpoint + key, status="pending")
            # 上传前保存路径，超时后的重试仍复用原 URL。
            self.save()
        self.api("put-object", "--key", self.state["key"], "--body", Path(config_path).resolve().as_uri(),
                 "--object-acl", "public-read", "--storage-class", "Standard", "--cache-control", "no-store",
                 "--content-type", "application/yaml; charset=utf-8",
                 "--content-disposition", 'attachment; filename="clash.yaml"')
        self.check(config_path)
        self.state["status"] = "published"
        self.save()

    def revoke(self, dry=False):
        if self.state is None:
            raise ValueError("找不到本地订阅记录")
        self.preflight()
        if dry:
            return
        self.api("put-object-acl", "--key", self.state["key"], "--object-acl", "private")
        status, _ = self.fetch(self.state["url"])
        if status not in (403, 404):
            raise RuntimeError("撤销后链接仍可下载，请检查桶策略")
        self.state["status"] = "revoked"
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("publish", "check", "revoke"))
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--state", required=True, help="本地私密订阅记录；保存 URL，更新时复用")
    parser.add_argument("--config", help="proxy_config.py 生成的 Clash 文件")
    parser.add_argument("--dry", action="store_true", help="只读取、校验，不上传或修改 ACL")
    args = parser.parse_args()
    if args.action in ("publish", "check") and not args.config:
        parser.error("publish / check 需要 --config")
    try:
        subscription = Subscription(load_config(args.deployment), args.bucket, args.profile, args.state)
        if args.action == "publish":
            subscription.publish(args.config, args.dry)
        elif args.action == "revoke":
            subscription.revoke(args.dry)
        else:
            subscription.preflight()
            subscription.check(args.config)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.TimeoutExpired):
        sys.exit("订阅操作失败：请检查配置、专用桶地域/标签/ACL、CLI 登录与访问策略；URL 保留在本地记录")
    print("只读检查通过" if args.dry else "订阅操作通过；链接保存在 " + str(args.state))


if __name__ == "__main__":
    main()
