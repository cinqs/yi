#!/usr/bin/env bash
#
# 交互式把阿里云 AccessKey 写进 ~/.aliyun/config.json。
#
# 为什么不直接 `cat > 文件 <<EOF`：
#   zsh 会把多行命令整段记进 ~/.zsh_history，密钥就跟着落进历史文件了。
#   这个脚本用 `read -s` 交互输入，密钥只存在于内存和最终那个 600 权限的文件里，
#   不会出现在命令行、shell 历史或任何对话记录中。
#
# 用法： ./tools/set-credentials.sh [profile名]     默认 profile 名 = yi

set -euo pipefail

PROFILE="${1:-yi}"
ALIYUN_DIR="${HOME}/.aliyun"
TARGET="${ALIYUN_DIR}/config.json"

echo "阿里云 AccessKey 写入工具"
echo "  目标文件 : ${TARGET}"
echo "  profile  : ${PROFILE}"
echo

if [ -f "${TARGET}" ]; then
  echo "注意：该文件已存在。我会保留其它 profile，只新增/覆盖 '${PROFILE}'。"
  echo
fi

printf 'AccessKey ID     （会显示）: '
read -r AK_ID
printf 'AccessKey Secret （不显示）: '
read -r -s AK_SECRET
echo
echo

if [ -z "${AK_ID}" ] || [ -z "${AK_SECRET}" ]; then
  echo "ID 或 Secret 是空的，已中止，什么都没写。" >&2
  exit 1
fi

mkdir -p "${ALIYUN_DIR}"
chmod 700 "${ALIYUN_DIR}"

AK_ID="${AK_ID}" AK_SECRET="${AK_SECRET}" PROFILE="${PROFILE}" TARGET="${TARGET}" python3 - <<'PY'
import json
import os

target = os.environ["TARGET"]
profile = os.environ["PROFILE"]

data = {"current": profile, "profiles": [], "meta": ""}
if os.path.exists(target):
    try:
        with open(target, "r", encoding="utf-8") as handle:
            existing = json.load(handle)
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict):
        data.update({k: v for k, v in existing.items() if k != "profiles"})
        data["profiles"] = [
            item for item in (existing.get("profiles") or []) if isinstance(item, dict)
        ]

data["profiles"] = [item for item in data["profiles"] if item.get("name") != profile]
data["profiles"].append(
    {
        "name": profile,
        "mode": "AK",
        "access_key_id": os.environ["AK_ID"],
        "access_key_secret": os.environ["AK_SECRET"],
    }
)
data["current"] = profile

tmp = target + ".tmp"
fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
os.replace(tmp, target)
os.chmod(target, 0o600)

names = ", ".join(str(item.get("name")) for item in data["profiles"])
print("已写入 {}（权限 600），现有 profile: {}".format(target, names))
print("其中 current = {}".format(data["current"]))
PY

echo
echo "下一步（都不花钱，只读）："
echo "  cd $(cd "$(dirname "$0")/.." && pwd)"
echo "  ./yi init --profile ${PROFILE}"
echo "  ./yi doctor --api"
echo
echo "跑完这两个，把输出发出来，我会先确认权限和资源都对，再动手建机器。"
