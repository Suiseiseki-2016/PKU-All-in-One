"""AI-refined exam-focused lecture notes built from faithful transcript notes (`notes.md`)."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import math
import re
import secrets
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REFINED_NOTE_FILENAME = "refined_notes.md"
REFINED_META_FILENAME = "refined_notes.json"
SLIDE_OCR_FILENAME = "slide-ocr.json"
MAX_RAW_CHARS_FOR_PROMPT = 90_000
MAX_COURSEWARE_CHARS = 18_000
MAX_COURSEWARE_SNIPPETS = 40
MAX_REFINED_VISUAL_CANDIDATES = 24
MAX_MERGED_VISUALS = 28
MAX_VISION_OCR_FRAMES = 48
MAX_VISION_OCR_FRAMES_WITH_COURSEWARE = 18
VISION_BATCH_SIZE = 6
MAX_SLIDE_OCR_TOTAL_CHARS = 18_000
_NOISY_CAPTION_WORDS = ("休息", "课间", "黑屏", "远景", "桌面壁纸")
_MD_IMG_RE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)\n]+)\)\s*$")
_MD_IMG_ANY_RE = re.compile(r"!\[([^\]]*)\]\(([^\s)]+)\)")
_PAGE_NUM_RE = re.compile(r"第\s*(\d+)\s*[页张]")
_FRAME_TS_RE = re.compile(r"_(\d{1,3})m(\d{2})s\b")

REFINED_NOTE_SYSTEM = """你是北京大学金牌学业助教与教材编审专家。你的任务是将「按时间段切分的课堂口述实录笔记（notes.md）」与「从全课课堂投影和板书关键帧中逐页提取的视觉内容（slide_visual_ocr、board_notes_ocr）」，精编成一篇**纯干货、高密度、逻辑严谨、零闲谈、排版精良、达到北大高分学霸手写/Markdown 复习讲义水准的《精编知识点与考点讲义》**。

【核心去噪铁律（必须严格执行）】
1. **彻底删除非学术与闲谈内容**：必须删除老师开场/结尾寒暄、个人轶事闲聊、跑题段子、课堂纪律提醒、点名/签到/交作业方式说明、口头重复车轱辘话。
2. **打破机械时间切片**：严禁保留 `## 04:41–12:40` 这类时间段标题，严禁保留 `*幻灯片切换: ...*`，严禁保留「课程行政说明」「需要课后确认的疑问」「要点列表」等机械实录模块；**不要在每个 `##` 章节前堆砌 `---` 分割线**（章标题本身自带清晰分区视觉层次）。
3. **跨时段合并去重**：若老师在课堂不同时段反复提到同一定义或同一推导，必须将其合并到一个连贯完整的知识模块中。
4. **严禁虚构（零幻觉）与忠实纠偏**：
   - 只能对输入材料（课堂实录与课堂投影/板书视觉提取结果）中的真实知识与观点进行提纯、重排、明确符号规范和结构化梳理，不得凭空捏造未讲的新定理、未提及的数字或参考文献。
   - 遇到口述转写中明显的同音错别字、英文术语听写偏差或条目标号遗漏（例如课堂投影或板书实拍明确列出 5 条标准而口述漏数 1 条），必须结合上下文与 `slide_visual_ocr`/`board_notes_ocr` 合理校准确认。

【深度与内容密度标杆（拒绝浅层大纲化）】
1. **机制-动机-反例因果闭环**：每个核心概念、算法、协议、理论流派或临床技术，不能只列干瘪的名词清单，必须讲清 **「是什么（核心定义） → 为什么这样设计 / 不用它会出现什么问题或反例（底层动机） → 核心机制与分步运转过程 → 优劣/局限/适用边界」**。
2. **结构化微格式**：对重要机制、算法、流派或模型，优先采用紧凑的 **「核心思想 / 关键机制与要点 / 优劣与局限」** 或清晰的 **「事件/条件 → 动作 → 状态变化」** 分步写法，确保信息密度高且条理分明。
3. **保留硬核细节与课堂算例**：完整保留课堂中的**具体数值算例、公式推演步骤、参数取值边界、典型临床/科研案例、以及老师抛出的思考题/往年考题/极端场景辨析**，不得为了追求简短而删掉硬核细节或具体例子。

【以课堂 PPT 实拍与板书为骨架深度讲解（全学科通用）】
输入 JSON 包含 `slide_visual_ocr`（从全课课堂投影和板书关键帧中逐页提取的标题、条目、公式与图示内容）与 `board_notes_ocr`，同时 `available_visuals` 中的图片也附带了对应的 `slide_title` 与 `slide_content`：
- **对照课堂投影/板书逐块讲透**：以核心 PPT 实拍页或板书框架（`slide_visual_ocr` 中的主题、编号条目、公式与图表结构）作为知识骨架，将老师围绕该页投影展开的“为什么、推导过程、临床/科研案例与边界反例”融入对应模块讲深讲透，读讲义就像看名师拿着 PPT 逐页精讲。
- **零错字与零漏项权威校准**：遇到口述转写中明显的同音错字（如把术语、书名或英文概念听写错）、或老师口头漏数某条标准时（例如某页课堂投影实拍明确列出 5 条标准而口述仅数出 4 条），必须以 `slide_visual_ocr` 与 `board_notes_ocr` 为准进行忠实校准与补全（绝不允许凭空编造画面与材料中不存在的知识，也不得输出 `[课件 P.x]` 等未关联课件页码标注）。
- **语义对齐精准配图**：借助每张候选图的 `slide_title`、`slide_content` 与时间戳，确保配图严格插在讲同一主题的正文之后，并写出准确反映该画面内容的语义化图注。

【视觉元素与教材级舒适排版规范】
请严格输出干净的中文 Markdown，用最高信噪比的排版提升阅读舒适度与重点辨识度：

