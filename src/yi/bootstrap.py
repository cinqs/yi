"""Renders the cloud-init user-data that turns a bare Ubuntu VM into a VLESS server.

The script is intentionally self-contained: it downloads nothing except the pinned
Xray release, so a spot instance that gets recycled can be rebuilt from the same
bytes every time.
"""

from __future__ import annotations

from typing import Any

_SCRIPT = r"""#!/bin/bash
set -euo pipefail

exec > >(tee -a /var/log/yi-bootstrap.log) 2>&1
echo "[yi] bootstrap start $(date -Is)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends curl jq openssl ca-certificates unzip

# --- kernel tuning: BBR matters a lot on long-haul mobile links -------------
cat > /etc/sysctl.d/99-yi.conf <<'SYSCTL'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.ip_forward = 1
net.core.somaxconn = 8192
net.ipv4.tcp_fastopen = 3
fs.file-max = 1048576
SYSCTL
sysctl --system >/dev/null

# --- xray ------------------------------------------------------------------
curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh -o /tmp/xray-install.sh
bash /tmp/xray-install.sh install {{XRAY_INSTALL_ARGS}}

mkdir -p /root/yi /usr/local/etc/xray
chmod 700 /root/yi

UUID="{{UUID}}"
SHORT_ID="{{SHORT_ID}}"
PRIVATE_KEY="{{PRIVATE_KEY}}"
PUBLIC_KEY="{{PUBLIC_KEY}}"

# 凭据优先用调用方注入的（客户端已经导入过，重建时应该保持不变）；
# 没有注入就现场生成，保证脚本单独跑也能用。
if [ -z "${UUID}" ] || [ -z "${PRIVATE_KEY}" ] || [ -z "${PUBLIC_KEY}" ]; then
  echo "[yi] 未注入凭据，本次由服务端现场生成（重建后客户端需要重新导入配置）"
  UUID=$(cat /proc/sys/kernel/random/uuid)
  SHORT_ID=$(openssl rand -hex 8)
  KEYS=$(xray x25519)
  # 1.8.x 输出 "Private key / Public key"，25+ 输出 "PrivateKey / Password (PublicKey)"
  PRIVATE_KEY=$(printf '%s\n' "$KEYS" | grep -i 'private' | head -n1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '[:space:]')
  PUBLIC_KEY=$(printf '%s\n' "$KEYS" | grep -iE 'public|password' | head -n1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '[:space:]')
fi
if [ -z "$PRIVATE_KEY" ] || [ -z "$PUBLIC_KEY" ]; then
  echo "[yi] REALITY key generation failed"
  exit 1
fi
echo "[yi] 凭据指纹: ${UUID:0:8}"

# --- REALITY 伪装目标：逐个试，用第一个真正能用的 -----------------------------
# 这里必须实测而不是硬编码。REALITY 会把 dest 的真实证书链抓下来、把签名换成
# 自己算的 HMAC 再发给客户端；证书链一旦超过它的缓冲上限，握手就装不完，
# 客户端会拿到"未修改的真实证书"，判定对面不是 REALITY 服务器并回退成浏览器爬虫。
# 实测案例：www.microsoft.com 的证书链 8273 字节 -> 必失败；cloudflare/bing/apple 正常。
write_server_config() {
  local dest="$1"
  # xray 按**文件扩展名**判断配置格式，临时文件必须以 .json 结尾，
  # 否则 `xray run -test` 会报 "Failed to get format"（踩过）。
  local tmpcfg=/tmp/yi-server-config.json
  cat > "$tmpcfg" <<EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": {{XRAY_PORT}},
      "protocol": "vless",
      "settings": {
        "clients": [{ "id": "${UUID}"{{FLOW_FRAGMENT}} }],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${dest}:443",
          "xver": 0,
          "serverNames": ["${dest}"],
          "privateKey": "${PRIVATE_KEY}",
          "shortIds": ["${SHORT_ID}"]
        }
      },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"] }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "direct" },
    { "protocol": "blackhole", "tag": "block" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      { "type": "field", "ip": ["geoip:private"], "outboundTag": "direct" }
    ]
  }
}
EOF
  if ! xray run -test -config "$tmpcfg" >/dev/null 2>&1; then
    return 1
  fi
  mv "$tmpcfg" /usr/local/etc/xray/config.json
  systemctl restart xray
  return 0
}

systemctl enable xray

# --- a throwaway client config, used to prove this box can actually proxy -----
mkdir -p /opt/yi
write_client_config() {
  local sni="$1"
  cat > /root/yi/client-test.json <<EOF
{
  "log": { "loglevel": "none" },
  "inbounds": [
    { "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
      "settings": { "udp": false } }
  ],
  "outbounds": [
    {
      "protocol": "vless",
      "settings": {
        "vnext": [
          {
            "address": "127.0.0.1",
            "port": {{XRAY_PORT}},
            "users": [{ "id": "${UUID}", "encryption": "none"{{FLOW_FRAGMENT}} }]
          }
        ]
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "serverName": "${sni}",
          "fingerprint": "{{FINGERPRINT}}",
          "publicKey": "${PUBLIC_KEY}",
          "shortId": "${SHORT_ID}"
        }
      }
    }
  ]
}
EOF
}

cat > /opt/yi/selftest.sh <<'SELFTEST_SCRIPT'
#!/bin/bash
# Runs a throwaway Xray *client* against our own REALITY endpoint over loopback.
# Without a phone in the room, this is the only way to prove the server works.
set -uo pipefail
CFG=/root/yi/client-test.json
LOG=/tmp/yi-selftest.log

pkill -f "xray run -c ${CFG}" >/dev/null 2>&1 || true
sleep 0.3
nohup /usr/local/bin/xray run -c "$CFG" >"$LOG" 2>&1 &
PID=$!

for _ in $(seq 1 20); do
  if ss -lnt 2>/dev/null | grep -q '127.0.0.1:10808'; then break; fi
  sleep 0.5
done

CODE=$(curl -sS --max-time 15 --socks5-hostname 127.0.0.1:10808 \
        -o /dev/null -w '%{http_code}' https://www.gstatic.com/generate_204 2>/dev/null)
kill "$PID" >/dev/null 2>&1 || true

if [ "$CODE" = "204" ]; then
  echo "ok"
else
  echo "failed(http_code=${CODE:-none})"
  tail -n 10 "$LOG" 2>/dev/null
fi
SELFTEST_SCRIPT
chmod +x /opt/yi/selftest.sh

WINNER_DEST=""
SELFTEST_RESULT="failed(no candidate tried)"
for DEST in {{REALITY_DESTS}}; do
  echo "[yi] 尝试伪装目标: ${DEST}"
  write_client_config "${DEST}"
  if ! write_server_config "${DEST}"; then
    SELFTEST_RESULT="failed(config rejected for ${DEST})"
    echo "[yi] ${DEST} 的配置被 xray 拒绝，跳过"
    continue
  fi
  # `awk '{print $1}'` 去掉 tr 补出来的尾随空格，否则 "ok " != "ok"，循环不会中断（踩过）
  SELFTEST_RESULT=$(/opt/yi/selftest.sh 2>&1 | head -n1 | tr -d '"\\' | tr -c '[:print:]' ' ' | awk '{print $1}')
  echo "[yi] ${DEST} 自检结果: ${SELFTEST_RESULT}"
  if [ "${SELFTEST_RESULT}" = "ok" ]; then
    WINNER_DEST="${DEST}"
    break
  fi
done

if [ -z "${WINNER_DEST}" ]; then
  echo "[yi] 所有候选伪装目标都自检失败，保留最后一个配置；客户端大概率连不上"
  WINNER_DEST=$(echo {{REALITY_DESTS}} | awk '{print $1}')
  write_client_config "${WINNER_DEST}"
  write_server_config "${WINNER_DEST}" || true
fi
echo "[yi] 最终伪装目标: ${WINNER_DEST}"

for _ in $(seq 1 30); do
  systemctl is-active --quiet xray && break
  sleep 1
done
if ! systemctl is-active --quiet xray; then
  echo "[yi] xray failed to start"
  journalctl -u xray --no-pager -n 50 || true
  exit 1
fi

# --- host firewall as a second layer (the cloud security group is the first) --
if command -v ufw >/dev/null 2>&1; then
  ufw --force reset >/dev/null 2>&1 || true
  ufw default deny incoming >/dev/null 2>&1 || true
  ufw default allow outgoing >/dev/null 2>&1 || true
  ufw allow {{XRAY_PORT}}/tcp >/dev/null 2>&1 || true
  ufw allow 22/tcp >/dev/null 2>&1 || true
  ufw --force enable >/dev/null 2>&1 || true
fi

# --- self check + machine-readable handoff ----------------------------------
if ! ss -lnt 2>/dev/null | grep -q ':{{XRAY_PORT}} '; then
  echo "[yi] port {{XRAY_PORT}} is not listening"
  exit 1
fi

PUBLIC_IP=$(curl -fsS --max-time 5 http://100.100.100.200/latest/meta-data/public-ipv4 || true)
XRAY_VERSION=$(xray version | head -n1 | awk '{print $2}')

cat > /root/yi/server-info.json.tmp <<EOF
{
  "ready": true,
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "selftest": "${SELFTEST_RESULT}",
  "xray_version": "${XRAY_VERSION}",
  "protocol": "vless",
  "transport": "tcp",
  "security": "reality",
  "address": "${PUBLIC_IP}",
  "port": {{XRAY_PORT}},
  "uuid": "${UUID}",
  "flow": "{{FLOW}}",
  "sni": "${WINNER_DEST}",
  "fingerprint": "{{FINGERPRINT}}",
  "public_key": "${PUBLIC_KEY}",
  "short_id": "${SHORT_ID}"
}
EOF
mv /root/yi/server-info.json.tmp /root/yi/server-info.json
chmod 600 /root/yi/server-info.json

echo "[yi] bootstrap done $(date -Is)"
"""


