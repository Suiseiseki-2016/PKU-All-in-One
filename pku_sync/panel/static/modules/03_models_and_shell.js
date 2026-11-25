  function ensureModelDraft(forceReset) {
    var data = (state.models && state.models.data) || {};
    if (!state.models) {
      state.models = { loading: false, loaded: false, saving: false, testing: "", testResult: null, error: "", data: null, draft: null };
    }
    if (!state.models.draft || forceReset) {
      state.models.draft = {
        openai_base_url: data.openai_base_url || "",
        openai_api_key: "",
        clear_openai_api_key: false,
        notes_model: data.notes_model || "deepseek-chat",
        refine_model: data.refine_model || "",
        notes_strategy: data.notes_strategy === "transcript" ? "transcript" : "topics",
        vision_enabled: data.vision_enabled !== false,
        vision_base_url: data.vision_base_url || "",
        vision_api_key: "",
        clear_vision_api_key: false,
        vision_model: data.vision_model || "",
        transcription_backend: data.transcription_backend || "openai",
        asr_base_url: data.asr_base_url || "",
        asr_api_key: "",
        clear_asr_api_key: false,
        asr_model: data.asr_model || "whisper-1",
        dashscope_api_key: "",
        clear_dashscope_api_key: false,
        whisper_model: data.whisper_model || "small",
        whisper_device: data.whisper_device || "auto",
        whisper_compute_type: data.whisper_compute_type || "auto"
      };
    }
    return state.models.draft;
  }

  function captureModelFormToDraft() {
    var form = document.getElementById("models-config-form");
    if (!form) return ensureModelDraft(false);
    var draft = ensureModelDraft(false);
    function val(id) {
      var el = document.getElementById(id);
      return el ? String(el.value || "") : null;
    }
    function checked(id) {
      var el = document.getElementById(id);
      return el ? Boolean(el.checked) : null;
    }
    var map = {
      "model-openai-base-url": "openai_base_url",
      "model-openai-api-key": "openai_api_key",
      "model-notes-model": "notes_model",
      "model-refine-model": "refine_model",
      "model-notes-strategy": "notes_strategy",
      "model-vision-base-url": "vision_base_url",
      "model-vision-api-key": "vision_api_key",
      "model-vision-model": "vision_model",
      "model-asr-base-url": "asr_base_url",
      "model-asr-api-key": "asr_api_key",
      "model-asr-model": "asr_model",
      "model-dashscope-api-key": "dashscope_api_key",
      "model-whisper-model": "whisper_model",
      "model-whisper-device": "whisper_device",
      "model-whisper-compute-type": "whisper_compute_type"
    };
    Object.keys(map).forEach(function (id) {
      var v = val(id);
      if (v !== null) draft[map[id]] = v;
    });
    var visionOn = checked("model-vision-enabled");
    if (visionOn !== null) draft.vision_enabled = visionOn;
    return draft;
  }

  function modelReadinessInfo() {
    var data = (state.models && state.models.data) || null;
    if (!data) {
      return {
        loaded: false,
        llmConfigured: false,
        visionConfigured: false,
        asrConfigured: false,
        ready: false,
        notesModel: "尚未读取",
        asrLabel: "OpenAI 兼容"
      };
    }
    var backend = String(data.transcription_backend || "openai").toLowerCase();
    var asrLabel = backend === "dashscope" ? "百炼 Qwen3-ASR"
      : backend === "local" ? ("Whisper " + (data.whisper_model || "small"))
      : (data.asr_model || "OpenAI 兼容");
    return {
      loaded: true,
      llmConfigured: Boolean(data.llm_configured),
      visionConfigured: Boolean(data.vision_configured),
      asrConfigured: Boolean(data.asr_configured),
      ready: Boolean(data.llm_configured && data.asr_configured),
      notesModel: data.notes_model || "未设置",
      asrLabel: asrLabel
    };
  }

  function activeModelSummaryCard(location) {
    var info = modelReadinessInfo();
    var onModels = state.view === "models";
    var badgeText = !info.loaded && state.models && state.models.loading
      ? "检测中"
      : (info.ready ? "模型已就绪" : (info.llmConfigured ? "待配 ASR" : "待配置模型"));
    var badgeCls = info.ready ? "is-ready" : "is-pending";
    var extra = location === "mobile" ? ' mobile-quota-block' : '';
    return '<div class="account-card sidebar-quota-compact sidebar-models-card' + extra + (onModels ? ' is-membership-active' : '') + '" data-block="models-summary">' +
      '<button type="button" class="sidebar-quota-jump" data-action="open-models" aria-label="前往模型配置">' +
      '<div class="sidebar-quota-line">' +
      '<span class="nav-status-pill ' + badgeCls + '">' + esc(badgeText) + '</span>' +
      '<span class="sidebar-quota-arrow" aria-hidden="true">→</span>' +
      '</div>' +
      '<div class="sidebar-quota-meta"><span>对话：' + esc(info.notesModel) + '</span><span>语音：' + esc(info.asrLabel) + '</span></div>' +
      '</button></div>';
  }

  function modelTestResultBanner(target) {
    var res = state.models && state.models.testResult;
    if (!res || res.target !== target) return "";
    var cls = res.ok ? "model-test-result is-ok" : "model-test-result is-err";
    var prefix = res.ok ? "✓ 连接成功" : "⚠ 测试未通过";
    var latency = res.latency_ms ? (" · " + res.latency_ms + "ms") : "";
    return '<div class="' + cls + '" role="status" data-model-test-result="' + esc(target) + '">' +
      '<strong>' + esc(prefix + latency) + '</strong>' +
      '<span>' + esc(res.message || res.detail || "") + '</span>' +
      '</div>';
  }

  function modelSettingsScreen() {
    var copy = (COPY && COPY.models) || {};
    var data = (state.models && state.models.data) || {};
    var draft = ensureModelDraft(false);
    var presets = Array.isArray(data.presets) ? data.presets : [];
    var loading = Boolean(state.models && state.models.loading && !state.models.loaded);
    var saving = Boolean(state.models && state.models.saving);
    var testing = (state.models && state.models.testing) || "";
    var backend = String(draft.transcription_backend || "openai").toLowerCase();
    var presetButtons = presets.map(function (preset) {
      var active = String(draft.openai_base_url || "").trim().replace(/\/+$/, "") === String(preset.base_url || "").trim().replace(/\/+$/, "");
      return '<button type="button" class="btn btn-small model-preset-chip' + (active ? ' btn-primary' : ' btn-quiet') + '" data-action="apply-model-preset" data-preset="' + esc(preset.id) + '">' +
        esc(preset.label || preset.id) + '</button>';
    }).join("");

    var openaiKeyHint = draft.clear_openai_api_key
      ? "将在保存时清除已保存的 API Key"
      : (data.openai_api_key_set
          ? ("当前已配置：" + (data.openai_api_key_masked || "sk-***") + "（留空保持不变）")
          : "请输入 API Key（如 sk-...，Ollama 可留空）");
    var visionKeyHint = draft.clear_vision_api_key
      ? "将在保存时清除视觉模型 API Key（改回复用主模型）"
      : (data.vision_api_key_set
          ? ("已单独配置：" + (data.vision_api_key_masked || "sk-***") + "（留空保持不变）")
          : "留空则复用上方对话大模型 API Key");
    var asrKeyHint = draft.clear_asr_api_key
      ? "将在保存时清除 ASR API Key（改回复用主模型）"
      : (data.asr_api_key_set
          ? ("已单独配置：" + (data.asr_api_key_masked || "sk-***") + "（留空保持不变）")
          : "留空则复用上方对话大模型 API Key");
    var dsKeyHint = draft.clear_dashscope_api_key
      ? "将在保存时清除百炼 API Key"
      : (data.dashscope_api_key_set
          ? ("已配置：" + (data.dashscope_api_key_masked || "sk-***") + "（留空保持不变）")
          : "请输入阿里云百炼 API Key（sk-...）");

    var asrBackendPanel = "";
    if (backend === "dashscope") {
      asrBackendPanel =
        '<div class="model-field-grid">' +
        '<div class="model-field">' +
        '<label class="field-label" for="model-dashscope-api-key">阿里云百炼 DashScope API Key</label>' +
        '<div class="model-secret-row">' +
        '<input id="model-dashscope-api-key" type="password" class="text-input" autocomplete="new-password" placeholder="' + esc(dsKeyHint) + '" value="' + esc(draft.dashscope_api_key) + '">' +
        (data.dashscope_api_key_set && !draft.clear_dashscope_api_key
          ? button("清除已存 Key", "clear-model-key", { small: true, quiet: true, attrs: { "data-key-field": "dashscope_api_key" } })
          : "") +
        '</div>' +
        '<small class="form-note">调用百炼原生 <code>qwen3-asr-flash</code> 接口，中文课堂识别准确率高，音频分段在内存中流式处理，不落盘永久保存。</small>' +
        '</div>' +
        '</div>';
    } else if (backend === "local") {
      var localBadge = data.local_asr_available
        ? '<span class="status-badge indexed">✓ 本机已安装 faster-whisper</span>'
        : '<span class="status-badge urgent">⚠ 本机未安装 faster-whisper（请运行 uv sync --extra local-asr）</span>';
      var whisperModels = ["tiny", "base", "small", "medium", "large-v3"].map(function (m) {
        return '<option value="' + m + '"' + (draft.whisper_model === m ? ' selected' : '') + '>' + m + '</option>';
      }).join("");
      var whisperDevices = ["auto", "cpu", "cuda"].map(function (d) {
        return '<option value="' + d + '"' + (draft.whisper_device === d ? ' selected' : '') + '>' + d + '</option>';
      }).join("");
      var whisperCompute = ["auto", "int8", "float16", "float32"].map(function (c) {
        return '<option value="' + c + '"' + (draft.whisper_compute_type === c ? ' selected' : '') + '>' + c + '</option>';
      }).join("");
      asrBackendPanel =
        '<div class="model-local-status">' + localBadge + '</div>' +
        '<div class="model-field-grid model-field-grid-3">' +
        '<div class="model-field"><label class="field-label" for="model-whisper-model">Whisper 模型尺寸</label>' +
        '<select id="model-whisper-model" class="text-input">' + whisperModels + '</select></div>' +
        '<div class="model-field"><label class="field-label" for="model-whisper-device">计算设备</label>' +
        '<select id="model-whisper-device" class="text-input">' + whisperDevices + '</select></div>' +
        '<div class="model-field"><label class="field-label" for="model-whisper-compute-type">量化精度</label>' +
        '<select id="model-whisper-compute-type" class="text-input">' + whisperCompute + '</select></div>' +
        '</div>';
    } else {
      asrBackendPanel =
        '<div class="model-field-grid">' +
        '<div class="model-field"><label class="field-label" for="model-asr-base-url">ASR Base URL（选填）</label>' +
        '<input id="model-asr-base-url" type="text" class="text-input" autocomplete="off" placeholder="留空则默认沿用上方对话模型 Base URL" value="' + esc(draft.asr_base_url) + '"></div>' +
        '<div class="model-field"><label class="field-label" for="model-asr-api-key">ASR API Key（选填）</label>' +
        '<div class="model-secret-row">' +
        '<input id="model-asr-api-key" type="password" class="text-input" autocomplete="new-password" placeholder="' + esc(asrKeyHint) + '" value="' + esc(draft.asr_api_key) + '">' +
        (data.asr_api_key_set && !draft.clear_asr_api_key
          ? button("清除已存 Key", "clear-model-key", { small: true, quiet: true, attrs: { "data-key-field": "asr_api_key" } })
          : "") +
        '</div></div>' +
        '<div class="model-field"><label class="field-label" for="model-asr-model">语音转写模型名</label>' +
        '<input id="model-asr-model" type="text" class="text-input" autocomplete="off" placeholder="whisper-1" value="' + esc(draft.asr_model || "whisper-1") + '"></div>' +
        '</div>' +
        '<small class="form-note">支持任意 OpenAI Audio Transcriptions 兼容接口（提示：DeepSeek 官方暂无语音转写端点，可切换“阿里云百炼”或“本机 Faster-Whisper”，也可在此单独填写 OpenAI/Groq 等 ASR 接口）。</small>';
    }

    return breadcrumb([{ label: "我的课程", action: "open-dashboard" }, { label: "模型配置" }]) +
      '<div class="topline"><div><h1 class="page-title">' + esc(copy.title || "模型配置（BYOK）") + '</h1>' +
      '<p class="page-desc">' + esc(copy.subtitle || "自由配置您自己的大模型、视觉识别与语音转写服务。所有 API Key 仅保存于本机 .env 文件。") + '</p></div></div>' +
      '<div class="campus-trust-callout" data-byok-security-note>' +
      '<strong>🔑 本地直连 · 开源无账号</strong> · ' +
      '<span>' + esc(copy.security_note || "请求直接从本机发往您配置的 API 服务商或本地模型，不经过任何第三方中转服务器。") + '</span>' +
      '</div>' +
      (loading ? '<section class="panel" role="status">正在读取当前本机 .env 模型配置…</section>' : '') +
      (state.models && state.models.error ? '<p class="form-note" role="alert">' + esc(state.models.error) + '</p>' : '') +
      '<form id="models-config-form" class="models-config-form" data-models-form>' +
      '<section class="panel model-config-section" data-model-presets-box>' +
      '<div class="model-section-head"><div><h2>1 · 快速选择模型服务商预设</h2><p class="form-note">一键填入接口地址与推荐模型，不会清空您已填的独立密钥。</p></div></div>' +
      '<div class="model-presets-row">' + (presetButtons || '<span class="form-note">连接面板后将显示可用服务商预设。</span>') + '</div>' +
      '</section>' +
      '<section class="panel model-config-section" data-model-llm-card>' +
      '<div class="model-section-head"><div><h2>2 · 对话大模型（课堂笔记 · 精细整理 · 导图自测 · AI 问答）</h2></div>' +
      '<span class="nav-status-pill ' + (data.llm_configured ? 'is-ready' : 'is-pending') + '">' + (data.llm_configured ? '✓ 已配置' : '待配置') + '</span></div>' +
      '<div class="model-field-grid">' +
      '<div class="model-field"><label class="field-label" for="model-openai-base-url">API Base URL（OpenAI 兼容接口）</label>' +
      '<input id="model-openai-base-url" type="text" class="text-input" autocomplete="off" placeholder="点击上方预设自动填充服务商 /v1 地址" value="' + esc(draft.openai_base_url) + '"></div>' +
      '<div class="model-field"><label class="field-label" for="model-openai-api-key">API Key</label>' +
      '<div class="model-secret-row">' +
      '<input id="model-openai-api-key" type="password" class="text-input" autocomplete="new-password" placeholder="' + esc(openaiKeyHint) + '" value="' + esc(draft.openai_api_key) + '">' +
      (data.openai_api_key_set && !draft.clear_openai_api_key
        ? button("清除已存 Key", "clear-model-key", { small: true, quiet: true, attrs: { "data-key-field": "openai_api_key" } })
        : "") +
      '</div></div>' +
      '<div class="model-field"><label class="field-label" for="model-notes-model">主模型（NOTES_MODEL）</label>' +
      '<input id="model-notes-model" type="text" class="text-input" autocomplete="off" placeholder="deepseek-chat / gpt-4o / qwen-plus" value="' + esc(draft.notes_model) + '"></div>' +
      '<div class="model-field"><label class="field-label" for="model-refine-model">精细整理专用模型（选填，REFINE_MODEL）</label>' +
      '<input id="model-refine-model" type="text" class="text-input" autocomplete="off" placeholder="留空则默认复用主模型（可填推理/长文本强模型如 deepseek-reasoner）" value="' + esc(draft.refine_model) + '"></div>' +
      '<div class="model-field"><label class="field-label" for="model-notes-strategy">默认笔记生成模式</label>' +
      '<select id="model-notes-strategy" class="text-input">' +
      '<option value="topics"' + (draft.notes_strategy === "topics" ? ' selected' : '') + '>按知识点模块精讲（推荐）</option>' +
      '<option value="transcript"' + (draft.notes_strategy === "transcript" ? ' selected' : '') + '>按课堂时间顺序逐段整理</option>' +
      '</select></div>' +
      '</div>' +
      '<div class="model-section-actions">' +
      button(testing === "llm" ? "正在测试对话模型…" : "测试对话模型连接", "test-model-connection", { small: true, quiet: true, disabled: Boolean(testing || saving), attrs: { "data-target": "llm" } }) +
      '</div>' +
      modelTestResultBanner("llm") +
      '</section>' +
      '<section class="panel model-config-section" data-model-vision-card>' +
      '<div class="model-section-head"><div><h2>3 · 黑板板书与课件视觉识别（VLM OCR）</h2></div>' +
      '<span class="nav-status-pill ' + (!draft.vision_enabled ? 'is-muted' : data.vision_configured ? 'is-ready' : 'is-pending') + '">' +
      (!draft.vision_enabled ? '已关闭' : data.vision_configured ? '✓ 可用' : '待配置') + '</span></div>' +
      '<label class="model-toggle-label"><input id="model-vision-enabled" type="checkbox"' + (draft.vision_enabled ? ' checked' : '') + '> <span>启用多模态视觉模型提取课堂黑板手写板书与数学公式（关闭则仅依赖语音转写 + PDF 课件文本）</span></label>' +
      '<div class="model-field-grid">' +
      '<div class="model-field"><label class="field-label" for="model-vision-base-url">视觉模型 Base URL（选填）</label>' +
      '<input id="model-vision-base-url" type="text" class="text-input" autocomplete="off" placeholder="留空则复用主模型 Base URL" value="' + esc(draft.vision_base_url) + '"></div>' +
      '<div class="model-field"><label class="field-label" for="model-vision-api-key">视觉模型 API Key（选填）</label>' +
      '<div class="model-secret-row">' +
      '<input id="model-vision-api-key" type="password" class="text-input" autocomplete="new-password" placeholder="' + esc(visionKeyHint) + '" value="' + esc(draft.vision_api_key) + '">' +
      (data.vision_api_key_set && !draft.clear_vision_api_key
        ? button("清除已存 Key", "clear-model-key", { small: true, quiet: true, attrs: { "data-key-field": "vision_api_key" } })
        : "") +
      '</div></div>' +
      '<div class="model-field"><label class="field-label" for="model-vision-model">视觉模型名称（选填）</label>' +
      '<input id="model-vision-model" type="text" class="text-input" autocomplete="off" placeholder="如 qwen3-vl-plus / gpt-4o-mini（留空则复用主模型）" value="' + esc(draft.vision_model) + '"></div>' +
      '</div>' +
      '<div class="model-section-actions">' +
      button(testing === "vision" ? "正在测试视觉模型…" : "测试视觉模型连接", "test-model-connection", { small: true, quiet: true, disabled: Boolean(testing || saving || !draft.vision_enabled), attrs: { "data-target": "vision" } }) +
      '</div>' +
      modelTestResultBanner("vision") +
      '</section>' +
      '<section class="panel model-config-section" data-model-asr-card>' +
      '<div class="model-section-head"><div><h2>4 · 课堂回放语音转写（ASR）</h2></div>' +
      '<span class="nav-status-pill ' + (data.asr_configured ? 'is-ready' : 'is-pending') + '">' + (data.asr_configured ? '✓ 已配置' : '待配置') + '</span></div>' +
      '<div class="model-asr-tabs" role="tablist" aria-label="语音转写后端">' +
      button("OpenAI 兼容接口", "select-asr-backend", { small: true, primary: backend === "openai", quiet: backend !== "openai", attrs: { "data-backend": "openai" } }) +
      button("阿里云百炼（推荐中文）", "select-asr-backend", { small: true, primary: backend === "dashscope", quiet: backend !== "dashscope", attrs: { "data-backend": "dashscope" } }) +
      button("本机 Faster-Whisper", "select-asr-backend", { small: true, primary: backend === "local", quiet: backend !== "local", attrs: { "data-backend": "local" } }) +
      '</div>' +
      asrBackendPanel +
      '<div class="model-section-actions">' +
      button(testing === "asr" ? "正在检查转写后端…" : "测试语音转写配置", "test-model-connection", { small: true, quiet: true, disabled: Boolean(testing || saving), attrs: { "data-target": "asr" } }) +
      '</div>' +
      modelTestResultBanner("asr") +
      '</section>' +
      '<div class="model-save-bar">' +
      button(saving ? "正在写入 .env…" : "保存模型配置并应用", "save-model-settings", { primary: true, disabled: Boolean(saving) }) +
      '<span class="form-note">保存后立即写入本机 <code>.env</code> 文件并热更新运行实例，无需重启服务。</span>' +
      '</div>' +
      '</form>';
  }

  function cachedCourseNotice(course) {
    if (!course.read_only) return '';
    return course.read_only_message || '这门课是上次缓存的本地目录；点击上方「更新课程目录」重新连接教学网。';
  }

  function updateCard() {
    var copy = COPY.update;
    var u = state.update;
    if (!u || !u.current_version) return "";
    if (!u.supported) {
      return (
        '<div class="update-card"><div class="account-name">' +
        esc(template(copy.version, { version: u.current_version })) +
        '</div><p>' +
        esc(copy.desktop_managed) +
        "</p></div>"
      );
    }
    var status = u.available
      ? template(copy.available, { version: u.latest_version })
      : copy.up_to_date;
    var action = u.available
      ? button(u.requested ? copy.requested : u.deferred ? copy.retry : copy.action, "update", {
          small: true,
          disabled: u.requested,
          attrs: { "data-version": u.latest_version }
        })
      : "";
    return (
      '<div class="update-card"><div class="account-name">' +
      esc(template(copy.version, { version: u.current_version })) +
      "</div><p>" +
      esc(status) +
      "</p>" +
      action +
      "</div>"
    );
  }

  function requestUpdate(version) {
    return requestJson("/api/update/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: version })
    }).then(function (result) {
      if (!result.ok) return showToast(COPY.update.request_failed);
      var status = result.body && result.body.status;
      state.update.requested = status === "requested";
      state.update.deferred = status === "deferred";
      render();
      showToast(status === "deferred" ? COPY.update.deferred : COPY.update.requested);
    });
  }

  function shell(content) {
    var modelInfo = modelReadinessInfo();
    var modelPill = (!modelInfo.loaded && state.models && state.models.loading)
      ? ''
      : '<span class="nav-status-pill ' + (modelInfo.ready ? 'is-connected' : 'is-pending') + '">' +
        (modelInfo.ready ? '已配置' : '待配置') + '</span>';
    var campusPill = '<span class="nav-status-pill' + (state.campus.configured ? ' is-connected' : '') + '">' +
      (state.campus.configured ? '已连接' : '未连接') + '</span>';
    return (
      '<div class="app-shell"><aside class="sidebar" aria-label="' +
      esc(COPY.nav.label) +
      '">' +
      brand(false) +
      '<p class="side-kicker">' +
      esc(COPY.nav.kicker) +
      '</p><nav class="nav"><button type="button" class="' +
      (state.view === "dashboard" || state.view === "campus-course" ? "active" : "") +
      '" data-nav="dashboard" data-action="open-dashboard"' +
      (state.view === "dashboard" ? ' aria-current="page"' : "") +
      '><span class="nav-icon" aria-hidden="true">' +
      esc(GLYPH.home) +
      "</span>" +
      esc(COPY.nav.dashboard) +
      '</button><button type="button" class="btn btn-quiet' + (state.view === "models" ? " active" : "") + '" data-nav="models" data-action="open-models"' + (state.view === "models" ? ' aria-current="page"' : "") + '><span class="nav-icon" aria-hidden="true">◈</span><span>' + esc(COPY.nav.models || "模型配置") + '</span>' + modelPill + '</button>' +
      '<button type="button" class="btn btn-quiet' + (state.view === "mcp-hub" ? " active" : "") + '" data-nav="mcp-hub" data-action="open-mcp-hub"' + (state.view === "mcp-hub" ? ' aria-current="page"' : "") + '><span class="nav-icon" aria-hidden="true">✦</span><span>' + esc(COPY.nav.mcp_hub || "AI 接入 (MCP)") + '</span></button>' +
      '<button type="button" class="btn btn-quiet' + (state.view === "campus-account" ? " active" : "") + '" data-nav="campus-account" data-action="campus-account-settings"' + (state.view === "campus-account" ? ' aria-current="page"' : "") + '><span class="nav-icon" aria-hidden="true">⚙</span><span>' + esc(COPY.nav.campus_account || "教学网连接") + '</span>' + campusPill + '</button>' +
      '</nav><div class="sidebar-spacer"></div>' +
      activeModelSummaryCard("desktop") +
      updateCard() +
      '</aside><header class="mobile-topbar"><div class="mobile-topbar-left">' +
      '<button type="button" class="mobile-overview" data-nav="dashboard" data-action="open-dashboard" aria-label="' +
      esc(COPY.nav.dashboard) +
      '"' +
      (state.view === "dashboard" ? ' aria-current="page"' : "") +
      '><span aria-hidden="true">' +
      esc(GLYPH.home) +
      '</span><span>' +
      esc(COPY.nav.dashboard) +
      '</span></button>' +
      brand(true) +
      '</div><div class="mobile-connection">' +
      '</div><details class="mobile-account-menu" data-persistent-disclosure="mobile-account"><summary>工作台配置</summary><div class="mobile-account-content">' +
      '<div class="mobile-quick-nav">' +
      button(COPY.nav.models || "模型配置", "open-models", { small: true, quiet: true }) +
      button(COPY.nav.mcp_hub || "AI 接入 (MCP)", "open-mcp-hub", { small: true, quiet: true }) +
      button(COPY.nav.campus_account || "教学网连接", "campus-account-settings", { small: true, quiet: true }) +
      '</div>' +
      activeModelSummaryCard("mobile") + updateCard() +
      '</div></details></header>' +
      '<main class="main"><div class="main-inner">' +
      content +
      "</div></main></div>"
    );
  }

  /* -- dashboard --------------------------------------------------------- */

  function dashboardScreen() {
    return campusDashboardScreen();
  }

  /* -- course detail ----------------------------------------------------- */

  function breadcrumb(trail, extraHtml) {
    var parts = trail.map(function (item) {
      if (!item.action) return "<span>" + esc(item.label) + "</span>";
      var attrs = "";
      if (item.course) attrs = ' data-course="' + esc(item.course) + '"';
      return (
        '<button type="button" data-action="' +
        esc(item.action) +
        '"' +
        attrs +
        ">" +
        esc(item.label) +
        "</button>"
      );
    });
    return (
      '<div class="breadcrumb">' +
      parts.join('<span aria-hidden="true">/</span>') +
      (extraHtml || '') +
      "</div>"
    );
  }

  function formatTransfer(bytes) {
    if (!bytes) return "0 B";
    if (bytes < 1024) return Math.round(bytes) + " B";
    if (bytes < 1048576) return (bytes / 1024).toFixed(bytes >= 102400 ? 0 : 1) + " KB";
    return (bytes / 1048576).toFixed(bytes >= 104857600 ? 0 : 1) + " MB";
  }

  function termReviewUrl(courseId, recordingId) {
    return "/api/campus/courses/" + encodeURIComponent(courseId) + "/recordings/" +
      encodeURIComponent(recordingId) + "/term-review";
  }

  function openTermReview(recordingId) {
    var courseId = state.campus.courseId;
    var current = state.termReview;
    if (current.courseId === courseId && current.recordingId === recordingId) {
      state.termReview = { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
      render();
      return;
    }
    var review = { courseId: courseId, recordingId: recordingId, status: "loading", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
    state.termReview = review;
    render();
    return requestJson(termReviewUrl(courseId, recordingId)).then(function (result) {
      if (state.termReview !== review || state.campus.courseId !== courseId) return;
      if (!result.ok || !result.body) {
        review.status = "error";
        review.error = (result.body && result.body.detail) || "暂时无法读取待核对片段，请稍后再试。";
      } else {
        review.status = result.body.status === "needs_review" && result.body.target ? "needs_review" : "none";
        review.target = result.body.target || null;
        review.transcriptSha256 = result.body.transcript_sha256 || "";
        review.videoAvailable = Boolean(result.body.video_available);
      }
      render();
    }).catch(function () {
      if (state.termReview !== review || state.campus.courseId !== courseId) return;
      review.status = "error";
      review.error = "暂时无法读取待核对片段，请稍后再试。";
      render();
    });
  }

  function submitTermReview() {
    var review = state.termReview;
    if (review.status !== "needs_review" || !review.confirmed || !review.target || !review.transcriptSha256) return;
    var lecture = (state.recordings.items || []).concat((state.campus.course && state.campus.course.lessons) || [])
      .find(function (item) { return item.id === review.recordingId; }) || {};
    var target = (lecture.transcript_review && lecture.transcript_review.target) || review.target || {};
    var candidateTerm = target.candidate_term || lecture.candidate_term || "RSA";
    review.status = "submitting";
    review.error = "";
    render();
    return requestJson(termReviewUrl(review.courseId, review.recordingId), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segment_index: review.target.segment_index,
        transcript_sha256: review.transcriptSha256, confirmed_spoken_term: candidateTerm, confirmed: true })
    }).then(function (result) {
      if (state.termReview !== review || state.campus.courseId !== review.courseId) return;
      if (!result.ok || !result.body || result.body.status !== "corrected") {
        review.status = "needs_review";
        review.error = (result.body && result.body.detail) || "修正没有保存，请重新核听后重试。";
        review.confirmed = false;
        render();
        return;
      }
      review.status = "corrected";
      review.confirmed = false;
      return loadCampusCourse(review.courseId);
    }).catch(function () {
      if (state.termReview !== review || state.campus.courseId !== review.courseId) return;
      review.status = "needs_review";
      review.confirmed = false;
      review.error = "修正没有保存，请稍后重试。";
      render();
    });
  }

  function formatAudioPosition(samples) {
    var seconds = Math.floor(Math.max(0, Number(samples) || 0) / 16000);
    var minutes = Math.floor(seconds / 60);
    return minutes + ":" + String(seconds % 60).padStart(2, "0");
  }

  function isNoteProgressUnit(unit) {
    return unit === "划分知识点" || unit === "整理知识点" || unit === "知识点分段" ||
      unit === "段" || unit === "章节" || unit === "笔记段" || unit === "整合笔记";
  }

  function taskUnitName(task) {
    var unit = task.progress_unit;
    if (unit === "bytes") return "未传输";
    if (unit === "segments") return "段录像";
    if (unit === "audio_extract") return "音频";
    if (unit === "audio_parts") return "段音频";
    if (unit === "划分知识点") return "个知识模块";
    if (unit === "整合笔记") return "步整合";
    if (isNoteProgressUnit(unit)) return "个知识点";
    if (unit === "courses") return "门课程";
    return "项";
  }

  function taskRemainingText(task, remaining) {
    if (task.progress_unit === "bytes") return formatTransfer(remaining);
    if (task.progress_unit === "audio_extract") return formatAudioPosition(remaining);
    return String(remaining);
  }

  function recordingTaskProgress(task) {
    var done = Math.max(0, Number(task.progress_done) || 0);
    var total = Math.max(0, Number(task.progress_total) || 0);
    var remaining = Math.max(0, Number(task.progress_remaining) || (total ? total - done : 0));
    var isNoteUnit = isNoteProgressUnit(task.progress_unit);
    var overallPercent = task.kind === "process" && typeof task.progress_percent === "number" &&
      task.progress_percent > 0;
    var percent = overallPercent
      ? (task.state === "done" ? 100 : Math.max(1, Math.min(99, Math.round(task.progress_percent))))
      : (total ? Math.min(100, Math.round(done / total * 100)) : 0);
    var detail = task.progress_unit === "bytes"
      ? (total ? formatTransfer(done) + " / " + formatTransfer(total) : formatTransfer(task.bytes_done))
      : task.progress_unit === "segments" ? done + " / " + total + " 段录像"
      : task.progress_unit === "audio_extract" ? "已处理 " + formatAudioPosition(done) +
        (total ? " / " + formatAudioPosition(total) : "") + " 音频"
      : task.progress_unit === "audio_parts" ? done + " / " + total + " 段音频"
      : task.progress_unit === "整合笔记" ? "正在整合课堂笔记"
      : isNoteUnit ? "已完成 " + done + " / " + total + " 个知识点"
      : task.progress_unit === "courses" ? "已完成 " + done + " / " + total + " 门课程"
      : task.progress_unit === "items" ? "已完成 " + done + " / " + total + " 项"
      : task.progress_unit === "remux" ? "正在合成录像文件" : "请稍候";
    if (total && task.progress_unit !== "remux") {
      detail += " · 剩余 " + taskRemainingText(task, remaining) + " " + taskUnitName(task);
    }
    if (task.speed_bps && (task.progress_unit === "bytes" || task.progress_unit === "segments")) {
      detail += " · " + formatTransfer(task.speed_bps) + "/秒";
    }
    if (task.current_item) detail += " · " + task.current_item;
    return '<div class="recording-progress"><progress class="recording-progress-bar" aria-label="' + esc(campusReadError(task.stage)) + '进度" ' +
      (overallPercent ? 'value="' + esc(percent) + '" max="100"'
        : total ? 'value="' + esc(done) + '" max="' + esc(total) + '"' : '') + '></progress>' +
      '<span>' + esc(total ? percent + "% · " + detail : detail) + '</span></div>';
  }

  function recordingTaskStatus() {
    var task = state.recordings.task;
    if (!task || task.state === "idle") return "";
    if (task.kind === 'sync' && task.state === 'done' && !task.error && !(task.warnings || []).length) return '';
    var activeCourseId = state.campus.courseId;
    if (state.recordings.taskCourseId && state.recordings.taskCourseId !== activeCourseId && task.kind === "process") return "";
    if (state.recordings.taskCourseId && state.recordings.taskCourseId !== activeCourseId &&
        task.kind === "sync" && state.view === "campus-course") return "";
    var activeStage = task.progress_unit === "audio_extract" ? "提取音轨" :
      task.progress_unit === "audio_parts" ? "音频流式转写" : (task.stage || "转写并生成笔记");
    var stepPrefix = task.stage_total
      ? "第 " + Math.max(1, Number(task.stage_index) || 1) + "/" + task.stage_total + " 步 · " : "";
    var message = task.state === "running" ? stepPrefix + "正在" + activeStage + "…" :
      task.state === "done" || task.state === "partial" || task.state === "cancelled" ? task.stage : task.error || "处理未完成，请稍后重试。";
    if ((task.kind === "sync" || task.kind === "summary") && task.state === "done" && Number(task.summary_created) === 0 && Number(task.summary_existing) > 0)
      message = (task.kind === "sync" ? "课程目录更新完成；" : "") + "复用已有课程总结 " + task.summary_existing + " 页，本轮未生成新总结。";
    message = campusReadError(message);
    var progress = task.state === "running" ? recordingTaskProgress(task) : "";
    var warnings = (task.warnings || []).length
      ? '<small class="recording-warning">' + esc((task.warnings || []).slice(0, 2).map(campusReadError).join("；")) +
        ((task.warnings || []).length > 2 ? "…" : "") + '</small>' : "";
    var failure = task.state === "failed" && task.failure_id
      ? '<small class="recording-failure-id">故障编号：' + esc(task.failure_id) + '</small>'
      : "";
    var isModelFail = task.state === "failed" && /未配置|模型配置|API Key|api_key|openai_api_key|dashscope_api_key|faster-whisper|401|402/i.test(task.error || "");
    return '<div class="recording-task" role="status" aria-live="polite"><div class="recording-task-text">' + esc(message) +
      (isModelFail ? button("前往模型配置", "open-models", { small: true, primary: true }) : "") +
      (task.state === "running" && task.kind === "sync" ? button("取消目录更新", "recording-cancel-sync", { small: true, quiet: true }) : "") +
      (task.state === "done" && /^\/api\//.test(task.result_url || '') ? (task.kind === "download"
        ? '<a class="btn btn-small btn-quiet" href="' + esc(task.result_url) + '" download>保存录像到电脑</a>'
        : button(task.kind === "summary" ? "打开课程总结" : task.kind === "sync" ? "查看更新结果" : "查看笔记", "recording-open-result", { small: true, quiet: true, attrs: { "data-url": task.result_url } })) : "") +
      warnings + failure +
      '</div>' + progress + '</div>';
  }

  function backgroundSyncStatus() {
    var task = state.recordings.background;
    if (!task || task.state === "idle") return "";
    if (task.state === 'done' && !task.error && !(task.warnings || []).length &&
        !(task.course_results || []).some(function (row) { return row.error || (row.state && row.state !== 'done'); })) return '';
    var labels = { pending: "等待更新", running: "正在更新", done: "完成", confirmed: "已核对", not_read: "尚未读取", unconfirmed: "尚未核对", failed: "失败", partial: "部分完成", cancelled: "已取消", interrupted: "中断待恢复", unknown: "状态未确认" };
    var list = task.course_results || [];
    var doneCount = list.filter(function (r) { return r.state === "done"; }).length;
    var failedCount = list.filter(function (r) { return r.state && r.state !== "done" && r.state !== "pending" && r.state !== "running"; }).length;
    var rows = list.slice().sort(function (a, b) {
      return Number(a.state === "done") - Number(b.state === "done");
    }).map(function (row) {
      var course = state.campus.courses.find(function (item) { return item.id === row.course_id; });
      var rowDelta = formatDeltaSummary(row.delta);
      return '<li data-sync-course="' + esc(row.course_id) + '" data-sync-state="' + esc(row.state) + '"><strong>' + esc(row.title || course && course.title || row.course_id) + '</strong> · <span class="sync-state-tag">' + esc(labels[row.state] || "状态未确认") + '</span> ' +
        '<small> · 教学网：' + esc(labels[row.metadata_state] || "状态未确认") + '</small>' +
        (rowDelta ? '<small class="sync-delta-tag"> · ' + esc(rowDelta) + '</small>' : row.unchanged ? '<small> · 已是最新</small>' : '') +
        (row.summary_created ? '<small>本轮新建总结 ' + esc(row.summary_created) + ' 页</small>' : '') +
        (row.summary_existing ? '<small>复用已有总结 ' + esc(row.summary_existing) + ' 页</small>' : '') +
        (row.error ? '<p role="alert">' + esc(campusReadError(row.error)) + '</p>' : '') +
        (row.failure_id ? '<small class="recording-failure-id">故障编号：' + esc(row.failure_id) + '</small>' : '') + '</li>';
    }).join('');
    return '<section class="panel background-sync-banner" data-background-sync data-background-state="' + esc(task.state) + '" role="status" aria-live="polite">' +
      '<div class="background-sync-bar"><div class="background-sync-main"><span class="background-sync-badge">' +
      (task.state === "done" ? "✓ 已更新" : task.state === "running" ? "正在更新" : "后台更新") + '</span>' +
      '<p class="background-sync-stage" data-background-stage>' + esc(campusReadError(task.stage || labels[task.state])) + '</p>' +
      (list.length > 0 && task.state !== "running" ? '<span class="background-sync-counts">已完成 ' + doneCount + ' / ' + list.length + ' 门' + (failedCount ? ' · ' + failedCount + ' 门未完成' : '') + '</span>' : '') +
      '</div><div class="background-sync-actions">' +
      (task.state === "running" ? button("取消后台更新", "recording-cancel-background", { small: true, quiet: true, disabled: state.recordings.backgroundPending }) : '') +
      (['failed', 'partial', 'cancelled', 'interrupted'].indexOf(task.state) >= 0 ? button("恢复未完成课程", "recording-resume-background", { small: true, disabled: state.recordings.backgroundPending }) : '') +
      '</div></div>' + (task.state === "running" ? recordingTaskProgress(task) : '') +
      (task.error ? '<p role="alert" class="background-sync-error">' + esc(campusReadError(task.error)) + '</p>' : '') +
      (list.length ? '<details class="background-sync-details" data-sync-details data-persistent-disclosure="background-sync-details"><summary>' +
        (failedCount ? '查看未完成与各课明细 (' + failedCount + ' 门待处理 / 共 ' + list.length + ' 门)' : '查看各课更新明细 (' + list.length + ' 门)') +
        '</summary><ul class="background-sync-list">' + rows + '</ul></details>' : '') + '</section>';
  }

  function updateBackgroundSync(resume) {
    if (state.recordings.backgroundPending) return Promise.resolve();
    var epoch = accountQuotaEpoch;
    state.recordings.backgroundPending = true;
    render();
    return requestJson(resume ? "/api/recordings/sync" : "/api/recordings/cancel-sync", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(resume ? { scope: "background", resume: true } : { scope: "background" })
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body || result.body.scope !== "background") return recordingError(campusReadError(result.body && result.body.detail));
      state.recordings.background = result.body;
    }).catch(function () {
      if (epoch === accountQuotaEpoch) recordingError("后台更新结果未确认；已有内容保留，请读取任务状态后再恢复。");
    }).finally(function () {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.backgroundPending = false;
      render();
    });
  }

  function captureCredentialFields(ids) {
    var draft = { fields: {}, focus: null };
    ids.forEach(function (id) {
      var field = document.getElementById(id);
      if (!field) return;
      draft.fields[id] = field.value;
      if (document.activeElement === field) draft.focus = { id: id, start: field.selectionStart, end: field.selectionEnd };
    });
    return draft;
  }

  function restoreCredentialFields(draft) {
    if (!draft) return;
    Object.keys(draft.fields).forEach(function (id) {
      var field = document.getElementById(id);
      if (field) field.value = draft.fields[id];
    });
    if (draft.focus) {
      var field = document.getElementById(draft.focus.id);
      if (field) {
        field.focus({ preventScroll: true });
        if (draft.focus.start !== null) field.setSelectionRange(draft.focus.start, draft.focus.end);
      }
    }
  }
