# PKU All in One

**开源（MIT 协议）、纯本地运行、自带密钥（BYOK）的北京大学（Peking University）教学网学习工作台。**

直连北大教学网（`course.pku.edu.cn` / Blackboard / IAAA），本地同步课程目录、课件资料、作业与通知，支持按需零落盘流式提取课程回放，使用你自己的大模型（LLM）、视觉模型（VLM）和语音转写（OpenAI 兼容 ASR / 阿里云百炼 / 本地 `faster-whisper`）生成板书与课件图文对齐笔记、知识图谱与考前总结，并通过 MCP 接入个人 AI 助手。

> **Peking University (北京大学) Course Workspace (`course.pku.edu.cn`)**
> - **本仓库（开源本地 BYOK 版）**：无第三方账号体系、无中转代理服务器、无会员收费——所有课程数据与 API Key 均仅保存在你的本机目录与 `.env` 中。
> - **免部署在线云端工作台（官方托管版）**：[https://pku.aeoluswu.info](https://pku.aeoluswu.info)（[使用指南](https://pku.aeoluswu.info/guides) · [常见问题 FAQ](https://pku.aeoluswu.info/faq) · [`llms.txt`](https://pku.aeoluswu.info/llms.txt)）

---

## 核心特性

- **零账号开箱即用**：启动面板直接进入「我的课程」工作台，无需注册、登录或激活任何外部平台账号。
- **BYOK 多模型可视配置**：在 Web UI「模型配置」页面一键切换或自定义：
  - **文本大模型（LLM）**：支持 DeepSeek、OpenAI、阿里云百炼、硅基流动、智谱 GLM、OpenRouter、本地 Ollama 等所有 OpenAI 兼容接口；可单独指定「精细整理 / 总结 / 出题」模型（`REFINE_MODEL`）。
  - **视觉识别（VLM）**：可选开启黑板板书与课件关键帧 OCR（空字段自动复用文本 LLM 配置，或独立指定视觉端点与模型）。
  - **语音转写（ASR）**：支持三类后端任意切换——`openai`（OpenAI / 硅基流动等 `/audio/transcriptions` 接口）、`dashscope`（阿里云百炼 `qwen3-asr-flash`）与 `local`（纯离线 `faster-whisper`）。
- **零落盘流式处理**：回放音频与关键帧默认在内存中经 PyAV / OpenCV 流式解码抽取，不保留大体积原始视频文件（可按需开启本地保存）。
- **纯内存 IAAA / Blackboard Cookie 缓存**：按账号隔离的短 TTL（默认 12 分钟）纯内存 Cookie 缓存，不落盘；遇到 401 或跳转登录页时自动清空缓存并透明重登 IAAA，连续同步与批处理更快更稳。
- **高密度学习产物**：支持讲次基础笔记、精细讲义（板书截图 + 课件同屏对齐 + Mermaid 知识导图）、自测练习与批改、考前课程总结、PDF 导出与可选 Notion 同步。
- **开箱可用的本地 MCP Server**：内置 7 个只读/协作 MCP 工具，支持 Claude Code、Codex 等 AI 客户端一键接入你的本地课程库。

---

## 快速开始

### 1. 安装依赖

需要 **Python 3.11+** 与 [`uv`](https://docs.astral.sh/uv/)：

```bash
# 基础运行环境（包含面板、PyAV/OpenCV 媒体处理、OpenAI 兼容请求与 MCP）
uv sync

# 可选：若需要使用纯离线本地 faster-whisper 语音转写
uv sync --extra local-asr
```

### 2. 启动本地 Web 工作台

```bash
uv run pku-sync panel
```

服务默认只绑定本机回环地址（`127.0.0.1:8791` 起自动探测），浏览器打开 `/app` 后可直接在左侧导航栏完成全部配置：

1. **「模型配置」**：选择 DeepSeek / 阿里云百炼 / OpenAI / 硅基流动 / 智谱 / OpenRouter / Ollama 预设或填写自定义 Base URL 与 API Key，可点击「测试连通性」验证 LLM / VLM / ASR，保存后直接写入本地 `.env` 并热更新生效。
2. **「教学网连接」**：填入 10 位北大学号与 IAAA 统一身份认证密码，验证后即可同步当学期课程目录、课件、作业 DDL 与通知。
3. **「我的课程」**：浏览课件、分析作业要求、对单讲或批量回放执行「转写并精细整理」，阅读讲义、知识图谱、自测题与课程总结，并支持导出 Markdown / PDF。
4. **「AI 接入 (MCP)」**：复制配置片段或运行命令，将本机课程空间接入 Claude Code / Codex 等 AI 客户端。

---

## 常用 CLI 命令

除了 Web 工作台，也可以直接通过命令行驱动同步、转写与 MCP：

```bash
uv run pku-sync setup                         # 交互式初始化教学网账号与 MCP 宿主
uv run pku-sync discover                      # 查看账号可见课程与当前学期识别结果
uv run pku-sync sync                          # 同步课程公告、课件、作业与录播索引
uv run pku-sync download                      # 下载已索引的课堂录像（可选 --workers 2）
uv run pku-sync process                       # 转写 + 关键帧 + LLM 生成讲次笔记
uv run pku-sync panel                         # 启动本地 Web 工作台
uv run pku-sync coursetable                   # 读取个人课表（--list-terms / --term）
uv run pku-sync grades                        # 读取教学网成绩明细（--json）
uv run pku-sync submit list --course <课程>   # 列出可提交作业并支持本地预览草稿
uv run pku-sync doctor                        # 一键自检（配置/凭据/依赖/磁盘等）
uv run pku-sync mcp serve                     # 以 stdio 方式启动本地 MCP Server
uv run pku-sync mcp configure claude          # 为 Claude Code / Codex 注册 MCP 服务
```

完整使用说明与环境变量说明见 [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) 与 [`.env.example`](.env.example)。

---

## 目录结构

```text
pku_sync/
  ai_provider.py      # BYOK LLM / VLM / ASR 解析、预设与连通性测试
  auth.py             # IAAA / Blackboard 认证与纯内存按账号隔离 Cookie 缓存
  board_ocr.py        # 关键帧板书与课件视觉识别
  config.py           # 基于 .env 的本地配置模型
  transcription.py    # PyAV 内存音轨提取与 OpenAI / DashScope / faster-whisper 转写
  mcp_server.py       # 7 个本地 MCP 工具服务入口
  panel/              # FastAPI 本地 Web 工作台、BYOK 设置接口与零外部依赖前端 SPA
desktop/              # Tauri 2 桌面壳与 sidecar 打包脚本
tests/                # 自动化测试套件
```

---

## 开发与测试

```bash
uv sync --extra dev
uv run python -m compileall -q pku_sync
uv run python -m pku_sync.panel.bundle_frontend --check
uv run python scripts/check_frontend_js.py
uv run pytest -q
```

---

## License

本项目采用 [MIT License](LICENSE) 开源。
