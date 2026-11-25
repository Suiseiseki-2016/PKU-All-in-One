# PKU All in One 使用手册（开源本地 BYOK 版）

本文档介绍如何在本机配置并运行 **PKU All in One** 开源本地版，包括模型配置（BYOK）、教学网同步、课堂录像整理、PDF/Notion 导出以及本地 MCP 接入。

---

## 1. 环境准备

- **Python**：3.11 或以上
- **包管理器**：[`uv`](https://docs.astral.sh/uv/)
- **账号与模型**：
  - 北京大学 IAAA / 教学网账号
  - 以下任意一种 AI 服务组合：
    - **全云端 BYOK**：任意 OpenAI 兼容文本大模型（如 DeepSeek、OpenAI、百炼、硅基流动、智谱 GLM、OpenRouter）+ 语音转写（OpenAI `/audio/transcriptions`、硅基流动 SenseVoice 或阿里云百炼 `qwen3-asr-flash`）
    - **半离线 / 全离线**：本地 Ollama + 本地 `faster-whisper`（无需联网调用外部 AI API）

安装依赖：

```bash
uv sync
# 如需在本地离线运行 faster-whisper 语音转写：
uv sync --extra local-asr
```

---

## 2. 启动 Web 工作台与 BYOK 模型配置

启动本地工作台（仅监听 `127.0.0.1`）：

```bash
uv run pku-sync panel
```

启动后进入右侧主界面的 **「模型配置」**（侧边栏常驻入口，无需先登录任何外部账号）：

### 2.1 文本大模型（LLM）

- **快速预设**：点击「DeepSeek」「阿里云百炼」「OpenAI」「硅基流动」「智谱 GLM」「OpenRouter」「Ollama 本地」即可自动填入推荐 Base URL 与默认模型名。
- **字段说明**：
  - `OPENAI_BASE_URL`：OpenAI 兼容接口地址（例如 `https://api.deepseek.com/v1`）
  - `OPENAI_API_KEY`：对应服务商的 API Key（本地 Ollama 可留空）
  - `NOTES_MODEL`：日常基础笔记、作业要求分析与日常问答模型
  - `REFINE_MODEL`：可选，单独为「精细讲义 / 知识导图 / 课程总结 / 自测出题与批改」指定更强模型；留空则复用 `NOTES_MODEL`
  - `NOTES_STRATEGY`：`topics`（按知识点分段并融合板书图文，推荐）或 `transcript`（逐段整理）

### 2.2 视觉模型（VLM，板书与课件 OCR）

- 勾选启用视觉识图后，默认自动继承文本大模型的 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 与 `NOTES_MODEL`。
- 若文本模型为纯文本（如 `deepseek-chat`），可单独填写 `VISION_BASE_URL`、`VISION_API_KEY` 与 `VISION_MODEL`（例如百炼 `qwen-vl-max-latest` 或 OpenRouter `google/gemini-2.5-flash`）。

### 2.3 语音转写（ASR）

支持三种转写后端，在「模型配置」页切换保存即可即时生效：

1. **OpenAI 兼容接口（`TRANSCRIPTION_BACKEND=openai`）**
   - 填写 `ASR_BASE_URL`、`ASR_API_KEY`、`ASR_MODEL`（默认 `whisper-1`；如使用硅基流动可设为 `https://api.siliconflow.cn/v1` 与 `FunAudioLLM/SenseVoiceSmall`）。
   - `ASR_BASE_URL` / `ASR_API_KEY` 留空时默认回退使用 `OPENAI_BASE_URL` / `OPENAI_API_KEY`。
2. **阿里云百炼（`TRANSCRIPTION_BACKEND=dashscope`）**
   - 填写 `DASHSCOPE_API_KEY` 与 `ASR_MODEL`（默认 `qwen3-asr-flash`），支持最长 120 秒切片直传中文高精度识别。
3. **本地离线 Whisper（`TRANSCRIPTION_BACKEND=local`）**
   - 需先安装 `uv sync --extra local-asr`。
   - 可选配置 `WHISPER_MODEL`（`tiny`/`base`/`small`/`medium`/`large-v3`）、`WHISPER_DEVICE`（`auto`/`cpu`/`cuda`）、`WHISPER_COMPUTE_TYPE`（`auto`/`int8`/`float16`）。

配置完成后，可在各区块点击 **「测试连通性」**（支持未保存前直接以当前表单值试测），确认通过后点击 **「保存模型配置并应用」** 写入本地 `.env`。

---

## 3. 连接教学网与同步课程

1. 在左侧栏进入 **「教学网连接」**（或编辑 `.env` 中的 `PKU_USERNAME` 与 `PKU_PASSWORD`），验证通过后即可拉取本学期课程目录、课件文件、通知公告与作业信息。
2. 客户端内置按账号隔离的**纯内存短 TTL Cookie 缓存**（默认 720 秒，可通过 `PKU_SESSION_CACHE_TTL_SECONDS` 调整）：
   - Cookie 不写入磁盘；
   - 连续同步、下载课件或批量整理多讲时直接复用内存会话，减少重复登录 IAAA；
   - 若服务端返回 401 或跳转回登录页，自动失效缓存并透明重新登录。

---

## 4. 课程学习、整理与导出

在 **「我的课程」** 中打开任意课程：

- **讲次整理**：单讲或批量触发「转写并精细整理」，默认采用零落盘流式提取音轨与板书关键帧，生成「基础笔记 (`notes.md`)」「精细讲义 (`refined_notes.md`)」「逻辑导图」与「自测练习」。
- **课程总结与导出**：汇总全课程讲次与作业，支持阅读器内直接下载 Markdown 或导出带中文字体排版与 Mermaid 图表的 PDF。
- **可选 Notion 同步**：可在 `.env` 中配置内部集成密钥 `NOTION_TOKEN`（或使用自建 OAuth 应用运行 `uv run pku-sync notion login`），将整理后的课程主页与讲次页同步至 Notion。

---

## 5. 本地 MCP 接入 AI 客户端

点击左侧栏 **「AI 接入 (MCP)」**，或运行命令：

```bash
# 直接运行 stdio MCP Server
uv run pku-sync mcp serve

# 自动写入宿主配置（支持 claude / codex 等）
uv run pku-sync mcp configure claude
```

当前内置 7 个 MCP 工具：
- `list_courses`：列出本地课程与已就绪讲次统计
- `read_lecture`：按讲次读取转写笔记与精细讲义
- `search_course_knowledge`：在本地课程库中进行关键词检索
- `read_course_material`：读取本地已同步课件（含 PDF 文本提取）
- `get_assignments_and_grades`：读取作业要求、提交状态与成绩
- `submit_course_work`：辅助准备/预览课程作业提交草稿
- `organize_course_content`：触发本地课程整理或导出
