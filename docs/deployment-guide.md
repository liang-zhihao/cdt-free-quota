# 香港 / 新加坡部署、Clash 与订阅

从本页开始，可在同一仓库完成地域选择、资源创建、CDT 守护、函数计算、Hysteria2、Mac / Clash 客户端和固定 HTTPS 订阅。各阶段先验证再继续，计费资源按当前报价创建。已有资源可从对应阶段继续。

先阅读 [免责声明](../DISCLAIMER.md)、[前置条件](../README.md) 和 [计费说明](billing.md)。香港与新加坡都属于非中国内地额度池；同一账号同时部署两地会共享该池，不能给两台机器各算一份 200 GB。本流程面向单地域部署；迁移时先结算并纳入当月已有用量，再启用新实例。

## 1. 选择地域并准备资源

以下二选一，在同一终端保留地域变量：

```sh
export REGION_ID=cn-hongkong
# 或：export REGION_ID=ap-southeast-1
export REGION="$REGION_ID"
aliyun ecs DescribeZones --RegionId "$REGION"
```

选择查询返回且支持目标规格的可用区，设置 `ZONE`。香港形如 `cn-hongkong-c`，新加坡形如 `ap-southeast-1d`；库存以实时查询为准。准备 [工具链](toolchain.md) 所需阿里云 CLI、SSH、Python，以及 FC 部署依赖。

按 [资源创建命令](../scripts/provision/README.md) 依次执行下面各节。该文档使用同一个 `$REGION` / `$ZONE`；JSON 请求体中的 `<YOUR_*>` 必须填为目标资源值，不能直接照抄占位符。

| 顺序 | 步骤 | 继续前的判据 |
|---|---|---|
| 1 | §3 RAM 策略与角色 | ECS / FC 各自运行身份可用，策略指向目标桶和实例 |
| 2 | §4.1–4.4 网络、安全组、私有状态桶 | VSwitch 可用区正确，状态桶保持私有，SSH 来源受限 |
| 3 | `build-image.sh` → `import-image.sh` | 镜像已在所选地域可用；导入脚本读取 `REGION_ID` |
| 4 | §4.5 ECS | 目标实例绑定运行角色，抢占中断策略为 Stop |
| 5 | §4.6–4.8 EIP、共享带宽关联 | EIP 绑定该实例和 `PayByDominantTraffic` 共享带宽包 |
| 6 | §5.1 FC 初次创建 | 函数、执行角色、初始环境变量属于目标地域 |
| 7 | §5.2–5.3 EventBridge 与触发器 | sourceArn / target 使用目标地域，触发器与清单一致 |

创建前按标签查重，计费写操作前查询报价。`deploy-fc.py` 更新已存在的函数，首次创建按 §5.1 的控制台或 API 流程执行。香港、新加坡成本和库存可能不同，示例价格不作为报价承诺。

## 2. 填写统一部署配置并验收守护

```sh
umask 077
# 香港：
cp examples/deployment.hongkong.json deployment.local.json
# 新加坡改用：cp examples/deployment.singapore.json deployment.local.json
chmod 600 deployment.local.json
```

填入该实例、EIP、共享带宽包、状态桶及告警通道。详细字段与阈值规则见 [地域部署配置](singapore-deployment.md)。默认沿用守护的地域额度判断，显式阈值只能向下设置。

```sh
sh scripts/deploy-cdtguard.sh scripts/cdtguard.py root@<YOUR_EIP> \
  --config deployment.local.json --dry
sh scripts/deploy-cdtguard.sh scripts/cdtguard.py root@<YOUR_EIP> \
  --config deployment.local.json
python3 scripts/deploy-fc.py scripts/fc-keeper/index.js --config deployment.local.json --dry
python3 scripts/deploy-fc.py scripts/fc-keeper/index.js --config deployment.local.json
```

必须完成守护采样、计费侧初始化、`check-state.py` 独立 OSS 回读、FC 实际调用与触发器清单回读。存活巡检的默认启用状态沿用 `inventory.json`。配置中的实例 ID 要与 SSH 目标一致；本地校验不会创建资源或证明云端身份正确。

## 3. 部署代理、客户端与订阅

接着执行 [Hysteria2、Mac、Clash 与订阅](proxy-clients.md)：

1. 本地准备密码、证书与 `proxy.local.json`；服务器地址取自统一部署配置。
2. 运行 `scripts/proxy_config.py`，生成完整服务端、Mac 与 Clash 配置。已有服务端配置可合并保留；已有 VLESS 客户端可生成 Clash 回退节点。
3. 开放所需 UDP 端口，安装 sing-box / OpenRC 服务，检查配置后部署并保留备份。
4. 验证 Mac 请求与完整下载，导入 Clash Meta 并在手机实际网络测试。
5. 准备独立订阅桶，使用 `scripts/subscription.py` 发布并验证固定链接。以后使用相同本地订阅记录更新；需要时可撤销链接。

## 4. 对比速度并维护

Hysteria2 的客户端上下行设置与 Mac 网卡选择是可调整项。使用同一网络、下载目标和文件大小比较原协议与新协议，只比较完整下载结果。UDP 不通或效果较差时切回原配置；带宽峰值与 CDT 阈值保持现有预算约束。

测速后关闭临时客户端，确认守护仍在采样、OSS 状态新鲜。部署失败按备份恢复；证书更换时同时更新客户端与订阅。实际账单是否抵扣必须到 CDT 账单核对，协议或测速成功不能证明计费正确。

仓库测试覆盖配置、订阅操作以及可选的真实内核回环连接。云资源从零创建和手机实机连接需按上述步骤验收，不以单元测试通过替代。
