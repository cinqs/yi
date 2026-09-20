# 手动先跑通一遍（M1）

自动化之前先用手动路径把协议跑通，这样后面出问题能立刻分辨"是协议的问题"还是"是我脚本的问题"。

## 1. 控制台买机器

- 地域：中国香港
- 计费：抢占式实例，**1 小时保护期**，出价模式选"跟随市场价"
- 规格：2 vCPU 2 GiB（`ecs.e-c1m1.large` 或 `ecs.t6-c1m1.large`）
- 镜像：Ubuntu 22.04 64bit
- 系统盘：ESSD 40GiB
- 带宽：**按使用流量**，峰值 100Mbps
- 密钥对：新建或导入一个 ed25519 公钥
- 安全组：入方向 443/TCP 放行；22/TCP 只放行你自己的公网 IP

## 2. 装 Xray

```bash
ssh root@<公网IP>

apt-get update -y && apt-get install -y curl openssl jq

cat >>/etc/sysctl.d/99-yi.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.ip_forward = 1
EOF
sysctl --system

curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh -o /tmp/x.sh
bash /tmp/x.sh install --version v1.8.24

UUID=$(cat /proc/sys/kernel/random/uuid)
SID=$(openssl rand -hex 8)
xray x25519          # 记下 Private key 和 Public key
```

## 3. 写配置

```bash
cat >/usr/local/etc/xray/config.json <<EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [{
    "listen": "0.0.0.0",
    "port": 443,
    "protocol": "vless",
    "settings": {
      "clients": [{ "id": "$UUID", "flow": "xtls-rprx-vision" }],
      "decryption": "none"
    },
    "streamSettings": {
      "network": "tcp",
      "security": "reality",
      "realitySettings": {
        "dest": "www.microsoft.com:443",
        "serverNames": ["www.microsoft.com"],
        "privateKey": "<PRIVATE>",
        "shortIds": ["$SID"]
      }
    },
    "sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"] }
  }],
  "outbounds": [{ "protocol": "freedom" }, { "protocol": "blackhole" }]
}
EOF

xray run -test -config /usr/local/etc/xray/config.json
systemctl restart xray
systemctl status xray --no-pager
ss -lnt | grep 443
```

## 4. 客户端链接

把 `<PUBLIC>`、`$UUID`、`$SID` 填进去：

```
vless://<UUID>@<PUBLIC>:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.microsoft.com&fp=chrome&pbk=<PUBLIC_KEY>&sid=<SID>&type=tcp&headerType=none#HK
```

## 5. 验证清单

- [ ] Android v2rayNG 导入 → 连接成功 → `ipinfo.io` 显示 Hong Kong
- [ ] Android 1080p 视频不卡，切 4G/Wi-Fi 都能重连
- [ ] macOS Clash Verge Rev 导入 → 系统代理开启 → Safari 能开 Google
- [ ] macOS TUN 模式开启 → `curl -s https://ipinfo.io/ip` 返回香港 IP
- [ ] 关掉客户端后本机恢复正常直连
- [ ] `yi doctor` 的远端端口检查通过

任何一项不过，先解决再进 M2。把结论记在这里：

```
实测日期：
运营商：
香港 IP：
延迟：____ ms   丢包：____ %
```
