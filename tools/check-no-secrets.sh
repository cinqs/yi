#!/usr/bin/env bash
# 开源前的最后一道闸：确认仓库里没有混进真实凭据或真实云资源 ID。
#
# 为什么需要它：这个项目天然会接触 AccessKey、SSH 私钥、代理凭据、
# 以及"我自己的"交换机 / 安全组 / 实例 ID。这些东西一旦 commit，
# 就算马上删掉也已经在 GitHub 的历史里了。所以交给脚本去数，不靠自觉。
#
# 用法：./tools/check-no-secrets.sh      （make check 会自动调用）
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

# 只扫"会被发布出去的内容"：有 git 就按 git 索引扫（未被跟踪的文件再脏也无所谓），
# 没有 git 就退回遍历目录，但要排除构建产物和虚拟环境。
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  list_files() { git ls-files; }
else
  list_files() {
    find . -type f \
      -not -path './.git/*' -not -path './.venv/*' -not -path './vendor/*' \
      -not -path './dist/*' -not -path './build/*' -not -path './.ruff_cache/*' \
      -not -path './.playwright-cli/*' -not -path '*.egg-info/*' \
      | sed 's|^\./||'
  }
fi

# 明确"假得一眼看得出来"的形态，避免误报：
#   - 一整段重复字符（0000…、1111…）是测试用的占位 ID
#   - RFC 5737 文档用 IP、示例域名
FAKE_MARKER='0{8,}|1{8,}|2{8,}|3{8,}|4{8,}|5{8,}|6{8,}|7{8,}|8{8,}|9{8,}|EXAMPLE|example\.(com|org|net)|(^|[^0-9])192\.0\.2\.|198\.51\.100\.|203\.0\.113\.'

fail=0

# check <标题> <提示> <正则> [--ignore-case]
# 注意：命中内容必须用命令替换取回，**不能**写成 `scan | report` ——
# 管道右侧是子壳，里面设的 fail=1 出了子壳就没了，脚本会"报了错却返回 0"。
check() {
  local title="$1" hint="$2" pat="$3"; shift 3
  local hits
  hits="$(scan "$pat" "$@")"
  [ -z "$hits" ] && return 0
  printf '\n\033[31m✗ %s\033[0m\n' "$title"
  printf '%s\n' "$hits" | sed 's/^/    /'
  printf '  \033[2m%s\033[0m\n' "$hint"
  fail=1
}

scan() {  # scan <正则> [--ignore-case]
  local pat="$1"; shift
  local flags="-n"
  [ "${1:-}" = "--ignore-case" ] && flags="-ni"
  list_files | while IFS= read -r f; do
    [ -f "$f" ] || continue
    # grep 二进制安全：-I 跳过二进制文件，避免刷屏
    grep -I $flags -E "$pat" "$f" 2>/dev/null | sed "s|^|$f:|"
  done | grep -Ev "$FAKE_MARKER" || true
}

echo "== 脱敏检查 =="

# 1. 阿里云 AccessKey ID：LTAI + 一长串，公开仓库里出现就是事故
check "发现疑似阿里云 AccessKey ID" \
  "AccessKey 只能放在 ~/.aliyun/config.json 或环境变量里，永远不要进仓库。" \
  'LTAI[A-Za-z0-9]{10,}'

# 2. 私钥正文
check "发现私钥正文" \
  "SSH 私钥 / REALITY 私钥都必须在生成地原地保存，不进版本库。" \
  'BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY'

# 3. 真实形态的云资源 ID（16 位以上；测试用的全 0 占位会被 FAKE_MARKER 过滤掉）
check "发现真实形态的阿里云资源 ID" \
  "换成 vsw-000… 这类占位值，或写进 docs 的示例里并加 EXAMPLE 标记。" \
  '(vsw|sg|i|vpc|eip|eni|nat|cbwp)-[a-z0-9]{16,}'

# 4. 真实代理凭据：UUID 必须是 v4 形态、且不是 1111… 那种占位值
check "发现真实形态的 UUID" \
  "确认它不是某台真实服务器的凭据；测试请用 11111111-2222-… 这种占位值。" \
  '\b[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b'

# 5. 本地状态文件不该被跟踪（里面有 AccessKey 引用、SSH 私钥、代理配置）
tracked_state="$(list_files | grep -E '(^|/)(state\.json|identity\.json|config\.toml|id_ed25519|known_hosts|applied\.json)$' || true)"
if [ -n "$tracked_state" ]; then
  printf '\n\033[31m✗ %s\033[0m\n' "发现被跟踪的本地状态文件"
  printf '%s\n' "$tracked_state" | sed 's/^/    /'
  printf '  \033[2m%s\033[0m\n' "这些文件在 ~/.config/yi 下，属于运行时状态，必须留在 .gitignore 里。"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "  OK —— 没有发现需要脱敏的内容"
fi
exit "$fail"