def render_user_data(config: dict[str, Any], identity: dict[str, Any] | None = None) -> str:
    flow = str(config.get("vless_flow") or "").strip()
    dests = config.get("reality_dests")
    if not dests:
        # 向后兼容：老配置里可能只有单个 reality_sni
        dests = [config.get("reality_sni") or "www.cloudflare.com"]
    if isinstance(dests, str):
        dests = [dests]
    xray_version = str(config.get("xray_version") or "").strip()
    # "latest" / 空 == 不传 --version，让官方脚本装最新版
    install_args = "" if xray_version.lower() in ("", "latest") else f"--version {xray_version}"
    replacements = {
        "{{XRAY_INSTALL_ARGS}}": install_args,
        "{{XRAY_PORT}}": str(int(config["xray_port"])),
        "{{REALITY_DESTS}}": " ".join(str(item).strip() for item in dests if str(item).strip()),
        "{{FINGERPRINT}}": str(config["reality_fingerprint"]),
        "{{FLOW}}": flow,
        "{{FLOW_FRAGMENT}}": f', "flow": "{flow}"' if flow else "",
    }
    # 注入已有的凭据，让重建出来的机器对客户端"还是同一台"
    for token, key in (
        ("{{UUID}}", "uuid"),
        ("{{SHORT_ID}}", "short_id"),
        ("{{PRIVATE_KEY}}", "private_key"),
        ("{{PUBLIC_KEY}}", "public_key"),
    ):
        replacements[token] = str((identity or {}).get(key) or "")
    script = _SCRIPT
    for token, value in replacements.items():
        script = script.replace(token, value)
    return script
