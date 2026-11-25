"""Host-specific MCP configuration for Factory, Claude Code, and Codex."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .llm import resolve_cli

NOTION_MCP_URL = "https://mcp.notion.com/mcp"
LOCAL_SERVER_NAME = "pku-sync"
NOTION_SERVER_NAME = "notion"
HOSTS = ("factory", "claude", "codex")

# Hosts spawn the local server from their own cwd, where the venv's
# editable-install .pth may not fire: Python 3.11+ site.py silently skips
# .pth files flagged UF_HIDDEN (FILE_ATTRIBUTE_HIDDEN on Windows), and
# iCloud's Desktop sync keeps re-applying that flag. The launcher script
# puts the project root on sys.path itself, so registration is immune.
LAUNCHER = Path(__file__).resolve().parent.parent / "scripts" / "mcp_launcher.py"
if not LAUNCHER.exists():
    LAUNCHER = Path(__file__).resolve().parent / "mcp_server.py"


def host_integration_catalog(
    python: str | None = None,
    data_dir: Path | None = None,
    *,
    launcher_override: str | None = None,
    data_dir_override: str | None = None,
    remote_url: str | None = None,
    exchange_url: str | None = None,
    campus_key: str = "",
) -> dict:
    """Generate MCP setup snippets (either zero-install Remote HTTP MCP with two-stage pairing or local stdio)."""
    prompt = '请通过 pku-sync MCP 查看我的课程笔记、重点提示与待交作业。'
    tools_summary = [
        {"name": "list_courses", "desc": "列出全部已同步课程、就绪讲次/精编讲次数与待交作业统计", "tier": "open"},
        {"name": "read_lecture", "desc": "一站式读取讲次目录或单讲笔记（优先返回 AI 精编考点讲义与 PDF 导出）", "tier": "open"},
        {"name": "search_course_knowledge", "desc": "跨课程全文检索精编讲义、课堂笔记、公式、作业与课件资料", "tier": "open"},
        {"name": "read_course_material", "desc": "读取整课期末复习总纲，或提取/下载教学网课件、实验压缩包（PDF/ZIP/DOCX/PPTX）", "tier": "open"},
        {"name": "get_assignments_and_grades", "desc": "查看待交作业要求、实时教学网成绩册回执，或拉取在线自测/测验题目", "tier": "open"},
        {"name": "submit_course_work", "desc": "统一预检提交书面作业或保存提交在线自测（强制二次确认）", "tier": "open"},
        {"name": "organize_course_content", "desc": "通过 MCP 触发回放转写、AI 精细整理（refine_lecture / batch_refine）、导出笔记 PDF/ZIP（export_pdf）、课程总结、目录同步及任务进度查询", "tier": "open"},
    ]
    if remote_url:
        rurl = str(remote_url).strip()
        ex_url = str(exchange_url or "").strip()
        clean_campus_key = (campus_key or "").strip()
        parsed_rurl = urlsplit(rurl)
        raw_mcp_key = (parse_qs(parsed_rurl.query).get("key") or [""])[0].strip()
        rpc_endpoint = (
            f"{parsed_rurl.scheme}://{parsed_rurl.netloc}{parsed_rurl.path}"
            if parsed_rurl.scheme and parsed_rurl.netloc
            else rurl.split("?", 1)[0]
        )
        dsh_url = rpc_endpoint if raw_mcp_key else rurl
        dsh_headers: dict[str, str] = {}
        if raw_mcp_key:
            dsh_headers["Authorization"] = f"Bearer {raw_mcp_key}"
        if clean_campus_key:
            dsh_headers["X-PKU-Campus-Key"] = clean_campus_key

        deepseek_dsh_obj: dict = {
            "transport": "streamable-http",
            "url": dsh_url,
        }
        if dsh_headers:
            deepseek_dsh_obj["headers"] = dsh_headers
        deepseek_dsh_http = json.dumps(
            {"mcpServers": {LOCAL_SERVER_NAME: deepseek_dsh_obj}},
            ensure_ascii=False,
            indent=2,
        )

        cordis_lines = [
            "- insert:",
            "    - id: mcp-pku-sync",
            "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:",
            f"        serverName: {LOCAL_SERVER_NAME}",
            "        transport: streamable-http",
            f"        url: {dsh_url}",
        ]
        if raw_mcp_key or clean_campus_key:
            cordis_lines.append("        headers:")
            if raw_mcp_key:
                cordis_lines.append(f"          Authorization: Bearer {raw_mcp_key}")
            if clean_campus_key:
                cordis_lines.append(f"          X-PKU-Campus-Key: {clean_campus_key}")
        deepseek_dsh_cordis = "\n".join(cordis_lines)

        std_server: dict = {'type': 'http', 'url': rurl}
        if clean_campus_key:
            std_server['headers'] = {'X-PKU-Campus-Key': clean_campus_key}
        standard_http = json.dumps(
            {'mcpServers': {LOCAL_SERVER_NAME: std_server}},
            ensure_ascii=False,
            indent=2,
        )
        toml_http = f'[mcp_servers.{LOCAL_SERVER_NAME}]\nurl = {json.dumps(rurl)}'
        if clean_campus_key:
            toml_http += (
                f'\n\n[mcp_servers.{LOCAL_SERVER_NAME}.http_headers]\n'
                f'X-PKU-Campus-Key = {json.dumps(clean_campus_key)}'
            )
        oc_server: dict = {'type': 'remote', 'url': rurl, 'enabled': True}
        if clean_campus_key:
            oc_server['headers'] = {'X-PKU-Campus-Key': clean_campus_key}
        opencode_http = json.dumps(
            {'mcp': {LOCAL_SERVER_NAME: oc_server}},
            ensure_ascii=False,
            indent=2,
        )
        claude_cli = f'claude mcp add --scope user --transport http {LOCAL_SERVER_NAME} "{rurl}"'
        if clean_campus_key:
            claude_cli += f' --header "X-PKU-Campus-Key: {clean_campus_key}"'
        host_prompt = (
            f'请用 HTTP GET（或 curl）仅请求一次 {ex_url}（用完即焚，勿搜网页或重复请求）。'
            '拿到 JSON 后按 agent_instructions 将 pku-sync 合并写入当前客户端的 MCP 配置'
            '（勿覆盖其他 MCP，勿完整回显密钥）；若无法改本地配置则直接用 remote_mcp_url 调 JSON-RPC。'
            '完成后告知配置路径（需重载请提醒）并测 list_courses。'
            if ex_url
            else (
                f'请将我的北大教学网 MCP（pku-sync，地址：{rurl}）合并写入你当前客户端的 MCP 配置'
                '（勿覆盖其他 MCP，勿完整回显密钥），完成后提醒我重载并调用 list_courses 验证连通。'
            )
        )
        homework_prompt = '请通过 pku-sync MCP 查看待交作业要求并帮我提交。'
        review_prompt = '请通过 pku-sync MCP 梳理最新一讲的课堂笔记与考点。'
        codex_desc = '推荐直接运行下方终端命令添加，或将 TOML 片段合并到 ~/.codex/config.toml。'
        if clean_campus_key:
            codex_desc += '如需通过 MCP 触发回放整理与目录同步，请使用带 X-PKU-Campus-Key 的 TOML 配置。'
        hosts = []
        host_specs = [
            (
                'deepseek-dsh',
                'DeepSeek',
                '$DSH_HOME/mcp.json',
                '',
                deepseek_dsh_http,
                host_prompt,
                '写入 $DSH_HOME/mcp.json（DeepSeek Harness），或使用下方 Cordis 配置挂载 @deepseek-ai/dsh-mcp-client；密钥通过 Authorization 请求头传递。',
                deepseek_dsh_cordis,
                'DeepSeek 原生 Agent 插件 · Streamable HTTP',
                'Streamable HTTP · Bearer',
            ),
            (
                'codex',
                'Codex',
                '~/.codex/config.toml',
                f'codex mcp add {LOCAL_SERVER_NAME} --url "{rurl}"',
                toml_http,
                host_prompt,
                codex_desc,
                '',
                '一次性配对码 · 自动发现工具',
                'CLI 命令 · TOML',
            ),
            (
                'claude',
                'Claude Code / Desktop',
                '~/.claude.json',
                claude_cli,
                standard_http,
                host_prompt,
                'Claude Code 推荐直接运行下方终端命令；Claude Desktop 请合并下方 JSON 到 ~/.claude.json（设置 → 开发者 → 编辑配置）。',
                '',
                '一次性配对码 · 自动发现工具',
                'CLI 命令 · 标准 JSON',
            ),
            (
                'antigravity',
                'Cursor / Antigravity',
                '~/.cursor/mcp.json 或 ~/.gemini/config/mcp_config.json',
                '',
                standard_http,
                host_prompt,
                '将下方 mcpServers JSON 合并到 Cursor（~/.cursor/mcp.json）或 Antigravity（~/.gemini/config/mcp_config.json）后重载。',
                '',
                '一次性配对码 · 自动发现工具',
                '标准 JSON (mcpServers)',
            ),
            (
                'oh-my-pi',
                'Oh My Pi',
                '~/.omp/agent/mcp.json',
                '',
                standard_http,
                host_prompt,
                '将下方 mcpServers JSON 合并到 ~/.omp/agent/mcp.json，保存后重启 Oh My Pi。',
                '',
                '一次性配对码 · 自动发现工具',
                '标准 JSON (mcpServers)',
            ),
            (
                'opencode',
                'OpenCode',
                '~/.config/opencode/opencode.json',
                '',
                opencode_http,
                host_prompt,
                'OpenCode 使用顶层 mcp 键与 type: "remote" 结构，请合并到 ~/.config/opencode/opencode.json。',
                '',
                '一次性配对码 · 自动发现工具',
                'OpenCode JSON (mcp.remote)',
            ),
        ]
        for key, name, path, cli, snippet, s_prompt, desc, cordis, badge, format_label in host_specs:
            entry = {
                'id': key,
                'name': name,
                'badge': badge,
                'format_label': format_label,
                'description': desc,
                'cli_command': cli,
                'config_path': path,
                'config_snippet': snippet,
                'sample_prompt': s_prompt,
                'homework_prompt': homework_prompt,
                'review_prompt': review_prompt,
            }
            if cordis:
                entry['cordis_snippet'] = cordis
            hosts.append(entry)
        return {
            'python_path': 'remote-http',
            'launcher_path': rurl,
            'remote_mcp_url': rurl,
            'exchange_url': ex_url,
            'campus_key_embedded': bool(clean_campus_key),
            'sample_prompt': host_prompt,
            'homework_prompt': homework_prompt,
            'review_prompt': review_prompt,
            'tools_summary': tools_summary,
            'hosts': hosts,
        }

    py = _posix_path(python or sys.executable)
    launcher = _posix_path(launcher_override) if launcher_override else LAUNCHER.as_posix()
    if data_dir_override:
        root = _posix_path(data_dir_override)
    else:
        target = (data_dir or Path('data')).expanduser()
        root = (target if target.is_absolute() else target.resolve()).as_posix()
    env = {'DATA_DIR': root}
    server = {'command': py, 'args': [launcher], 'env': env}
    standard = json.dumps({'mcpServers': {LOCAL_SERVER_NAME: server}}, ensure_ascii=False, indent=2)
    toml = ('[mcp_servers.pku-sync]\ncommand = ' + json.dumps(py) + '\nargs = [' +
            json.dumps(launcher) + ']\n\n[mcp_servers.pku-sync.env]\nDATA_DIR = ' + json.dumps(root))
    hosts = []
    for key, name, path, cli, snippet, format_label, desc in [
        (
            'deepseek-dsh',
            'DeepSeek',
            '$DSH_HOME/mcp.json',
            '',
            standard,
            '本地 stdio · JSON',
            '将下方本地 stdio 配置合并到 $DSH_HOME/mcp.json（DeepSeek Harness），保存后重启。',
        ),
        (
            'codex',
            'Codex',
            '~/.codex/config.toml',
            f'codex mcp add pku-sync --env "DATA_DIR={root}" -- "{py}" "{launcher}"',
            toml,
            'CLI 命令 · TOML',
            '推荐直接运行下方终端命令添加，或将 TOML 片段合并到 ~/.codex/config.toml 后重启。',
        ),
        (
            'claude',
            'Claude',
            '~/.claude.json',
            f'claude mcp add --scope user --env "DATA_DIR={root}" pku-sync -- "{py}" "{launcher}"',
            standard,
            'CLI 命令 · 标准 JSON',
            'Claude Code 可直接运行下方终端命令；Claude Desktop 请在「设置 → 开发者 → 编辑配置」中合并下方 JSON。',
        ),
        (
            'antigravity',
            'Antigravity',
            '~/.gemini/config/mcp_config.json',
            '',
            standard,
            '本地 stdio · JSON',
            '将下方本地 stdio 配置合并到 ~/.gemini/config/mcp_config.json，保存后重载。',
        ),
        (
            'oh-my-pi',
            'Oh My Pi',
            '~/.omp/agent/mcp.json',
            '',
            standard,
            '本地 stdio · JSON',
            '将下方本地 stdio 配置合并到 ~/.omp/agent/mcp.json，保存后重启 Oh My Pi。',
        ),
        (
            'opencode',
            'OpenCode',
            '~/.config/opencode/opencode.json',
            '',
            json.dumps({'mcp': {LOCAL_SERVER_NAME: {'type': 'local', 'command': [py, launcher],
                        'environment': env, 'enabled': True}}}, ensure_ascii=False, indent=2),
            '本地 stdio · OpenCode JSON',
            'OpenCode 使用顶层 mcp 键与 type: "local" 结构，请合并到 ~/.config/opencode/opencode.json。',
        ),
    ]:
        hosts.append({
            'id': key,
            'name': name,
            'badge': '本地只读 · MCP',
            'format_label': format_label,
            'description': desc,
            'cli_command': cli,
            'config_path': path,
            'config_snippet': snippet,
            'sample_prompt': prompt,
        })
    return {
        'python_path': py,
        'launcher_path': launcher,
        'sample_prompt': prompt,
        'tools_summary': tools_summary,
        'hosts': hosts,
    }


@dataclass(frozen=True)
class HostCommand:
    argv: tuple[str, ...]
    purpose: str


def executable(host: str) -> str:
    names = {"factory": "droid", "claude": "claude", "codex": "codex"}
    if host not in names:
        raise ValueError(f"unknown MCP host {host!r}; choose: {', '.join(HOSTS)}")
    # Same PATH-only resolution as the LLM CLI backends (llm.resolve_cli):
    # the host CLI must be on PATH, scheduled sessions included.
    found = resolve_cli(names[host])
    if not found:
        raise FileNotFoundError(f"{names[host]} is not installed or not on PATH")
    return found


def _posix_path(value: str) -> str:
    """Forward-slash form of a path argument.

    ``droid mcp add`` (verified live 2026-09-16, 0.218.2) strips backslashes
    out of arguments as escape characters, so registering
    ``E:\\remote_project\\...`` stores a broken
    ``E:remote_project...`` command in ~/.factory/mcp.json. Forward slashes
    are valid Windows path separators for CreateProcess and survive every
    host's argument parsing. A plain replace (not ``Path.as_posix``) is
    deliberate: POSIX ``PurePath`` treats ``\\`` as an ordinary filename
    character and would not normalize a Windows path.
    """
    return value.replace("\\", "/")


def configuration_commands(host: str, python: str | None = None) -> list[HostCommand]:
    """Return commands that add the local and official Notion MCP servers."""
    exe = executable(host)
    py = _posix_path(python or sys.executable)
    local = (py, _posix_path(str(LAUNCHER)))
    if host == "factory":
        return [
            HostCommand((exe, "mcp", "add", LOCAL_SERVER_NAME, *local), "local"),
            HostCommand(
                (exe, "mcp", "add", "--type", "http", NOTION_SERVER_NAME, NOTION_MCP_URL),
                "notion",
            ),
        ]
    if host == "claude":
        return [
            HostCommand(
                (exe, "mcp", "add", "--scope", "user", LOCAL_SERVER_NAME, "--", *local),
                "local",
            ),
            HostCommand(
                (
                    exe,
                    "mcp",
                    "add",
                    "--scope",
                    "user",
                    "--transport",
                    "http",
                    NOTION_SERVER_NAME,
                    NOTION_MCP_URL,
                ),
                "notion",
            ),
        ]
    return [
        HostCommand((exe, "mcp", "add", LOCAL_SERVER_NAME, "--", *local), "local"),
        HostCommand(
            (exe, "mcp", "add", NOTION_SERVER_NAME, "--url", NOTION_MCP_URL),
            "notion",
        ),
    ]


def login_command(host: str) -> tuple[str, ...] | None:
    exe = executable(host)
    if host == "claude":
        return (exe, "mcp", "login", NOTION_SERVER_NAME)
    if host == "codex":
        return (exe, "mcp", "login", NOTION_SERVER_NAME)
    return None  # Factory completes remote-server OAuth from /mcp.


def configure(host: str, python: str | None = None) -> list[str]:
    """Add both servers, tolerating a server that is already configured."""
    messages: list[str] = []
    for command in configuration_commands(host, python):
        result = subprocess.run(
            command.argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or result.stderr).strip()
        if result.returncode == 0:
            messages.append(f"{command.purpose}: added")
            continue
        lower = output.lower()
        if "already" in lower or "exists" in lower or "已存在" in output:
            messages.append(f"{command.purpose}: already configured")
            continue
        raise RuntimeError(
            f"{command.purpose} MCP configuration failed "
            f"(exit {result.returncode}): {output[-400:]}"
        )
    return messages


def authenticate(host: str) -> str:
    """Start the host-owned OAuth flow, or return Factory's one-step guidance."""
    command = login_command(host)
    if command is None:
        return "在 Factory/Droid 中运行 /mcp，选择 notion，然后点击浏览器授权。"
    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError(f"{host} Notion MCP OAuth failed (exit {result.returncode})")
    return f"{host} Notion MCP OAuth 已完成"
