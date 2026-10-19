# 新加坡部署配置

香港和新加坡可以使用同一套部署入口。资源创建仍按 [provision](../scripts/provision/README.md) 的依赖顺序逐步执行；本文将地域、实例、状态桶和告警通道集中到一份本地 JSON，再交给已有部署脚本。

## 1. 准备同地域资源

先完成 [前置条件](../README.md) 与 [免责声明](../DISCLAIMER.md) 的确认。创建计费资源前查询当前报价，按标签判重，每步完成后回读。

```sh
export REGION_ID=ap-southeast-1
REGION="$REGION_ID"
aliyun ecs DescribeZones --RegionId "$REGION"
```

从返回结果选择支持目标实例规格的可用区。示例中的 `ap-southeast-1d` 仅说明格式，实际库存由查询确定。VPC、VSwitch、ECS、自定义镜像、EIP、共享带宽包、OSS、FC 和 EventBridge 均使用同一地域。RAM 为账号级资源，策略中的实例 ARN 要使用实际地域、账号与实例 ID。

依次执行 provision 中的 RAM → 网络和私有状态桶 → 镜像 → ECS → EIP 与共享带宽包关联 → FC → EventBridge 和触发器。镜像导入脚本读取 `REGION_ID`。FC 初次创建时先把示例中的地域、可用区和 OSS 端点换为本节值；`deploy-fc.py` 更新已经存在的函数，不负责创建函数或触发器。

| 配置 | 新加坡 | 香港 |
|---|---|---|
| REGION_ID | ap-southeast-1 | cn-hongkong |
| 可用区格式 | ap-southeast-1d | cn-hongkong-c |
| OSS_ENDPOINT / OSS_EP | oss-ap-southeast-1.aliyuncs.com | oss-cn-hongkong.aliyuncs.com |
| BW_EP | metrics.ap-southeast-1.aliyuncs.com | metrics.cn-hongkong.aliyuncs.com |

## 2. 填写一份本地配置

在仓库根目录执行：

```sh
umask 077
cp examples/deployment.singapore.json deployment.local.json
chmod 600 deployment.local.json
```

编辑 `deployment.local.json` 中的占位符。香港可以改用 `examples/deployment.hongkong.json`。配置键如下：

- `REGION_ID`、`ZONE`：目标地域与查询得到的可用区。
- `ECS_INSTANCE_ID`、`ECS_NAME`、`EIP`：实际目标实例的信息。
- `BW_ID`：与该 EIP 关联的共享带宽包 ID。
- `OSS_BUCKET`：守护使用的私有状态桶，提前创建并授予运行身份所需权限。
- `PUSH_TOKEN`：上游 FC 要求的 PushPlus token。
- `TRAFFIC_THRESHOLD_GB`：可选数值，仅允许大于 0 且不超过 200。省略时，守护沿用实测地域判定，地域未知时先按较小额度；填 185 可留出余量。200 是这两个地域的额度上限，并不预留超调余量。

JSON 只存部署配置；云 API 身份沿用原部署方式。ECS 绑定运行角色，永久 AK/SK 不写入生成的守护文件。FC 部署仍需要原有 SDK、部署期环境变量，以及现有函数已配置的运行时身份。

该配置文件已加入 `.gitignore`。请勿上传配置、部署输出或包含告警 token 的服务器备份。配置入口当前支持香港、新加坡；原来的手工配置方式继续可用。

## 3. 校验并部署守护

```sh
sh scripts/deploy-cdtguard.sh scripts/cdtguard.py root@<YOUR_EIP> \
  --config deployment.local.json --dry

sh scripts/deploy-cdtguard.sh scripts/cdtguard.py root@<YOUR_EIP> \
  --config deployment.local.json
```

`--dry` 只做本地配置与源码检查，不连接服务器。配置中的实例 ID 必须与 SSH 目标一致；执行前用 `DescribeInstances` 回读地域、可用区、EIP 和实例角色。

脚本在私有临时目录生成待部署副本，再沿用原有上传、校验、备份、重启和验收流程；退出时清除本地临时副本。配置以常量写入副本，因此 OpenRC 清空环境变量也不会丢失。现有 `check-state.py` 可以直接读取其中的状态桶和端点。原调用方式与 `--no-restart` 继续可用。

## 4. 更新 FC 与验收

将同一份配置交给现有 FC 部署脚本：

```sh
python3 scripts/deploy-fc.py scripts/fc-keeper/index.js \
  --config deployment.local.json --dry

python3 scripts/deploy-fc.py scripts/fc-keeper/index.js \
  --config deployment.local.json
```

这里的 `--dry` 会读取现有函数配置，但不会更新或调用函数。配置文件里的地域和实例 ID 优先于同名本机环境变量；FC 查询、更新、实例状态检查和末尾回读使用同一个目标。FC 环境变量采用“读回 → 合并指定配置 → 写回 → 逐项复核”，其他键保持原值。部署脚本不会修改触发器启用状态。

按 provision §5.2/§5.3 在新加坡创建 EventBridge 规则和三个触发器，ARN 使用 `$REGION`。核对 `inventory.json` 声明的名称、cron、启用状态和 payload。UTC+8 的每日巡检时刻与地域无关。

验收时确认：

1. 守护启动日志中的计费侧初始化查询成功，状态文件时间持续推进。
2. 上传并运行 `scripts/check-state.py`，独立读回私有状态桶，月份正确、时间新鲜。
3. FC 部署末尾的代码版本、存活巡检分支与部署事实回读均通过。
4. 账单中对应地域的 CDT 免费额度实际抵扣；配置检查不代替账单核对。

失败时依据具体错误处理，不通过删除触发器声明来跳过验收。守护可按部署输出中的备份路径恢复并重启；FC 更新前应保存原代码及完整环境变量，回滚时一并恢复。

## 验证边界

自动测试覆盖香港、新加坡配置生成、无效输入拒绝、FC 环境变量保留、回读地域一致性及原调用兼容性。它们使用本地模拟，不创建计费资源，不代表在全新账号完成了端到端部署。
