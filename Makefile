# 工具链解析，三档：
#   1. PATH 里的 uv（CI 与推荐方式）
#   2. 常见安装位置里的 uv —— uv 的官方安装脚本默认装到 ~/.local/bin，
#      而那里不一定在 PATH 里，这一档能省掉一次"为什么 make 说找不到 uv"
#   3. 仓库里已有的 .venv —— 保证 clone 完就能跑 make check，
#      不会因为"机器上少一个工具"而卡住
UV := $(shell command -v uv 2>/dev/null || \
         for c in "$$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do \
           [ -x "$$c" ] && { echo "$$c"; break; }; \
         done)
ifeq ($(UV),)
  ifeq ($(wildcard .venv/bin/python),)
    PY   := $(error 找不到 uv，也没有 .venv。先看 docs/quickstart.md 装一遍环境)
    RUFF := $(PY)
  else
    PY   := .venv/bin/python
    RUFF := .venv/bin/ruff
  endif
else
  # 用解析出来的绝对路径，别写死 "uv"：uv 官方安装脚本装到 ~/.local/bin，
  # 那里常常不在 PATH 里，写死就会变成 "uv: No such file or directory"。
  PY   := $(UV) run python
  RUFF := $(UV) run ruff
endif

.PHONY: help setup test lint fmt check run app site social clean secrets docs

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup:  ## 准备开发环境（Python 3.12 + 虚拟环境）
	uv python install 3.12
	uv venv --python 3.12
	uv pip install -e .
	uv pip install ruff

test:  ## 跑单元测试
	$(PY) -m unittest discover -s tests -v

lint:  ## 静态检查（ruff）
	$(RUFF) check src app tests
	$(RUFF) format --check src app tests

fmt:  ## 自动格式化
	$(RUFF) format src app tests
	$(RUFF) check --fix src app tests

secrets:  ## 检查仓库里有没有混进真实凭据 / 真实云资源 ID
	@./tools/check-no-secrets.sh

docs:  ## 检查文档里的本地链接是否都能落地
	@./tools/check-doc-links.sh

check:  ## 提交前跑这一条：脱敏 + 文档链接 + 界面语法 + 静态检查 + 测试
	@$(MAKE) --no-print-directory secrets
	@$(MAKE) --no-print-directory docs
	@echo "== 界面脚本语法 =="
	@node -e "const fs=require('fs');const h=fs.readFileSync('app/ui/index.html','utf8');\
	[...h.matchAll(/<script(?![^>]*application\/json)[^>]*>([\s\S]*?)<\/script>/g)].forEach(m=>new Function(m[1]));\
	console.log('  OK')"
	@echo "== 脚本语法 =="
	@for f in yi yi-app tools/*.sh app/build-macos.sh; do bash -n "$$f" || exit 1; done
	@echo "  OK"
	@$(MAKE) lint
	@$(MAKE) test
	@echo "== 全部通过 =="

run:  ## 启动本地 agent（浏览器打开提示的地址）
	./yi-app

app:  ## 打包 macOS 应用
	./app/build-macos.sh

site:  ## 本地预览 GitHub Pages 站点
	@cd docs && python3 -m http.server 8788

social:  ## 重新生成社交预览图 docs/assets/social-preview.png
	@./tools/make-social-preview.sh

clean:  ## 清理构建产物与缓存
	rm -rf dist build vendor .ruff_cache .playwright-cli
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf src/*.egg-info