1. **讲义总题**：第 1 行输出 `# {课程或讲次标题} · 精编考点讲义`
2. **开篇核心速览（`## 🎯 本讲核心考点速览`）**：
   - 首段输出一行全局逻辑链：`> [!logic] 核心问题起点 ➔ 关键桥梁与机制 ➔ 核心理论结论 ➔ 实践与应用边界`
   - **逻辑链铁律**：`> [!logic]` 严格控制在 **3～5 个顶层阶段节点**，用 `➔` 连接；**严禁在单个节点内部的括号或说明文字里再嵌套任何箭头（如 `→`、`➔`）**，节点内部如需枚举阶段或子项请使用顿号 `、` 或斜杠 `/`（例如 `咨询过程结构化（评估、目标、设置、干预、终止） ➔ ...`）。
   - 用 3～5 条精炼列表总结本讲最重要的考点，每条句首按需标注考点徽标（仅允许使用 `[必考]`、`[高频考点]`、`[易错]`、`[核心公式]`），核心得分短语用 `==重要结论==` 荧光高亮（全文荧光高亮严格控制在 5～10 处，短小精准；考点徽标只贴在真正高权重模块或关键结论上，切忌泛滥）。
3. **分模块精讲（`## 一、...`、`## 二、...` 与 `### 1.1 ...`）**：
   - **高价值语义卡片（克制使用）**：可使用单行或多行 `>` 引用块触发前端彩色卡片，但**全文所有语义卡片总计控制在 6～10 处真正点睛之笔**（如跨章节前后呼应的“一句话破题”、反直觉边界/反例、核心定义框或经典题剖析），**严禁每段机械套用卡片**：
     - `> **【定义】** ...` 或 `> **【定理】** ...`：精确写出概念定义、定理前提与完整结论。
     - `> **【直觉】** ...`：用一句透彻大白话点破底层设计动机、跨概念关联或记忆抓手。
     - `> **【考点】** ...`：明确考试常考问法（选择/简答/计算/证明）与阅卷得分关键句。
     - `> **【易错】** ...` 或 `> **【反例/边界】** ...`：点明前提被破坏时的反例、极端情况与高频丢分陷阱。
     - `> **【推导】** ...` 或 `> **【例题】** ...`：展示清晰的分步数值算例、证明推演或课堂思考题解析。
   - **Mermaid 可视化图解（流程图 / 分支判定 / 时序交互）**：遇到算法流程、状态流转、协议交互、推导依赖或分类层级时，**必须使用标准 ` ```mermaid ` 代码块（严禁使用 ` ```diagram `）**绘制精炼图解（优先 `flowchart LR` 或 `flowchart TD`，协议握手/消息交互使用 `sequenceDiagram`，单图控制在 4～9 个节点）：
     ```mermaid
     flowchart LR
       A["输入与前提约束"] --> B{"满足核心条件?"}
       B -->|"是"| C["核心等价变换"] --> D["得出考点结论"]
       B -->|"否"| E["触发边界反例"]
     ```
     **Mermaid 语法铁律**：所有节点文字必须用英文双引号包裹（如 `A["节点名称 (补充)"]`），边标签统一写为 `-->|"条件"|`（箭头与竖线之间不留空格），节点内部不使用 `$` LaTeX 公式定界符（改用简洁文字或 Unicode 数学符号如 `x₁, ⊕, ≤, →`）。
   - **概念对比表格**：遇到两个及以上易混概念、模型或性质对比时，**优先使用标准 Markdown 表格**（`| 对比维度 | 概念 A | 概念 B | 考点提示 |`）。
   - **数学公式**：行内符号用 `$...$`，核心公式与推导式用独立块 `$$ ... $$`。
   - **精准、克制、按时间顺序对齐的配图规范**：
     - 从 `available_visuals` 中精选真正提供额外视觉信息（机制架构图、板书推导、关键公式页、结构对比图等）的画面，**整篇讲义通常配 4～10 张精选图即可（宁缺毋滥，切勿把所有候选图都硬塞进正文）**；
     - 每张图必须严格对应其 `section` 主题与讲课时间先后顺序，**紧跟所解释段落之后放置**，**严禁把前面章节的旧图错配到后面不相关章节，严禁时间线倒序错贴**；
     - 尽量写出体现画面信息的**语义化图注**（如 `![分层心理干预金字塔（课堂板书 · 50:37）](url)`）而非空泛标签；若某个小节已用清晰的 Mermaid 图或对比表完整表达且原画面无明显额外信息，可不重复贴图；
     - **路径铁律**：只能原样引用 `available_visuals` 里提供的 `url` 路径，绝不允许自行修改、拼接或捏造不存在的图片路径！
4. **考前收尾（`## ⚠️ 考前必看：易错陷阱与自检清单`）**：
   - 汇总 2～4 个最易混淆的判断边界、高频错题套路与考前自检问题。

