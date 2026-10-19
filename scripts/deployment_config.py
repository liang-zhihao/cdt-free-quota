"""两个部署入口共用的配置读取；不执行云端操作。"""
import ast
import json
import math
from pathlib import Path

__all__ = ["load_config", "fc_environment", "render_guard"]


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required = ("REGION_ID", "ZONE", "ECS_INSTANCE_ID", "ECS_NAME", "EIP",
                "BW_ID", "OSS_BUCKET", "PUSH_TOKEN")
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象")
    unknown = set(data) - set(required) - {"TRAFFIC_THRESHOLD_GB"}
    if unknown:
        raise ValueError("未知配置键: " + ", ".join(sorted(unknown)))
    for key in required:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or "<YOUR_" in value:
            raise ValueError("配置未填写: " + key)
    region = data["REGION_ID"]
    if region not in ("cn-hongkong", "ap-southeast-1"):
        raise ValueError("此部署配置入口支持 cn-hongkong / ap-southeast-1")
    zone = data["ZONE"]
    suffix = zone[len(region):] if zone.startswith(region) else ""
    expected_prefix = "-" if region == "cn-hongkong" else ""
    if len(suffix) != len(expected_prefix) + 1 or not suffix.startswith(expected_prefix) or not suffix[-1:].isalpha():
        raise ValueError("ZONE 必须属于 REGION_ID；实际可用性请用 DescribeZones 查询")
    if "TRAFFIC_THRESHOLD_GB" in data:
        value = data["TRAFFIC_THRESHOLD_GB"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("TRAFFIC_THRESHOLD_GB 必须是数值")
        if not math.isfinite(value) or not 0 < value <= 200:
            raise ValueError("TRAFFIC_THRESHOLD_GB 必须大于 0 且不超过 200")
    return data


def fc_environment(config):
    env = {key: config[key] for key in (
        "REGION_ID", "ZONE", "ECS_INSTANCE_ID", "ECS_NAME", "EIP", "OSS_BUCKET", "PUSH_TOKEN")}
    env["OSS_ENDPOINT"] = "oss-" + config["REGION_ID"] + ".aliyuncs.com"
    env["TRAFFIC_THRESHOLD_GB"] = str(config.get("TRAFFIC_THRESHOLD_GB", 200))
    return env


def render_guard(source, config):
    """只替换模块级配置值，保留函数、注释与 check-state.py 可读的常量形态。"""
    region = config["REGION_ID"]
    values = {
        "REGION": region, "ZONE": config["ZONE"], "INSTANCE_ID": config["ECS_INSTANCE_ID"],
        "INST_NAME": config["ECS_NAME"], "EIP": config["EIP"], "BW_ID": config["BW_ID"],
        "OSS_BUCKET": config["OSS_BUCKET"], "PUSH_TOKEN": config["PUSH_TOKEN"],
        "BW_EP": "metrics." + region + ".aliyuncs.com",
        "OSS_EP": "oss-" + region + ".aliyuncs.com",
    }
    replacements = {key: json.dumps(value, ensure_ascii=True) for key, value in values.items()}
    # 未指定阈值时保留按实测地域判断、未知地域取较小额度的原有行为。
    replacements["THRESHOLD_ENV"] = (json.dumps(str(config["TRAFFIC_THRESHOLD_GB"]))
                                     if "TRAFFIC_THRESHOLD_GB" in config else "None")
    tree = ast.parse(source)
    lines = source.encode("utf-8").splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in replacements:
            continue
        if target.id in found:
            raise ValueError("配置常量重复: " + target.id)
        found.add(target.id)
        value = node.value
        start = offsets[value.lineno - 1] + value.col_offset
        end = offsets[value.end_lineno - 1] + value.end_col_offset
        edits.append((start, end, replacements[target.id].encode("utf-8")))
    missing = set(replacements) - found
    if missing:
        raise ValueError("守护源码缺少配置常量: " + ", ".join(sorted(missing)))
    result = source.encode("utf-8")
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    result = result.decode("utf-8")
    compile(result, "cdtguard.py", "exec")
    return result
