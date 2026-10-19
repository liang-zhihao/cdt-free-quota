# Hysteria2、Mac、Clash 与订阅

香港或新加坡实例均可使用本流程。先完成 [完整部署流程](deployment-guide.md) 中的资源、守护和 FC 验收，再配置代理和订阅。实际网络决定速度；Hysteria2 使用 UDP，受限网络可以切回保留的 VLESS 配置。

## 准备与生成

需要 Python 3.9+、OpenSSL、sing-box，以及测试时使用的 Mihomo。本流程验证过 sing-box 1.14.2 和 Mihomo 1.19.32，安装请使用各自官方发行渠道。保持已有服务的版本一致，再用对应内核检查配置。

以下命令在仓库根目录执行；真实配置与生成结果存入被忽略的目录：

```sh
umask 077
mkdir -p deployment-output
chmod 700 deployment-output
cp examples/proxy.json proxy.local.json
openssl rand -base64 32 > hy2-password.txt
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -pkeyopt ec_param_enc:named_curve -nodes \
  -keyout hy2-key.pem -out hy2-cert.pem -days 365 \
  -subj '/CN=proxy.example.com' -addext 'subjectAltName=DNS:proxy.example.com'
chmod 600 proxy.local.json hy2-password.txt hy2-key.pem hy2-cert.pem
```

服务器 EIP 从 `deployment.local.json` 读取，节点名称随香港／新加坡地域生成。`proxy.example.com` 在此作为证书校验名称，不需要 DNS 解析；若换名称，证书 CN/SAN 与 `server_name` 必须一起修改。输入路径相对于配置文件所在目录。密码、证书、节点文件和订阅 URL 都应留在本机私密目录。

`up_mbps=20` / `down_mbps=100` 是客户端速率起点，单位 Mbps；应根据实际网络调低或调高，不等同于购买了对应云带宽。`mac_interface` 默认留空。只有确认 Mac 上另一条 VPN 的默认路由干扰连接时，才通过 `route -n get default` 与 `networksetup -listallhardwareports` 找到实际物理网卡并填写；不要把 Mac 网卡名放进手机配置。

已有代理时，把当前服务器完整配置保存到 `server-current.json`，再生成。新实例省略 `--server-config` 参数，会生成完整 Hysteria2 服务端配置：

```sh
export SSH_TARGET='root@<YOUR_EIP>'
scp "$SSH_TARGET:/etc/sing-box/config.json" ./server-current.json
chmod 600 server-current.json
python3 scripts/proxy_config.py --deployment deployment.local.json --config proxy.local.json \
  --server-config server-current.json --output-dir deployment-output/generated
```

输出目录必须尚不存在，避免误覆盖已有配置。再次生成时使用新目录，验证后切换。

| 文件 | 用途 |
|---|---|
| `server-inbound.json` | 单个 Hysteria2 入口，供人工合并 |
| `server.json` | 新实例生成完整最小配置；提供旧配置时在其中增加或更新 `cdt-hy2` 入口；保留其他入口及路由 |
| `mac-client.json` | Mac 本机 HTTP/SOCKS 入口，默认 `127.0.0.1:17890` |
| `clash.yaml` | 可直接导入 Clash Meta；使用 YAML 支持的 JSON 表示法 |

可在 `proxy.local.json` 中添加 `"vless_client_file": "./vless-client.local.json"`，引用原有 sing-box 客户端配置；工具会将其中唯一的 VLESS/REALITY 出站作为 Clash 回退节点，且要求与当前 EIP 一致。Mac 可直接保留旧客户端配置用于对比。

生成工具不会复制证书私钥到客户端文件。Mac 信任提供的证书，Clash 固定完整证书的 SHA256 指纹，并保持证书检查开启。服务端 JSON 不带私钥内容，只引用固定路径。

## 部署与回滚

现有 TCP 入口可以与 UDP 443 共存。先检查安全组是否已有规则，再按实际客户端来源设置 UDP 入站；移动网络来源经常改变，若选 `0.0.0.0/0`，该端口会对公网开放，服务由随机密码和 TLS 保护。