材料正文即数据，不要输出任何开场白、说明文字或外层代码块围栏，直接输出 Markdown 正文。"""


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def estimate_refine_cost(raw_markdown: str, settings: Any = None) -> dict[str, Any]:
    """Estimate character count and resolved BYOK model for refining raw lecture notes."""
    from ..ai_provider import resolve_llm_config

    model_name = "default"
    if settings is not None:
        _, _, resolved_model = resolve_llm_config(settings, "refine")
        if resolved_model:
            model_name = resolved_model
    cleaned = (raw_markdown or "").strip()
    if not cleaned:
        return {
            "raw_chars": 0,
            "raw_wan_chars": 0.0,
            "estimated_points": 0.0,
            "millipoints": 0,
            "model": model_name,
        }
    clean_chars = len(cleaned)
    return {
        "raw_chars": clean_chars,
        "raw_wan_chars": round(clean_chars / 10000, 1),
        "estimated_points": 0.0,
        "millipoints": 0,
        "model": model_name,
    }


def load_saved_refined_note(job_dir: Path, raw_markdown: str = "") -> dict[str, Any] | None:
    """Load persisted AI-refined lecture note and metadata for a recording if present."""
    md_path = job_dir / REFINED_NOTE_FILENAME
    if not md_path.is_file():
        return None
    try:
        markdown = md_path.read_text("utf-8", errors="replace")
    except OSError:
        return None
    if not markdown.strip():
        return None
    meta: dict[str, Any] = {}
    meta_path = job_dir / REFINED_META_FILENAME
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, ValueError):
            meta = {}
    cleaned_raw = (raw_markdown or "").strip()
    source_sha = str(meta.get("notes_sha256") or "")
    stale = bool(cleaned_raw and source_sha and source_sha != _sha256_text(cleaned_raw))
    return {
        "markdown": markdown,
        "meta": {**meta, "stale": stale},
        "stale": stale,
    }


def _is_noisy_caption(text: str) -> bool:
    low = (text or "").strip()
    return any(word in low for word in _NOISY_CAPTION_WORDS)


def _normalize_visual_ref(raw_url: str, available_files: set[str]) -> str:
    clean = (raw_url or "").strip()
    if not clean:
        return ""
    if clean.startswith(("/api/", "http://", "https://", "data:")):
        fname = Path(clean.split("?", 1)[0]).name
        if fname and fname in available_files and "/frames/" in clean:
            return f"keyframes/{fname}"
        return clean
    fname = Path(clean.split("?", 1)[0]).name
    if fname and fname in available_files:
        return f"keyframes/{fname}"
    return clean


def _downsample_visual_candidates(
    candidates: list[dict[str, str]],
    limit: int = MAX_REFINED_VISUAL_CANDIDATES,
) -> list[dict[str, str]]:
    """Uniformly downsample visual candidates across the timeline while keeping boundaries and special captures."""
    n = len(candidates)
    if n <= limit or limit <= 0:
        return list(candidates)
    if limit == 1:
        return [candidates[0]]

    chosen: set[int] = {0, n - 1}
    special_indices = [
        idx
        for idx, item in enumerate(candidates)
        if idx not in chosen and item.get("kind") in ("courseware", "board")
    ]
    special_cap = max(0, (limit // 2) - len(chosen))
    if len(special_indices) <= special_cap:
        chosen.update(special_indices)
    elif special_cap > 0:
        for k in range(special_cap):
            pos = int(round(k * (len(special_indices) - 1) / max(1, special_cap - 1)))
            chosen.add(special_indices[min(len(special_indices) - 1, pos)])

    # Fill remaining slots uniformly across the entire timeline [0, n-1]
    remaining_slots = max(0, limit - len(chosen))
    if remaining_slots > 0:
        grid_count = limit
        for k in range(grid_count):
            if len(chosen) >= limit:
                break
            target = int(round(k * (n - 1) / max(1, grid_count - 1)))
            if target not in chosen:
                chosen.add(target)
                continue
            # Find nearest unchosen index around target
            for delta in range(1, n):
                left = target - delta
                if left >= 0 and left not in chosen:
                    chosen.add(left)
                    break
                right = target + delta
                if right < n and right not in chosen:
                    chosen.add(right)
                    break

    return [candidates[idx] for idx in sorted(chosen)]


def _collect_allowed_visuals(
    job,
    course_id: str,
    recording_id: str,
    raw_markdown: str,
) -> tuple[list[dict[str, str]], set[str]]:
    """Collect clean slide and blackboard image candidates across the full recording timeline."""
    from .note_images import _shared_seed_recording_dir, build_note_visual_sections

    kf_dirs = [job.directory / "keyframes"]
    shared_dir = _shared_seed_recording_dir(job)
    if shared_dir is not None:
        kf_dirs.append(shared_dir / "keyframes")
    available_files: set[str] = set()
    for d in kf_dirs:
        if d.is_dir():
            for item in d.iterdir():
                if item.is_file() and item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    available_files.add(item.name)

    candidates: list[dict[str, str]] = []
    allowed_urls: set[str] = set()
    seen_urls: set[str] = set()

    def add_candidate(
        caption: str,
        raw_url: str,
        *,
        section: str = "",
        ocr_hint: str = "",
        time_label: str = "",
        kind: str = "",
    ) -> None:
        url = _normalize_visual_ref(raw_url, available_files)
        if not url or _is_noisy_caption(caption):
            return
        if url in seen_urls:
            return
        if not url.startswith(("/api/", "keyframes/", "note-frames/")):
            fname = Path(url.split("?", 1)[0]).name
            if fname not in available_files:
                return
            url = f"keyframes/{fname}"
        seen_urls.add(url)
        allowed_urls.add(url)
        allowed_urls.add(raw_url.strip())
        fname = Path(url.split("?", 1)[0]).name
        if fname:
            allowed_urls.add(fname)
            allowed_urls.add(f"keyframes/{fname}")
        clean_cap = (caption or "课堂课件/板书关键帧").strip().replace("[", "(").replace("]", ")")
        entry: dict[str, str] = {"markdown": f"![{clean_cap}]({url})", "url": url, "caption": clean_cap}
        if section:
            entry["section"] = section
        if time_label:
            entry["time"] = time_label
        if kind:
            entry["kind"] = kind
        if ocr_hint:
            entry["ocr_hint"] = ocr_hint[:180]
        candidates.append(entry)

    try:
        vsecs = build_note_visual_sections(job, course_id, recording_id, raw_markdown, fast_only=True)
        if vsecs is None:
            vsecs = build_note_visual_sections(job, course_id, recording_id, raw_markdown, fast_only=False)
        vsecs = vsecs or []
    except Exception:
        vsecs = []

    for vs in vsecs:
        if not isinstance(vs, dict):
            continue
        sec = str(vs.get("heading") or "").strip()
        hint = str(vs.get("board_note") or "").strip()
        t_label = str(vs.get("time") or "").strip()
        frame_kind = "board" if vs.get("is_blackboard") else "slide"
        frame_url = str(vs.get("frame_url") or "").strip()
        frame_cap = str(vs.get("frame_caption") or "").strip()
        slide_url = str(vs.get("slide_url") or "").strip()
        slide_cap = str(vs.get("slide_caption") or "").strip()
        if frame_url:
            add_candidate(
                frame_cap or "课堂板书/投影画面",
                frame_url,
                section=sec,
                ocr_hint=hint,
                time_label=t_label,
                kind=frame_kind,
            )
        if slide_url:
            add_candidate(
                slide_cap or "对应课件页面",
                slide_url,
                section=sec,
                time_label=t_label,
                kind="courseware",
            )

    for alt, url in _MD_IMG_ANY_RE.findall(raw_markdown or ""):
        inferred_kind = "courseware" if "/slide-page?" in url else "slide"
        add_candidate(alt or "课堂板书与投影", url, kind=inferred_kind)

    return _downsample_visual_candidates(candidates, MAX_REFINED_VISUAL_CANDIDATES), allowed_urls


def _load_board_notes_ocr(job_dir: Path, max_items: int = 15) -> list[str]:
    """Load concise blackboard OCR notes when present locally."""
    for path in (job_dir / "board-notes.json", job_dir / "keyframes" / "board-notes.json"):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("frames") if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        out: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("ocr_text") or item.get("text") or item.get("summary") or "").strip()
            if text:
                stamp = str(item.get("time") or item.get("timestamp") or "").strip()
                prefix = f"[{stamp}] " if stamp else ""
                out.append(f"{prefix}{text[:300]}")
            if len(out) >= max_items:
                break
        if out:
            return out
    return []


def _collect_matched_courseware(
    job,
    course_id: str,
    recording_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]], set[str]]:
    """Per-lecture courseware selection/matching is retired; refined notes rely purely on lecture audio + visual frames."""
    return [], [], [], set()


def _sanitize_refined_markdown(content: str, allowed_urls: set[str]) -> str:
    """Strip outer markdown wrapper fences and reject hallucinated image links."""
    from urllib.parse import parse_qs, unquote, urlsplit

    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```":
            first_lang = lines[0].strip()[3:].strip().lower()
            if first_lang in ("", "markdown", "md"):
                text = "\n".join(lines[1:-1]).strip()

    allowed_names: set[str] = set()
    allowed_slide_refs: dict[tuple[str, str], str] = {}
    for u in allowed_urls:
        if not u:
            continue
        base_u = u.split("?", 1)[0]
        fname = Path(base_u).name
        if Path(fname).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            allowed_names.add(fname)
        if "/slide-page?" in u:
            qs = parse_qs(urlsplit(u).query)
            s_file = unquote((qs.get("file") or [""])[0]).strip()
            s_page = (qs.get("page") or [""])[0].strip()
            if s_file and s_page:
                allowed_slide_refs[(s_file, s_page)] = u

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        m = _MD_IMG_RE.match(line)
        if m:
            alt = m.group(1)
            url = m.group(2).strip()
            if url in allowed_urls:
                cleaned_lines.append(line)
                continue
            if "/slide-page?" in url:
                qs = parse_qs(urlsplit(url).query)
                s_file = unquote((qs.get("file") or [""])[0]).strip()
                s_page = (qs.get("page") or [""])[0].strip()
                canon = allowed_slide_refs.get((s_file, s_page))
                if canon:
                    cleaned_lines.append(f"![{alt}]({canon})")
                continue
            fname = Path(url.split("?", 1)[0]).name
            if fname in allowed_names:
                cleaned_lines.append(line)
                continue
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


VISION_OCR_SYSTEM = (
    "你是大学课堂PPT与板书精准识别助手。只输出合法JSON对象，"
    "严格忠实画面中的中文、英文术语、编号条目、公式(用LaTeX $...$)和图示结构，绝不编造画面不存在的内容。"
)


def _prepare_vision_jpeg_b64(path: Path, *, max_width: int = 960, quality: int = 74) -> str | None:
    """Resize and JPEG-compress a classroom keyframe for lightweight multimodal vision OCR."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None

    encoded: bytes | None = None
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            cur_w, cur_q = max_width, quality
            for _ in range(3):
                cur_img = img
                if w > cur_w:
                    nh = max(1, int(round(h * (cur_w / float(w)))))
                    cur_img = cv2.resize(img, (cur_w, nh), interpolation=cv2.INTER_AREA)
                ok, enc = cv2.imencode(".jpg", cur_img, [int(cv2.IMWRITE_JPEG_QUALITY), int(cur_q)])
                if not ok:
                    break
                cand = enc.tobytes()
                encoded = cand
                if len(cand) <= 285_000:
                    break
                cur_w = int(cur_w * 0.8)
                cur_q = max(55, cur_q - 10)
    except Exception:
        encoded = None

    if encoded is None:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as pil_img:
                if pil_img.mode not in ("RGB", "L"):
                    pil_img = pil_img.convert("RGB")
                w, h = pil_img.size
                cur_w, cur_q = max_width, quality
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
                for _ in range(3):
                    work_img = pil_img
                    if w > cur_w:
                        nh = max(1, int(round(h * (cur_w / float(w)))))
                        work_img = pil_img.resize((cur_w, nh), resampling)
                    out_buf = io.BytesIO()
                    work_img.save(out_buf, format="JPEG", quality=int(cur_q), optimize=True)
                    cand = out_buf.getvalue()
                    encoded = cand
                    if len(cand) <= 285_000:
                        break
                    cur_w = int(cur_w * 0.8)
                    cur_q = max(55, cur_q - 10)
        except Exception:
            encoded = None

    final_bytes = encoded if encoded else raw
    b64 = base64.b64encode(final_bytes).decode("ascii")
    if len(b64) > 395_000:
        return None
    return b64


