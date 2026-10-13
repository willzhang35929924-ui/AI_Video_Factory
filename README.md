# AI Video Factory

本地中文视频生产控制台：Next.js + FastAPI + SQLAlchemy / SQLite。源码、数据库迁移、Provider/Adapter、RGB/Alpha 抠像与独立合成工作流保留；业务数据与源码分离。

此开发版没有预置生产 Task、Run、Asset、Review、背景或成片。空状态不是故障，也不会回填 Mock 业务数据。历史报告仅为旧环境记录，不代表新环境验收。

## 目录

```text
frontend/             页面、组件、API 客户端、测试、package-lock.json
backend/app/          API、模型、Service、Adapter
backend/migrations/   显式数据库迁移
backend/tests/        测试（Mock 仅供测试/显式演示模式）
comfy_nodes/          Factory 自定义节点源码
workflows/            Factory RGB/Alpha 与复用合成 API 工作流
scripts/              初始化、启动、停止、可选跨平台工具
config/               脱敏模板；*.local.* 为本机私有配置
storage/models/       本机必要模型（不在源码包内）
storage/vendor/       本机模型依赖（含指纹所需 Git 元数据）
```

`backend/.venv`、`frontend/node_modules` 为本机依赖，不在源码包内。数据库、媒体、日志、浏览器资料、API Key 与 Git 历史同样不打包。

## 当前 Mac 启动

```bash
cd /Users/xiaoyao/AI_Video_Factory
backend/.venv/bin/python scripts/factory_launcher.py check
backend/.venv/bin/python scripts/factory_launcher.py start
```

网页：http://127.0.0.1:3000/dashboard；后端：http://127.0.0.1:8000/docs。
停止仅本启动器拥有的服务：

```bash
backend/.venv/bin/python scripts/factory_launcher.py stop
```

启动器不会启动 ComfyUI/浏览器/影刀，不恢复旧队列，不生成图片，不公开发布。现在 `config/launcher.local.json` 指向安全开发发布配置，真实公开发布与 Description 请求均禁用。缺少外部引擎时页面如实显示未就绪，不伪造成功。

## 新目录 / 新机器

只在尚未建立依赖和私有配置时执行，不能覆盖现有本机配置：

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend
npm ci
cd ..
cp .env.example .env
cp backend/.env.example backend/.env
cp config.example.yaml config.yaml
backend/.venv/bin/python scripts/init_factory.py --confirm-create
backend/.venv/bin/python scripts/factory_launcher.py start
```

初始化只建立不存在的新库；已有数据库、残留 WAL、符号链接、越界路径会拒绝。它运行 Alembic，建立默认项目与设置，不导入示例任务。不要调用 Mock reset 清库。
Windows 使用 `backend/.venv/Scripts/python.exe`；保留 `scripts/windows/` 为跨平台源码，而非本次已删除的迁移数据包。

## 配置

- `.env`：本机服务密钥；不放入 `NEXT_PUBLIC_*`。
- `backend/.env`：后端模式、旧引擎路径、SQLite 与 storage 位置。路径可按本机修改。
- `config.yaml`：原有私有发布/Description 配置，已原样保留；不要因历史配置曾允许发布就自动启动发布。
- `config/development-publishing.local.yaml`：当前开发入口实际消费的安全策略。
- `config/development.local.json`：已保留的两个真实账号身份映射与 Factory 设置。无历史发布、授权核验、商品绑定或生产资产。账号必须重新核实登录。
- `config/launcher.local.json`：端口、存储路径及实际发布配置路径。

在一个**新库**恢复这份本机配置时使用：

```bash
backend/.venv/bin/python scripts/init_factory.py --local-config config/development.local.json --confirm-create
```

已有库不能再次执行初始化。配置改变后通过受控停止/启动生效。默认 `APP_MODE=integration`；不要用 Mock 结果证明真实生产成功。

## 外部引擎边界

Factory 控制台自身可独立启动，但真实采集、Prompt、RPA 与视频处理仍通过已有 Adapter 调用外部引擎，并未复制或重写它们：

- `LEGACY_TIKTOK_AUTO_ROOT / LEGACY_TIKTOK_AUTO_PYTHON`：旧采集引擎、真实 Chrome Profile 与旧生产规则。
- `LEGACY_VIDEO_AUTO_ROOT / LEGACY_VIDEO_AUTO_PYTHON`：旧视频/Prompt/RPA 引擎。
- `QUALITY_PYTHON`：已有视频质量分析环境。
- ComfyUI 本地 API、Factory 节点、必要模型、VidBee、影刀与 HubStudio：按真实功能配置；不在源码 ZIP 内。

旧 `TikTok_Auto`、`video_auto`、Chrome Profile、ComfyUI 原工作流与其资产没有被本次清理修改。需要业务运行时先确认登录、权限、模型和费用，不能把空库启动等同于生产通过。

## 验证

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
cd ../frontend
npm run check
```

本次清理与启动证据保存在 `logs/cleanup-20261012/`，其中删除清单只有路径/大小/哈希，不保留被销毁媒体和旧数据库内容。