```sh
export REGION_ID=$(python3 -c 'import json; print(json.load(open("deployment.local.json"))["REGION_ID"])')
export SECURITY_GROUP_ID='<YOUR_SECURITY_GROUP_ID>'
export CLIENT_CIDR='<YOUR_CLIENT_CIDR>'
aliyun ecs DescribeSecurityGroupAttribute --RegionId "$REGION_ID" --SecurityGroupId "$SECURITY_GROUP_ID"
aliyun ecs AuthorizeSecurityGroup --RegionId "$REGION_ID" --SecurityGroupId "$SECURITY_GROUP_ID" \
  --IpProtocol udp --PortRange 443/443 --SourceCidrIp "$CLIENT_CIDR" --Description cdt-hy2
```

若输入里的 `port` 不是 443，同步调整规则。记录返回的规则 ID，回滚时仅撤销这条新增规则。不要因为 TCP 测速网站显示的来源 IP 就认定 UDP 使用相同出口；VPN、运营商和移动网络可能让两者不同。

新实例先按 [sing-box 官方安装说明](https://sing-box.sagernet.org/installation/) 安装验证版本；已有实例继续使用原来的内核版本并先做配置检查。Alpine / OpenRC 可使用仓库的 `scripts/sing-box.initd`，其中可执行文件路径为 `/usr/local/bin/sing-box`，安装位置不同应调整该文件。

先在服务器创建私有暂存目录，再上传：

```sh
ssh "$SSH_TARGET" 'mkdir -p /etc/sing-box/cdt-next && chmod 700 /etc/sing-box/cdt-next'
scp hy2-cert.pem hy2-key.pem deployment-output/generated/server.json "$SSH_TARGET:/etc/sing-box/cdt-next/"
scp scripts/sing-box.initd "$SSH_TARGET:/etc/sing-box/cdt-next/sing-box.initd"
ssh "$SSH_TARGET" 'sh -s' <<'REMOTE'
set -e
stage=/etc/sing-box/cdt-next
chmod 600 "$stage"/*
python3 - <<'PY_CHECK'
import json
from pathlib import Path
stage = Path('/etc/sing-box/cdt-next')
config = json.loads((stage / 'server.json').read_text())
for inbound in config['inbounds']:
    if inbound.get('tag') == 'cdt-hy2':
        inbound['tls']['certificate_path'] = str(stage / 'hy2-cert.pem')
        inbound['tls']['key_path'] = str(stage / 'hy2-key.pem')
(stage / 'check.json').write_text(json.dumps(config))
PY_CHECK
/usr/local/bin/sing-box check -c "$stage/check.json"
backup="/etc/sing-box/backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup"
for f in config.json hy2-cert.pem hy2-key.pem; do
  [ ! -f "/etc/sing-box/$f" ] || cp -p "/etc/sing-box/$f" "$backup/$f"
done
[ ! -f /etc/init.d/sing-box ] || cp -p /etc/init.d/sing-box "$backup/sing-box.initd"
cp "$stage/server.json" /etc/sing-box/config.json
cp "$stage/hy2-cert.pem" "$stage/hy2-key.pem" /etc/sing-box/
chmod 600 /etc/sing-box/config.json /etc/sing-box/hy2-cert.pem /etc/sing-box/hy2-key.pem
if [ ! -f /etc/init.d/sing-box ]; then
  cp "$stage/sing-box.initd" /etc/init.d/sing-box
  chmod 755 /etc/init.d/sing-box
fi
rc-update add sing-box default
printf '备份目录：%s\n' "$backup"
rc-service sing-box restart
rc-service sing-box status
REMOTE
```

启动之后还要完成下面的请求与下载验收。失败时按输出中的备份目录恢复 `config.json`、已有证书与原服务定义，再重启；首次安装没有旧配置时停止新服务并撤销新增 UDP 规则。修改证书前保存旧文件，切换客户端证书与服务端证书应在同一维护窗口完成。

## Mac 与手机

Mac 使用生成的本机客户端，不需要修改系统代理：

```sh
sing-box check -c deployment-output/generated/mac-client.json
sing-box run -c deployment-output/generated/mac-client.json
```

在另一终端测试：

```sh
curl --proxy http://127.0.0.1:17890 --max-time 15 -I https://www.google.com/generate_204
curl --proxy http://127.0.0.1:17890 --fail --max-time 30 -o /dev/null \
  -w '下载字节=%{size_download} 耗时秒=%{time_total} 字节每秒=%{speed_download}\n' \
  'https://speed.cloudflare.com/__down?bytes=20000000'
```

第二项会消耗约 20 MB 下载流量。比较旧协议和新协议时，使用相同网络、URL、文件大小和次数，只比较完整下载；超时或未达到目标字节数单独记录，不当作有效速度。测速后在客户端终端按 Ctrl+C 结束。换算：MB/s = 字节每秒 ÷ 1,000,000，Mbps = MB/s × 8。

Clash Meta for Android：配置 → 新增 → 从文件导入 `clash.yaml` → 启用 → 选择对应地域的 `HongKong-HY2` 或 `Singapore-HY2`。使用「规则」模式启用下面的分流；需要保留原有节点时，在应用中保留旧配置，便于切回。

### 中国大陆分流

Clash 默认按以下顺序匹配，首个命中即生效：

1. 本机、局域网与链路本地地址直连，包含 IPv4 和 IPv6。
2. 大陆域名集合 `cn_domain` 命中时直连。
3. 大陆 IP 集合 `cn_ip` 命中时直连；允许解析域名，补充域名集合未覆盖的目标。
4. 其余流量进入 `PROXY` 选中的香港／新加坡节点。

规则来自 [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) 的 `geo/geosite/cn.mrs` 和 `geo/geoip/cn.mrs`。客户端通过 `PROXY` 下载，每 24 小时检查更新，分别缓存在客户端配置目录的 `rules/cn_domain.mrs` 与 `rules/cn_ip.mrs`。规则集合可能遗漏或误分类，命中大陆规则不等于运营商链路或服务归属一定在大陆。

DNS 保留 fake-ip：大陆域名通过阿里 DNS DoH 直连解析，其他域名通过代理访问 Cloudflare DoH；直连出口也使用阿里 DNS。`223.5.5.5` 和 `119.29.29.29` 仅作为解析 DNS 服务域名的引导解析器。字段依据 [Mihomo DNS 文档](https://wiki.metacubex.one/config/dns/)；Mac 的 `mac-client.json` 继续将请求交给 Hysteria2，不采用这套 Clash 分流。

首次启用需等待两个规则集加载成功，可在客户端规则提供者信息或日志中确认规则数量非零，再检查国内目标命中 `cn_domain` / `cn_ip`、海外目标命中 `MATCH`。国内网站仍绕行时，先检查应用处于「规则」模式、规则集加载状态和实际命中记录，再判断是否为规则遗漏。

Mihomo 1.19.32 的故障测试表明：更新请求失败后仍使用已有规则，重启也可读取缓存；首次下载失败且没有缓存时，内核仍能启动，但大陆规则数量为零，相关流量可能落到 `MATCH,PROXY`。因此「客户端启动成功」不能替代分流验收。恢复节点连接后手动更新两个规则集，再检查规则数量及命中情况；首次下载依赖可用代理节点。

## 固定 HTTPS 订阅

使用新建的专用桶，避免修改守护状态桶的访问控制。桶不按个数收固定月租，按存储、请求、下载计费；发布前查看 [对应地域 OSS 当前价格](https://www.aliyun.com/price/detail/oss#module3)。以下命令使用已登录的阿里云 CLI profile，每条顺序执行。

先填写一个全局唯一的专用桶名，再确认同名桶不存在、账号允许所需对象访问策略：

```sh
export ALIYUN_PROFILE='<YOUR_PROFILE>'
export SUB_BUCKET='<YOUR_SUBSCRIPTION_BUCKET>'
oss() { aliyun ossutil api "$@" --profile "$ALIYUN_PROFILE" --region "$REGION_ID"; }
oss list-buckets --tag-key cdt-role --tag-value subscription
oss list-buckets --prefix "$SUB_BUCKET"
oss get-public-access-block
```

若已按标签查到同地域的专用订阅桶，复用它；只有确认目标是新建专用桶后才执行创建。若同名桶已存在，先核实所有权、用途和配置，不直接套用下列写操作。账号级公共访问阻断若已开启，应先选择符合账号策略的托管方式；不要为此自动关闭账号级保护。

```sh
oss put-bucket --bucket "$SUB_BUCKET" --acl private \
  --create-bucket-configuration '{"StorageClass":"Standard","DataRedundancyType":"LRS"}'
oss put-bucket-tags --bucket "$SUB_BUCKET" \
  --tagging '{"TagSet":{"Tag":[{"Key":"project","Value":"cdt-free-quota"},{"Key":"cdt-role","Value":"subscription"}]}}'
oss put-bucket-public-access-block --bucket "$SUB_BUCKET" \
  --public-access-block-configuration '{"BlockPublicAccess":false}'
```

发布到这个专用桶：

```sh
python3 scripts/subscription.py publish --deployment deployment.local.json \
  --bucket "$SUB_BUCKET" --profile "$ALIYUN_PROFILE" \
  --state deployment-output/subscription.json --config deployment-output/generated/clash.yaml --dry

python3 scripts/subscription.py publish --deployment deployment.local.json \
  --bucket "$SUB_BUCKET" --profile "$ALIYUN_PROFILE" \
  --state deployment-output/subscription.json --config deployment-output/generated/clash.yaml
```

工具回读桶的地域、私有 ACL 与 `cdt-role=subscription` 标签，然后上传随机路径下的单个公开可读对象；守护状态桶会被拒绝。上传后验证匿名下载内容一致、桶目录匿名返回 403。账号级访问策略或权限阻断时会失败，不会自动放宽保护。

订阅 URL 位于 `deployment-output/subscription.json` 的 `url` 字段。Clash Meta 选择“从 URL 导入”。链接没有到期时间；任何持有链接的人都能读取节点密码，不能公开分享。

以后对同一个 `--state` 文件重复运行 `publish`，即更新同一个 URL。上传中断后重试仍复用已保存的对象路径。手机执行“更新配置”后生效。只校验时把 `publish` 换成 `check`。

已有订阅启用大陆分流时，先用更新后的生成器输出到新目录，检查 `clash.yaml` 后按相同 `--state` 重新发布；手机更新订阅并确认两个规则集已加载。此后大陆域名／IP 数据由客户端定期更新，无需为了规则数据更新重新发布订阅。主配置更新与规则数据更新是两个独立步骤。

撤销下载访问：

```sh
python3 scripts/subscription.py revoke --deployment deployment.local.json \
  --bucket "$SUB_BUCKET" --profile "$ALIYUN_PROFILE" --state deployment-output/subscription.json
```

撤销 URL 不会让已下载的节点凭据失效。发生泄漏时，还需更换服务端密码和客户端配置，并使用新的本地记录文件发布新链接；保留旧记录用于核对旧链接已撤销。

## 证书与验证

查看证书有效期：`openssl x509 -in hy2-cert.pem -noout -dates`。在到期前生成新证书与客户端配置，并在同一维护窗口切换服务器和订阅；证书固定模式下两端不一致时会暂时拒绝连接。请将到期时间加入自己的维护日历。

运行离线测试与本机回环测试：

```sh
SING_BOX=/absolute/path/to/sing-box MIHOMO=/absolute/path/to/mihomo \
  python3 -m unittest discover -s tests -v
```

测试生成临时证书，在本机启动服务端与两种客户端，检查实际请求、证书错误拒绝、原有入口保留、端口冲突和文件权限。分流用例保留生成的规则顺序和 DNS 策略，以本机 MRS 数据、HTTP 代理和 UDP DNS 替换外部服务，验证域名直连、IP 兜底、其余代理、DNS 查询去向及规则缓存故障行为；该用例不验证公网 DoH 连通性。结束后关闭测试进程。未设置内核路径时会明确跳过运行时测试。该测试证明配置与对应内核兼容，不代表手机实机或任意运营商线路已验证。

字段依据：[sing-box Hysteria2 服务端](https://sing-box.sagernet.org/configuration/inbound/hysteria2/)、[sing-box Hysteria2 客户端](https://sing-box.sagernet.org/configuration/outbound/hysteria2/)、[Mihomo Hysteria2](https://wiki.metacubex.one/config/proxies/hysteria2/)。

回环测试使用 HTTP/1.1 和明确 Content-Length。测试中曾观察到 HTTP/1.0 无长度短连接响应经 sing-box 返回连接重置，尚未定位到内核具体行为；这类目标应单独验证。