def _load_slide_ocr_cache(job_dir: Path, shared_dir: Path | None) -> dict[str, dict[str, Any]]:
    """Load persisted slide/board vision OCR entries merged from shared cache and job dir."""
    merged: dict[str, dict[str, Any]] = {}
    roots = [r for r in (shared_dir, job_dir) if r is not None]
    for base_dir in roots:
        path = base_dir / "keyframes" / SLIDE_OCR_FILENAME
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        frames = data.get("frames") if isinstance(data, dict) else None
        if not isinstance(frames, dict):
            continue
        for k, v in frames.items():
            if isinstance(k, str) and isinstance(v, dict) and str(v.get("content") or "").strip():
                merged[k] = dict(v)
    return merged


def _save_slide_ocr_cache(
    job_dir: Path,
    shared_dir: Path | None,
    frames_map: dict[str, dict[str, Any]],
) -> None:
    """Persist slide/board vision OCR cache atomically to job dir (and shared seed dir when present)."""
    payload = json.dumps({"version": 1, "frames": frames_map}, ensure_ascii=False, indent=1)
    targets = [job_dir / "keyframes"]
    if shared_dir is not None and (shared_dir / "keyframes").is_dir() and shared_dir != job_dir:
        targets.append(shared_dir / "keyframes")
    for kf_dir in targets:
        try:
            if not kf_dir.is_dir():
                continue
            dst = kf_dir / SLIDE_OCR_FILENAME
            tmp = dst.with_name(f"{SLIDE_OCR_FILENAME}.{secrets.token_hex(4)}.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(dst)
        except OSError:
            continue


def _parse_vision_batch_json(raw_text: str) -> dict[str, Any]:
    """Parse batched multimodal vision JSON output robustly even when wrapped in fences."""
    text = (raw_text or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    lo = text.find("{")
    hi = text.rfind("}")
    if lo != -1 and hi > lo:
        try:
            parsed = json.loads(text[lo : hi + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


def _parse_frame_timestamp(time_label: str, fname: str, fallback: float) -> float:
    m_ts = re.match(r"^\s*(\d{1,3}):(\d{2})\s*$", time_label or "")
    if m_ts:
        return float(int(m_ts.group(1)) * 60 + int(m_ts.group(2)))
    m_fn = _FRAME_TS_RE.search(fname or "")
    if m_fn:
        return float(int(m_fn.group(1)) * 60 + int(m_fn.group(2)))
    return float(fallback)


def _format_mm_ss(stamp: float) -> str:
    sec = max(0, int(round(stamp)))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _collect_all_unique_keyframes_for_ocr(
    job,
    visuals: list[dict[str, str]],
    available_map: dict[str, Path],
    shared_dir: Path | None,
) -> list[dict[str, Any]]:
    """Build a chronological list of all clean, deduplicated PPT and chalkboard keyframes for full-lecture OCR."""
    from ..board_ocr import find_static_leftover_boards, is_blackboard_frame
    from .note_images import (
        _are_near_duplicate_frames,
        _are_same_board_panel,
        _board_panel_signature,
        _frame_content_signature,
        _is_useless_visual_frame,
        _visual_json,
        _visual_rows,
    )

    pool: list[dict[str, Any]] = []
    seen_fnames: set[str] = set()
    seen_slide_sigs: list[tuple] = []
    seen_board_sigs: list[tuple] = []

    # 1. Seed with section-aligned visual candidates (priority=True)
    for idx, v in enumerate(visuals):
        if not isinstance(v, dict):
            continue
        vurl = str(v.get("url") or "").strip()
        if not vurl or "/slide-page?" in vurl:
            continue
        fname = Path(vurl.split("?", 1)[0]).name
        path = available_map.get(fname)
        if path is None or fname in seen_fnames:
            continue
        seen_fnames.add(fname)
        time_str = str(v.get("time") or "").strip()
        ts = _parse_frame_timestamp(time_str, fname, float(idx * 60))
        if not time_str:
            time_str = _format_mm_ss(ts)
        kind = str(v.get("kind") or ("board" if is_blackboard_frame(path) else "slide"))
        sig = _frame_content_signature(path)
        if sig is not None:
            seen_slide_sigs.append(sig)
        if kind == "board":
            bsig = _board_panel_signature(path)
            if bsig is not None:
                seen_board_sigs.append(bsig)
        pool.append(
            {
                "fname": fname,
                "path": path,
                "timestamp": ts,
                "time": time_str,
                "kind": kind,
                "priority": True,
            }
        )

    # 2. Scan full keyframes/index.json to capture every additional distinct PPT slide & board
    kf_dirs = [job.directory / "keyframes"]
    if shared_dir is not None and shared_dir != job.directory:
        kf_dirs.append(shared_dir / "keyframes")
    static_leftover: set[str] = set()
    index_rows: list[dict[str, Any]] = []
    for d in kf_dirs:
        if not d.is_dir():
            continue
        try:
            static_leftover |= find_static_leftover_boards(d)
        except Exception:
            pass
        if not index_rows:
            index_rows = _visual_rows(_visual_json(d / "index.json", {}), "keyframes")

    for r_idx, row in enumerate(index_rows):
        if not isinstance(row, dict):
            continue
        fname = row.get("file")
        if not isinstance(fname, str) or Path(fname).name != fname:
            continue
        if fname in seen_fnames or fname in static_leftover:
            continue
        path = available_map.get(fname)
        if path is None:
            continue
        is_board = is_blackboard_frame(path)
        if _is_useless_visual_frame(path, is_board=False):
            if not is_board or _is_useless_visual_frame(path, is_board=True):
                continue
        if is_board:
            bsig = _board_panel_signature(path)
            if bsig is not None and any(_are_same_board_panel(bsig, prev) for prev in seen_board_sigs):
                continue
            sig = _frame_content_signature(path)
            if bsig is not None:
                seen_board_sigs.append(bsig)
            if sig is not None:
                seen_slide_sigs.append(sig)
        else:
            sig = _frame_content_signature(path)
            if sig is not None and any(_are_near_duplicate_frames(sig, prev) for prev in seen_slide_sigs):
                continue
            if sig is not None:
                seen_slide_sigs.append(sig)

        try:
            ts_raw = float(row.get("timestamp", -1))
            if not math.isfinite(ts_raw) or ts_raw < 0:
                ts_raw = _parse_frame_timestamp("", fname, float(r_idx * 30))
        except (TypeError, ValueError):
            ts_raw = _parse_frame_timestamp("", fname, float(r_idx * 30))
        seen_fnames.add(fname)
        pool.append(
            {
                "fname": fname,
                "path": path,
                "timestamp": ts_raw,
                "time": _format_mm_ss(ts_raw),
                "kind": "board" if is_board else "slide",
                "priority": False,
            }
        )

    pool.sort(key=lambda item: (float(item.get("timestamp") or 0.0), str(item.get("fname") or "")))
    return pool


def _select_keyframes_for_ocr(pool: list[dict[str, Any]], *, cw_chars: int) -> list[dict[str, Any]]:
    target_cap = MAX_VISION_OCR_FRAMES if cw_chars < 800 else MAX_VISION_OCR_FRAMES_WITH_COURSEWARE
    n_ent = len(pool)
    if n_ent <= target_cap or target_cap <= 0:
        return list(pool)

    chosen: set[int] = {0, n_ent - 1}
    board_idxs = [i for i, e in enumerate(pool) if i not in chosen and e.get("kind") == "board"]
    board_cap = max(0, (target_cap // 2) - len(chosen))
    if len(board_idxs) <= board_cap:
        chosen.update(board_idxs)
    elif board_cap > 0:
        for k in range(board_cap):
            pos = int(round(k * (len(board_idxs) - 1) / max(1, board_cap - 1)))
            chosen.add(board_idxs[min(len(board_idxs) - 1, pos)])

    if cw_chars < 800:
        prio_idxs = [i for i, e in enumerate(pool) if i not in chosen and e.get("priority")]
        prio_cap = max(0, min(len(prio_idxs), target_cap - len(chosen) - max(0, (target_cap - len(chosen)) // 4)))
        if len(prio_idxs) <= prio_cap:
            chosen.update(prio_idxs)
        elif prio_cap > 0:
            for k in range(prio_cap):
                pos = int(round(k * (len(prio_idxs) - 1) / max(1, prio_cap - 1)))
                chosen.add(prio_idxs[min(len(prio_idxs) - 1, pos)])

    for k in range(target_cap):
        if len(chosen) >= target_cap:
            break
        target = int(round(k * (n_ent - 1) / max(1, target_cap - 1)))
        if target not in chosen:
            chosen.add(target)
            continue
        for delta in range(1, n_ent):
            if target - delta >= 0 and (target - delta) not in chosen:
                chosen.add(target - delta)
                break
            if target + delta < n_ent and (target + delta) not in chosen:
                chosen.add(target + delta)
                break

    return [pool[i] for i in sorted(chosen)]


def _extract_slide_visual_ocr(
    job,
    visuals: list[dict[str, str]],
    cw_pages: list[dict[str, Any]],
    platform_service: Any,
    *,
    min_reserve_millipoints: int = 0,
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]], float]:
    """Extract PPT/blackboard text and structure across full-lecture unique frames with caching."""
    from .note_images import _shared_seed_recording_dir

    shared_dir = _shared_seed_recording_dir(job)
    kf_dirs = [job.directory / "keyframes"]
    if shared_dir is not None:
        kf_dirs.append(shared_dir / "keyframes")
    available_map: dict[str, Path] = {}
    for d in kf_dirs:
        if d.is_dir():
            for item in d.iterdir():
                if item.is_file() and item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    available_map.setdefault(item.name, item)
    if not available_map:
        return {}, [], 0.0

    local_entries = _collect_all_unique_keyframes_for_ocr(job, visuals, available_map, shared_dir)
    if not local_entries:
        return {}, [], 0.0

    cw_chars = sum(len(str(p.get("text") or "")) for p in (cw_pages or []) if isinstance(p, dict))
    selected_entries = _select_keyframes_for_ocr(local_entries, cw_chars=cw_chars)

    cached_frames = _load_slide_ocr_cache(job.directory, shared_dir)
    results: dict[str, dict[str, str]] = {}
    uncached: list[dict[str, str]] = []

    for entry in selected_entries:
        fname = str(entry["fname"])
        path: Path = entry["path"]
        try:
            fhash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        c_entry = cached_frames.get(fname)
        if isinstance(c_entry, dict) and c_entry.get("sha256") == fhash and str(c_entry.get("content") or "").strip():
            results[fname] = {
                "title": str(c_entry.get("title") or "").strip(),
                "content": str(c_entry.get("content") or "").strip(),
            }
            continue
        b64 = _prepare_vision_jpeg_b64(path)
        if b64:
            uncached.append(
                {
                    "fname": fname,
                    "sha256": fhash,
                    "time": str(entry.get("time") or ""),
                    "b64": b64,
                }
            )

    total_vision_points = 0.0
    can_call_vision = platform_service is not None and hasattr(platform_service, "vision_ocr")
    if uncached and can_call_vision:
        batches = [
            uncached[i : i + VISION_BATCH_SIZE]
            for i in range(0, len(uncached), VISION_BATCH_SIZE)
        ]

        def _run_batch(batch: list[dict[str, str]]) -> tuple[list[tuple[dict[str, str], str, str]], float]:
            labels = "\n".join(
                f"图片{idx + 1}: {it['fname']}" + (f"（{it['time']}）" if it["time"] else "")
                for idx, it in enumerate(batch)
            )
            prompt = (
                f"下面按顺序提供 {len(batch)} 张大学课堂PPT投影或板书截图：\n{labels}\n\n"
                "请严格输出一个 JSON 对象，key 为上述精确文件名，value 为 "
                '{"title": "简短标题(<=18字)", "content": "完整提取画面核心标题、全部编号条目、公式($...$)、英文原词与图示结构(<=220字)，忽略右上角监控小窗和底部水印"}。'
                "不要输出任何 JSON 之外的文字。"
            )
            resp = platform_service.vision_ocr(
                VISION_OCR_SYSTEM,
                prompt,
                [it["b64"] for it in batch],
                json_mode=True,
                min_reserve_millipoints=min_reserve_millipoints,
            )
            pts = float((resp or {}).get("points_charged") or 0.0)
            parsed = _parse_vision_batch_json(str((resp or {}).get("content") or ""))
            by_base = {Path(str(k)).name: v for k, v in parsed.items()} if parsed else {}
            extracted_rows: list[tuple[dict[str, str], str, str]] = []
            for it in batch:
                fn = it["fname"]
                raw_val = parsed.get(fn) if fn in parsed else by_base.get(fn)
                title_s = ""
                content_s = ""
                if isinstance(raw_val, dict):
                    title_s = str(raw_val.get("title") or "").strip()
                    content_s = str(raw_val.get("content") or "").strip()
                elif isinstance(raw_val, str):
                    content_s = raw_val.strip()
                if content_s:
                    extracted_rows.append((it, title_s[:40], content_s[:360]))
            return extracted_rows, pts

        batch_outputs: list[tuple[list[tuple[dict[str, str], str, str]], float]] = []
        if len(batches) == 1:
            try:
                batch_outputs.append(_run_batch(batches[0]))
            except Exception as exc:
                logger.warning("slide vision OCR batch failed gracefully: %s", type(exc).__name__)
        else:
            with ThreadPoolExecutor(max_workers=min(5, len(batches))) as pool:
                futures = [pool.submit(copy_context().run, _run_batch, b) for b in batches]
                for fut in futures:
                    try:
                        batch_outputs.append(fut.result())
                    except Exception as exc:
                        logger.warning("slide vision OCR batch failed gracefully: %s", type(exc).__name__)

        cache_dirty = False
        for extracted_rows, pts in batch_outputs:
            total_vision_points += pts
            for it, title_s, content_s in extracted_rows:
                fn = it["fname"]
                results[fn] = {"title": title_s, "content": content_s}
                cached_frames[fn] = {
                    "sha256": it["sha256"],
                    "time": it["time"],
                    "title": title_s,
                    "content": content_s,
                }
                cache_dirty = True
        if cache_dirty:
            _save_slide_ocr_cache(job.directory, shared_dir, cached_frames)

    ordered_rows: list[dict[str, str]] = []
    ocr_chars = 0
    for entry in selected_entries:
        fn = str(entry["fname"])
        ocr_item = results.get(fn)
        if not ocr_item or not str(ocr_item.get("content") or "").strip():
            continue
        stitle = str(ocr_item.get("title") or "").strip()
        scontent = str(ocr_item.get("content") or "").strip()
        est_add = len(stitle) + len(scontent) + 28
        if ocr_chars + est_add > MAX_SLIDE_OCR_TOTAL_CHARS and ordered_rows:
            break
        ordered_rows.append(
            {
                "file": fn,
                "time": str(entry.get("time") or ""),
                "kind": str(entry.get("kind") or "slide"),
                "title": stitle,
                "content": scontent,
            }
        )
        ocr_chars += est_add

    return results, ordered_rows, round(total_vision_points, 4)


def _build_bounded_refine_user_json(
    payload_dict: dict[str, Any],
    *,
    system: str,
    model: str,
    operation_context: dict[str, str] | None,
    max_bytes: int = 238_000,
) -> str:
    """Serialize refine payload within request character and JSON-escaped byte caps."""
    from ..ai_provider import llm_request_body_size

    def _fits(u_json: str) -> bool:
        if len(system) + len(u_json) > 195_000:
            return False
        return (
            llm_request_body_size(
                "refine",
                system,
                u_json,
                model=model,
                operation_context=operation_context,
            )
            <= max_bytes
        )

    user_json = json.dumps(payload_dict, ensure_ascii=False)
    if _fits(user_json):
        return user_json

    work = copy.deepcopy(payload_dict)

    # 1. Remove duplicated slide_content from available_visuals (slide_visual_ocr carries the full text)
    for vis in work.get("available_visuals") or []:
        if isinstance(vis, dict):
            vis.pop("slide_content", None)
            if isinstance(vis.get("ocr_hint"), str) and len(vis["ocr_hint"]) > 80:
                vis["ocr_hint"] = vis["ocr_hint"][:80]
    user_json = json.dumps(work, ensure_ascii=False)
    if _fits(user_json):
        return user_json

    # 2. Trim verbose OCR / courseware snippet text while preserving full slide/page coverage
    for ocr_lim, cw_lim in ((150, 320), (100, 200)):
        for row in work.get("slide_visual_ocr") or []:
            if isinstance(row, dict) and isinstance(row.get("content"), str) and len(row["content"]) > ocr_lim:
                row["content"] = row["content"][:ocr_lim]
        for pg in work.get("matched_courseware_pages") or []:
            if isinstance(pg, dict) and isinstance(pg.get("text"), str) and len(pg["text"]) > cw_lim:
                pg["text"] = pg["text"][:cw_lim]
        user_json = json.dumps(work, ensure_ascii=False)
        if _fits(user_json):
            return user_json

    # 3. Uniformly downsample slide_visual_ocr if still above byte cap
    ocr_list = work.get("slide_visual_ocr") or []
    for cap in (36, 24, 16):
        if len(ocr_list) > cap:
            work["slide_visual_ocr"] = _downsample_visual_candidates(ocr_list, cap)
            user_json = json.dumps(work, ensure_ascii=False)
            if _fits(user_json):
                return user_json

    # 4. Proportionally trim raw_markdown and remaining extras until guaranteed under cap
    raw_md = str(work.get("raw_markdown") or "")
    for _ in range(12):
        if _fits(user_json):
            return user_json
        if len(raw_md) > 12_000:
            raw_md = raw_md[: max(12_000, int(len(raw_md) * 0.78))]
            work["raw_markdown"] = raw_md
        elif len(work.get("matched_courseware_pages") or []) > 8:
            pages = work["matched_courseware_pages"]
            work["matched_courseware_pages"] = pages[: max(8, len(pages) // 2)]
        elif len(work.get("slide_visual_ocr") or []) > 8:
            rows = work["slide_visual_ocr"]
            work["slide_visual_ocr"] = _downsample_visual_candidates(rows, max(8, len(rows) // 2))
        else:
            raw_md = raw_md[: int(len(raw_md) * 0.75)]
            work["raw_markdown"] = raw_md
        user_json = json.dumps(work, ensure_ascii=False)

    return user_json


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _prerender_referenced_slide_pages(job, markdown: str, *, max_pages: int = 24) -> int:
    """Render any PDF `/slide-page?` URLs in `markdown` to `keyframes/slides-cache/*.jpg`."""
    if "/slide-page?" not in (markdown or ""):
        return 0
    from urllib.parse import parse_qs, unquote, urlsplit
    from .note_images import _local_material, _slide_cache

    targets: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for _, url in _MD_IMG_ANY_RE.findall(markdown):
        if "/slide-page?" not in url:
            continue
        qs = parse_qs(urlsplit(url).query)
        sfile = unquote((qs.get("file") or [""])[0]).strip()
        try:
            spage = int((qs.get("page") or ["0"])[0])
        except ValueError:
            spage = 0
        if sfile and spage >= 1 and (sfile, spage) not in seen:
            seen.add((sfile, spage))
            targets.append((sfile, spage))
        if len(targets) >= max_pages:
            break
    if not targets:
        return 0

    rendered = 0
    try:
        import cv2
        import pypdfium2 as pdfium
    except Exception:
        return 0

    by_file: dict[str, list[int]] = {}
    for sfile, spage in targets:
        cache = _slide_cache(job, sfile, spage)
        if any(Path(f"{cache}{ext}").is_file() for ext in (".jpg", ".png")):
            continue
        by_file.setdefault(sfile, []).append(spage)

    for sfile, pages in by_file.items():
        pdf_path = _local_material(job, sfile)
        if pdf_path is None or not pdf_path.is_file():
            continue
        try:
            with pdfium.PdfDocument(str(pdf_path)) as document:
                for spage in pages:
                    if spage < 1 or spage > len(document):
                        continue
                    cache = _slide_cache(job, sfile, spage)
                    image = Path(f"{cache}.jpg")
                    if image.is_file():
                        continue
                    pdf_page = document[spage - 1]
                    bitmap = None
                    try:
                        bitmap = pdf_page.render(scale=min(2, 1600 / max(pdf_page.get_size())))
                        pixels = bitmap.to_numpy()
                        ok, encoded = cv2.imencode(".jpg", pixels)
                        if not ok:
                            continue
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        staged = image.with_name(f".{image.name}.{secrets.token_hex(4)}.tmp")
                        try:
                            staged.write_bytes(encoded.tobytes())
                            staged.replace(image)
                            rendered += 1
                        finally:
                            staged.unlink(missing_ok=True)
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        pdf_page.close()
        except Exception:
            continue
    return rendered


def generate_refined_lecture_note(
    job,
    *,
    course_id: str,
    recording_id: str,
    title: str,
    raw_markdown: str,
    platform_service: Any,
    force: bool = False,
    settings: Any = None,
) -> dict[str, Any]:
    """Generate and persist an AI-refined, chit-chat-free exam lecture handout."""
    cleaned_raw = (raw_markdown or "").strip()
    if not cleaned_raw:
        raise ValueError("本讲尚未生成课堂笔记，请先生成笔记后再使用 AI 精编考点讲义。")

    from .ai_bridge import LocalAIBridge

    active_settings = settings if settings is not None else getattr(platform_service, "_settings", None)
    if platform_service is None and active_settings is not None:
        platform_service = LocalAIBridge(active_settings)

    estimate = estimate_refine_cost(cleaned_raw, settings=active_settings)
    saved = load_saved_refined_note(job.directory, cleaned_raw)
    if saved is not None and not force:
        return {
            "ok": True,
            "markdown": saved["markdown"],
            "meta": {**saved["meta"], "stale": False},
            "reused": True,
            "points_charged": 0.0,
            "refine_estimate": estimate,
        }

    if platform_service is None or not hasattr(platform_service, "refine_note"):
        raise ValueError("AI 精编服务尚未就绪，请先在「模型配置」中完成设置。")

    safe_cid = re.sub(r"[^A-Za-z0-9_:.-]+", "_", str(course_id or "course"))[:40] or "course"
    safe_rid = re.sub(r"[^A-Za-z0-9_:.-]+", "_", str(recording_id or "rec"))[:40] or "rec"
    notes_sha = _sha256_text(cleaned_raw)
    op_id = f"refine-note:{safe_cid}:{safe_rid}:{notes_sha[:12]}"[:160]
    context = {
        "purpose": "refine-note",
        "course_id": safe_cid,
        "source_id": safe_rid,
        "client_operation_id": op_id,
    }

    visuals, allowed_urls = _collect_allowed_visuals(job, course_id, recording_id, cleaned_raw)
    cw_summary, cw_pages, slide_visuals, slide_allowed = _collect_matched_courseware(
        job, course_id, recording_id
    )
    allowed_urls |= slide_allowed
    merged_visuals: list[dict[str, str]] = list(slide_visuals)
    seen_merged = {str(v.get("url") or "") for v in merged_visuals}
    extra_visuals: list[dict[str, str]] = []
    for v in visuals:
        vurl = str(v.get("url") or "") if isinstance(v, dict) else ""
        if vurl and vurl not in seen_merged:
            extra_visuals.append(v)
            seen_merged.add(vurl)
        elif not vurl and isinstance(v, dict):
            extra_visuals.append(v)
    remaining_budget = max(0, MAX_MERGED_VISUALS - len(merged_visuals))
    merged_visuals.extend(_downsample_visual_candidates(extra_visuals, remaining_budget))

    ocr_by_file, slide_visual_ocr, _vision_pts = _extract_slide_visual_ocr(
        job,
        merged_visuals,
        cw_pages,
        platform_service,
        min_reserve_millipoints=0,
    )
    for v in merged_visuals:
        if not isinstance(v, dict):
            continue
        vurl = str(v.get("url") or "").strip()
        if not vurl or "/slide-page?" in vurl:
            continue
        fname = Path(vurl.split("?", 1)[0]).name
        ocr_entry = ocr_by_file.get(fname)
        if not ocr_entry:
            continue
        stitle = ocr_entry.get("title") or ""
        scontent = ocr_entry.get("content") or ""
        if stitle:
            v["slide_title"] = stitle
        if scontent:
            v["slide_content"] = scontent
            if not v.get("ocr_hint"):
                v["ocr_hint"] = scontent[:180]

    board_ocr = _load_board_notes_ocr(job.directory)
    payload_dict: dict[str, Any] = {
        "course_id": course_id,
        "lecture_title": title or recording_id,
        "raw_note_chars": estimate["raw_chars"],
        "available_visuals": merged_visuals[:MAX_MERGED_VISUALS],
        "matched_courseware": cw_summary,
        "matched_courseware_pages": cw_pages,
        "slide_visual_ocr": slide_visual_ocr,
        "board_notes_ocr": board_ocr,
        "raw_markdown": cleaned_raw[:MAX_RAW_CHARS_FOR_PROMPT],
    }

    selected_model = estimate["model"]
    user_json = _build_bounded_refine_user_json(
        payload_dict,
        system=REFINED_NOTE_SYSTEM,
        model=selected_model,
        operation_context=context,
    )
    response = platform_service.refine_note(
        user_json,
        system=REFINED_NOTE_SYSTEM,
        operation_context=context,
    )
    raw_refined = str((response or {}).get("content") or "")
    sanitized = _sanitize_refined_markdown(raw_refined, allowed_urls)
    if not sanitized:
        raise ValueError("AI 未返回有效的精编讲义内容，请稍后重试。")

    meta = {
        "schema": "refined-note-v1",
        "course_id": course_id,
        "recording_id": recording_id,
        "notes_sha256": notes_sha,
        "model": selected_model,
        "raw_chars": estimate["raw_chars"],
        "refined_chars": len(sanitized),
        "points_charged": 0.0,
        "vision_points_charged": 0.0,
        "slide_ocr_count": len(slide_visual_ocr),
        "courseware": cw_summary,
    }
    _atomic_write_text(job.directory / REFINED_NOTE_FILENAME, sanitized)
    _atomic_write_text(
        job.directory / REFINED_META_FILENAME,
        json.dumps(meta, ensure_ascii=False, indent=2),
    )
    try:
        _prerender_referenced_slide_pages(job, sanitized)
    except Exception:
        pass

    return {
        "ok": True,
        "markdown": sanitized,
        "meta": {**meta, "stale": False},
        "reused": False,
        "points_charged": 0.0,
        "refine_estimate": estimate,
    }
