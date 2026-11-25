  var imeComposingTarget = null;
  var renderDeferredDuringIme = false;

  function render() {
    if (imeComposingTarget && document.activeElement === imeComposingTarget && document.contains(imeComposingTarget)) {
      renderDeferredDuringIme = true;
      return;
    }
    renderDeferredDuringIme = false;
    var focusedLessonFilter = document.activeElement && document.activeElement.matches('[data-campus-lesson-filter]') ?
      { start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd,
        direction: document.activeElement.selectionDirection } : null;
    var focusedCampusSearch = document.activeElement && document.activeElement.matches('[data-campus-search]') ?
      { start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd,
        direction: document.activeElement.selectionDirection } : null;
    var focusedReadingSearch = document.activeElement && document.activeElement.matches('[data-reader-search], [data-campus-material-filter]') ?
      { key: document.activeElement.dataset.readerSearch, start: document.activeElement.selectionStart,
        end: document.activeElement.selectionEnd, direction: document.activeElement.selectionDirection } : null;
    var focusedModelInput = (document.activeElement &&
      document.activeElement.closest &&
      document.activeElement.closest("#models-config-form") &&
      document.activeElement.id)
      ? {
          id: document.activeElement.id,
          start: typeof document.activeElement.selectionStart === "number" ? document.activeElement.selectionStart : null,
          end: typeof document.activeElement.selectionEnd === "number" ? document.activeElement.selectionEnd : null,
          direction: document.activeElement.selectionDirection
        }
      : null;
    if (state.view === "models" && typeof captureModelFormToDraft === "function") {
      captureModelFormToDraft();
    }
    var sameAccount = renderedAccountEpoch === accountQuotaEpoch &&
      renderedAccountEmail === accountEmail();
    var credentialDraft = sameAccount ? captureCredentialFields(["recording-username", "recording-password"]) : null;
    var disclosures = {};
    document.querySelectorAll('[data-persistent-disclosure]').forEach(function (element) {
      disclosures[element.dataset.persistentDisclosure] = element.open;
    });
    var openSettings = document.querySelector(".campus-settings[open] > summary");
    var settingsTitle = openSettings ? openSettings.textContent : "";
    var sidebar = document.querySelector(".sidebar");
    var sidebarScroll = sidebar ? sidebar.scrollTop : 0;
    var main = document.querySelector(".main");
    var mainScroll = main ? main.scrollTop : 0;
    var windowScroll = window.scrollY || window.pageYOffset || 0;
    var preservedReaders = {};
    if (sameAccount) {
      document.querySelectorAll(".lecture-note-reader[data-reader-key]").forEach(function (readerEl) {
        var rKey = readerEl.dataset.readerKey;
        if (!rKey) return;
        preservedReaders[rKey] = {
          readerEl: readerEl,
          readerSig: readerEl._readerSig || "",
          markdownEl: readerEl.querySelector(".study-markdown"),
          markdownSig: readerEl._markdownSig || ""
        };
      });
    }
    var activeField = document.activeElement && document.activeElement.closest &&
      document.activeElement.closest("#assignment-prepare-form") ? document.activeElement.name : "";
    var caret = activeField && typeof document.activeElement.selectionStart === "number" ?
      document.activeElement.selectionStart : null;
    var screen;
    if (state.view === "models") screen = modelSettingsScreen();
    else if (state.view === "mcp-hub") screen = mcpHubScreen();
    else if (state.view === "campus-course") screen = campusCourseScreen();
    else if (state.view === "campus-account") screen = campusDashboardScreen();
    else if (state.view === "dashboard") screen = dashboardScreen();
    else screen = dashboardScreen();
    app.innerHTML = shell(screen);
    document.querySelectorAll(".lecture-note-reader[data-reader-key]").forEach(function (nextReaderEl) {
      var rKey = nextReaderEl.dataset.readerKey;
      var rItem = rKey ? readingPreviews[rKey] : null;
      var isLecture = rKey ? !/:summary$/.test(rKey) : false;
      var nextReaderSig = previewReaderSignature(rKey, rItem, isLecture);
      var nextMarkdownSig = previewMarkdownSignature(rItem);
      var prev = rKey && preservedReaders[rKey];
      if (prev && nextReaderSig && prev.readerSig === nextReaderSig &&
          !(focusedReadingSearch && focusedReadingSearch.key === rKey)) {
        prev.readerEl._readerSig = nextReaderSig;
        prev.readerEl._markdownSig = nextMarkdownSig;
        nextReaderEl.replaceWith(prev.readerEl);
        return;
      }
      if (prev && prev.markdownEl && nextMarkdownSig && prev.markdownSig === nextMarkdownSig) {
        var nextMd = nextReaderEl.querySelector(".study-markdown");
        if (nextMd) nextMd.replaceWith(prev.markdownEl);
      }
      nextReaderEl._readerSig = nextReaderSig;
      nextReaderEl._markdownSig = nextMarkdownSig;
    });
    hydrateMermaidDiagrams();
    renderedAccountEpoch = accountQuotaEpoch;
    renderedAccountEmail = accountEmail();
    restoreCredentialFields(credentialDraft);
    document.querySelectorAll('[data-batch-indeterminate]').forEach(function (element) { element.indeterminate = true; });
    document.querySelectorAll('[data-persistent-disclosure]').forEach(function (element) {
      var key = element.dataset.persistentDisclosure;
      if (Object.prototype.hasOwnProperty.call(disclosures, key)) element.open = disclosures[key];
    });
    var settings = document.querySelector(".campus-settings");
    if (settings && settingsTitle && settings.querySelector("summary").textContent === settingsTitle) settings.open = true;
    sidebar = document.querySelector(".sidebar");
    if (sidebar && sameAccount) sidebar.scrollTop = sidebarScroll;
    main = document.querySelector(".main");
    if (main) main.scrollTop = mainScroll;
    if (windowScroll) window.scrollTo(0, windowScroll);
    if (focusedModelInput) {
      var modelEl = document.getElementById(focusedModelInput.id);
      if (modelEl) {
        modelEl.focus({ preventScroll: true });
        if (focusedModelInput.start !== null && modelEl.setSelectionRange) {
          try {
            modelEl.setSelectionRange(focusedModelInput.start, focusedModelInput.end, focusedModelInput.direction);
          } catch (_) {}
        }
      }
    }
    if (activeField) {
      var replacement = document.querySelector('#assignment-prepare-form [name="' + activeField + '"]');
      if (replacement) {
        replacement.focus({ preventScroll: true });
        if (caret !== null && replacement.setSelectionRange) replacement.setSelectionRange(caret, caret);
      }
    }
    if (focusedLessonFilter) {
      var lessonFilter = document.querySelector('[data-campus-lesson-filter]');
      if (lessonFilter) {
        lessonFilter.focus({ preventScroll: true });
        lessonFilter.setSelectionRange(focusedLessonFilter.start, focusedLessonFilter.end, focusedLessonFilter.direction);
      }
    }
    if (focusedCampusSearch) {
      var campusSearch = document.querySelector('[data-campus-search]');
      if (campusSearch) {
        campusSearch.focus({ preventScroll: true });
        campusSearch.setSelectionRange(focusedCampusSearch.start, focusedCampusSearch.end, focusedCampusSearch.direction);
      }
    }
    if (focusedReadingSearch) {
      var readingSearch = Array.from(document.querySelectorAll('[data-reader-search], [data-campus-material-filter]')).find(function (input) {
        return input.dataset.readerSearch === focusedReadingSearch.key;
      });
      if (readingSearch) {
        readingSearch.focus({ preventScroll: true });
        readingSearch.setSelectionRange(focusedReadingSearch.start, focusedReadingSearch.end, focusedReadingSearch.direction);
      }
    }
  }

  function showToast(message) {
    toastEl.textContent = message;
    toastEl.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastEl.classList.remove("show");
    }, 2800);
  }

  function resetMcpHubCache() {
    mcpHub.status = null;
    mcpHub.lastFetchedAt = 0;
    mcpHub.lastJson = '';
    mcpHub.loading = false;
  }

  /* -- actions: BYOK model settings -------------------------------------- */

  function loadModelSettings() {
    if (!state.models) {
      state.models = { loading: false, loaded: false, saving: false, testing: "", testResult: null, error: "", data: null, draft: null };
    }
    state.models.loading = true;
    state.models.error = "";
    return requestJson("/api/settings/models").then(function (result) {
      state.models.loading = false;
      if (!result.ok || !result.body) {
        state.models.error = (result.body && result.body.detail) || "读取本机模型配置失败。";
        render();
        return;
      }
      state.models.data = result.body;
      state.models.loaded = true;
      if (!state.models.draft) {
        ensureModelDraft(true);
      }
      render();
    }).catch(function () {
      state.models.loading = false;
      state.models.error = "读取本机模型配置失败，请检查面板服务是否正常运行。";
      render();
    });
  }

  function applyModelPreset(presetId) {
    var draft = captureModelFormToDraft();
    var presets = (state.models && state.models.data && state.models.data.presets) || [];
    var preset = presets.find(function (p) { return p.id === presetId; });
    if (!preset) return;
    draft.openai_base_url = preset.base_url || "";
    if (preset.default_model) {
      draft.notes_model = preset.default_model;
    }
    if (preset.default_vision_model && !String(draft.vision_model || "").trim()) {
      draft.vision_model = preset.default_vision_model;
    }
    if (state.models) state.models.testResult = null;
    render();
    showToast("已填充 " + (preset.label || preset.id) + " 预设参数，填写 API Key 后点击「保存模型配置并应用」即可生效。");
  }

  function selectAsrBackend(backend) {
    var draft = captureModelFormToDraft();
    var normalized = String(backend || "openai").toLowerCase();
    if (["openai", "dashscope", "local"].indexOf(normalized) < 0) normalized = "openai";
    draft.transcription_backend = normalized;
    if (state.models) state.models.testResult = null;
    render();
  }

  function clearModelKey(keyField) {
    var draft = captureModelFormToDraft();
    if (["openai_api_key", "vision_api_key", "asr_api_key", "dashscope_api_key"].indexOf(keyField) < 0) return;
    draft[keyField] = "";
    draft["clear_" + keyField] = true;
    render();
  }

  function buildModelUpdatePayload(draft) {
    var payload = {
      openai_base_url: String(draft.openai_base_url || "").trim(),
      notes_model: String(draft.notes_model || "").trim(),
      refine_model: String(draft.refine_model || "").trim(),
      notes_strategy: draft.notes_strategy === "transcript" ? "transcript" : "topics",
      vision_enabled: Boolean(draft.vision_enabled),
      vision_base_url: String(draft.vision_base_url || "").trim(),
      vision_model: String(draft.vision_model || "").trim(),
      transcription_backend: String(draft.transcription_backend || "openai").toLowerCase(),
      asr_base_url: String(draft.asr_base_url || "").trim(),
      asr_model: String(draft.asr_model || "whisper-1").trim(),
      whisper_model: String(draft.whisper_model || "small").trim(),
      whisper_device: String(draft.whisper_device || "auto").trim(),
      whisper_compute_type: String(draft.whisper_compute_type || "auto").trim()
    };
    ["openai_api_key", "vision_api_key", "asr_api_key", "dashscope_api_key"].forEach(function (field) {
      var secret = String(draft[field] || "").trim();
      var clearFlag = "clear_" + field;
      if (secret) {
        payload[field] = secret;
        payload[clearFlag] = false;
      } else if (draft[clearFlag]) {
        payload[clearFlag] = true;
      }
    });
    return payload;
  }

  function saveModelSettings() {
    if (!state.models || state.models.saving) return Promise.resolve();
    var draft = captureModelFormToDraft();
    state.models.saving = true;
    state.models.error = "";
    render();
    return requestJson("/api/settings/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildModelUpdatePayload(draft))
    }).then(function (result) {
      state.models.saving = false;
      if (!result.ok || !result.body) {
        state.models.error = (result.body && result.body.detail) || ((COPY.models && COPY.models.save_failed) || "保存模型配置失败。");
        showToast(state.models.error);
        render();
        return;
      }
      state.models.data = result.body;
      state.models.loaded = true;
      ensureModelDraft(true);
      showToast((COPY.models && COPY.models.save_success) || "模型配置已保存到本机 .env 并立即生效。");
      render();
    }).catch(function () {
      state.models.saving = false;
      state.models.error = (COPY.models && COPY.models.save_failed) || "保存模型配置失败，请检查网络连接。";
      showToast(state.models.error);
      render();
    });
  }

  function testModelConnection(target) {
    if (!state.models || state.models.testing) return Promise.resolve();
    var cleanTarget = String(target || "llm").toLowerCase();
    var draft = captureModelFormToDraft();
    state.models.testing = cleanTarget;
    state.models.testResult = null;
    render();
    return requestJson("/api/settings/models/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target: cleanTarget,
        overrides: buildModelUpdatePayload(draft)
      })
    }).then(function (result) {
      state.models.testing = "";
      if (!result.body) {
        state.models.testResult = {
          ok: false,
          target: cleanTarget,
          message: (COPY.models && COPY.models.test_failed) || "连接测试失败"
        };
      } else {
        state.models.testResult = Object.assign({}, result.body, { target: cleanTarget });
      }
      showToast(state.models.testResult.message || state.models.testResult.detail || (state.models.testResult.ok ? "测试通过" : "测试未通过"));
      render();
    }).catch(function () {
      state.models.testing = "";
      state.models.testResult = { ok: false, target: cleanTarget, message: "无法连接到面板测试接口。" };
      showToast(state.models.testResult.message);
      render();
    });
  }

  /* -- navigation -------------------------------------------------------- */

  function openDashboard() {

    state.view = "dashboard";

    render();
  }

  function recordingError(message, courseId) {
    state.recordings.error = campusReadError(message || "操作没有完成，请稍后重试。");
    state.recordings.errorCourseId = courseId === undefined ?
      (state.campus.courseId || "") : courseId;
    render();
  }

  function recordingCredentialsForm() {
    var copy = COPY.course.recordings;
    var pending = Boolean(state.recordings.credentialsPending);
    var readonly = pending ? " readonly" : "";
    return '<form id="recording-credentials-form" class="recording-credentials" aria-busy="' + pending + '">' +
      '<div class="campus-trust-callout campus-trust-subtle" data-campus-security-notice style="flex-basis:100%;">' +
      '<strong>🔒 本机密钥加密保存 · 不上传外部账号平台</strong> · ' +
      '<span>凭据仅用于本机直连北大教学网并同步课程与回放，随时可一键清除。</span>' +
      '</div>' +
      '<label for="recording-username">' + esc(copy.username) + '<input id="recording-username" autocomplete="off" data-lpignore="true" placeholder="学号（如 230001xxxx，勿填邮箱）" required' + readonly + '></label>' +
      '<label for="recording-password">' + esc(copy.password) + '<input id="recording-password" type="password" autocomplete="new-password" data-lpignore="true" placeholder="北大门户（IAAA）密码" required' + readonly + '></label>' +
      '<div class="credentials-actions">' + button(pending ? copy.saving : copy.save, "recording-save-credentials", {
        primary: true, small: true, type: "submit", disabled: pending, attrs: { "aria-busy": String(pending) }
      }) + (state.campus.configured ? button("取消", "recording-cancel-credentials", { small: true, quiet: true, disabled: pending }) : "") + '</div>' +
      campusOverwriteExportHint(Boolean(state.recordings.editingCredentials)) +
      (state.recordings.credentialsError ? '<p role="alert" class="form-note">' + esc(state.recordings.credentialsError) + '</p>' : "") + '</form>';
  }

  function clearRecordingCredentials() {
    if (state.recordings.credentialsPending) return Promise.resolve();
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/credentials", { method: "DELETE" }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.configured = false;
      state.campus.configured = false;
      state.recordings.editingCredentials = false;
      showToast((result.body && result.body.message) || "已清除本地保存的教学网凭据并断开连接。");
      render();
    });
  }

  function saveRecordingCredentials() {
    if (state.recordings.credentialsPending) return Promise.resolve();
    var user = document.getElementById("recording-username");
    var password = document.getElementById("recording-password");
    var form = document.getElementById("recording-credentials-form");
    if (!form || !form.reportValidity()) return Promise.resolve();
    var rawUser = user.value.trim();
    if (/@(?:stu\.|alumni\.)?pku\.edu\.cn$/i.test(rawUser)) {
      rawUser = rawUser.split("@")[0].trim();
      user.value = rawUser;
    }
    if (!rawUser) {
      state.recordings.credentialsError = "请输入教学网账号。";
      render();
      document.getElementById("recording-username").focus();
      return Promise.resolve();
    }
    var epoch = accountQuotaEpoch;
    var body = JSON.stringify({
      username: rawUser,
      password: password.value,
      persist_mode: "encrypted_vault"
    });
    state.recordings.credentialsPending = true;
    state.recordings.credentialsError = "";
    render();
    return requestJson("/api/recordings/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body || result.body.verified !== true) {
        state.recordings.credentialsError = (result.body && result.body.detail) || "教学网登录尚未验证，请稍后重试。";
        return;
      }
      state.recordings.configured = true;
      state.campus.configured = true;
      state.campus.cookieKeyMissing = false;
      state.recordings.editingCredentials = false;
      state.recordings.error = "";
      password.value = "";
      loadCampusCourses().then(function () {
        if (epoch !== accountQuotaEpoch || window.__uiTest) return;
        window.setTimeout(function () {
          if (epoch !== accountQuotaEpoch) return;
          if ((!state.campus.courses || state.campus.courses.length === 0) &&
              !state.recordings.syncPending && !state.recordings.backgroundPending &&
              state.recordings.task.state === "idle" && state.recordings.background.state === "idle") {
            syncRecordingIndex();
          }
        }, 250);
      });
      showToast(COPY.course.recordings.verified);
    }).catch(function () {
      if (epoch === accountQuotaEpoch) state.recordings.credentialsError = "验证连接中断，请检查网络后重试。";
    }).finally(function () {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.credentialsPending = false;
      render();
    });
  }

  function syncRecordingIndex(courseId) {
    if (courseId ? courseTaskBusy(courseId) : state.recordings.backgroundPending ||
        state.recordings.background.state === "running" || state.recordings.syncPending || state.recordings.task.state === "running") return Promise.resolve();
    var epoch = accountQuotaEpoch;
    var taskCourseId = courseId ? (courseId) : "";
    var options = { method: "POST" };
    if (courseId) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({ course_id: courseId });
    }
    state.recordings.syncPending = true;
    render();
    return requestJson("/api/recordings/sync", options).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.syncPending = false;
      if (!result.ok) return recordingError((result.body && result.body.detail) || "录像目录同步失败。", taskCourseId);
      if (result.body.scope === "background") state.recordings.background = result.body;
      else {
        state.recordings.task = result.body;
        state.recordings.taskCourseId = taskCourseId;
      }
      state.recordings.error = "";
      if (result.body.state === 'done') {
        showToast('课程目录已更新');
        return loadCampusCourses().then(function () {
          if (epoch !== accountQuotaEpoch) return;
          if (state.view === 'campus-course') return loadCampusCourse(state.campus.courseId);
          render();
        });
      }
      render();
    }).catch(function () {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.syncPending = false;
      recordingError("录像目录同步失败。", taskCourseId);
    });
  }

  function cancelRecordingSync() {
    return requestJson("/api/recordings/cancel-sync", { method: "POST" }).then(function (result) {
      if (!result.ok) return showToast((result.body && result.body.detail) || "无法取消目录更新。");
      state.recordings.task = result.body;
      render();
    }).catch(function () { showToast("无法取消目录更新。"); });
  }

  function recordingNoteStyle(recordingId, disabled) {
    return "";
  }

  function openNotionPreview(url, onFailure) {
    return requestJson("/api/open-notion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url })
    }).then(function (result) {
      if (!result.ok || !result.body || !result.body.opened) throw new Error("browser open failed");
      if (["library", "course", "lecture"].indexOf(state.view) >= 0) openDashboard();
      showToast(COPY.launch.opened_external);
      return true;
    }).catch(function () {
      if (onFailure) onFailure();
      else showToast(COPY.launch.failed);
      return false;
    });
  }

  function disconnectNotion() {
    return requestJson("/api/connection/disconnect", { method: "POST" }).then(
      function (result) {
        if (result.ok && result.body) state.connection = result.body;

        render();
        if (!state.connection.connected) showToast(COPY.connection.disconnect_toast);
      }
    );
  }

  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function finishConnect() {
    if (state.connection.connected) {
      return loadNotionParents().then(function () {
        render();
        showToast(COPY.connection.reconnect_toast);
      });
    }
    render();
    if (state.connection.error) showToast(state.connection.error);
    return Promise.resolve();
  }

  function pollConnection() {
    stopPolling();
    var attempts = 0;
    pollTimer = window.setInterval(function () {
      attempts += 1;
      requestJson("/api/connection").then(function (result) {
        if (result.ok && result.body) state.connection = result.body;
        if (state.connection.state !== "connecting") {
          stopPolling();
          finishConnect();
        } else if (attempts >= POLL_MAX_ATTEMPTS) {
          stopPolling();
          showToast(COPY.connection.timeout);
        }
      }).catch(function () {
        if (attempts >= POLL_MAX_ATTEMPTS) {
          stopPolling();
          showToast(COPY.connection.failed);
        }
      });
    }, POLL_INTERVAL_MS);
  }

  function connectNotion() {
    return requestJson("/api/connection/connect", { method: "POST" }).then(
      function (result) {
        if (result.status === 409) {
          showToast(COPY.connection.busy);
          return;
        }
        if (!result.ok) {
          showToast((result.body && result.body.detail) || COPY.connection.failed);
          return;
        }
        if (result.body) state.connection = result.body;
        if (state.connection.connected || state.connection.state !== "connecting") return finishConnect();
        render();
        pollConnection();
      }
    ).catch(function () {
      showToast(COPY.connection.failed);
    });
  }

  function downloadAssignmentDraft() {
    var submission = state.campus.submission;
    var draft = submission.draft;
    var courseId = state.campus.courseId;
    var contentId = state.campus.submission.selectedId;
    if (!draft || state.campus.submission.downloadBusy) return;
    state.campus.submission.downloadBusy = true;
    var query = '?sha256=' + encodeURIComponent(draft.sha256) + '&course_id=' + encodeURIComponent(courseId) +
      '&content_id=' + encodeURIComponent(contentId);
    return requestJson('/api/campus/submission-drafts/' + encodeURIComponent(draft.draft_id) + '/check' + query).then(function (result) {
      if (state.campus.submission !== submission || state.campus.courseId !== courseId ||
          submission.selectedId !== contentId || submission.draft !== draft) return;
      if (!result.ok || !result.body || result.body.valid !== true || result.body.draft_id !== draft.draft_id ||
          result.body.sha256 !== draft.sha256) {
        state.campus.submission.error = (result.body && result.body.detail) || '草稿未通过当前预览核验，请重新准备。';
        render(); return;
      }
      if (window.__TAURI_INTERNALS__ && typeof window.__TAURI_INTERNALS__.invoke === 'function') {
        submission.saveStatus = { state: 'choosing', message: '请选择文件保存位置…' };
        render();
        return window.__TAURI_INTERNALS__.invoke('save_reviewed_draft', {
          draftId: draft.draft_id, sha256: draft.sha256, courseId: courseId,
          contentId: contentId, archiveName: draft.archive_name
        }).then(function (saved) {
          if (state.campus.submission !== submission || submission.draft !== draft) return;
          if (saved && saved.state === 'cancelled') {
            submission.saveStatus = { state: 'cancelled', message: '已取消保存，草稿仍可查看。' };
          } else if (saved && saved.state === 'saved' && saved.sha256 === draft.sha256 && saved.filename) {
            submission.saveStatus = { state: 'saved', sha256: saved.sha256, message: '文件已保存：' + saved.filename };
          } else {
            submission.saveStatus = null;
            submission.error = '保存结果未通过核验，请检查文件后重试。';
          }
          render();
        }).catch(function (error) {
          if (state.campus.submission !== submission || submission.draft !== draft) return;
          submission.error = typeof error === 'string' ? error : '文件未能保存，请检查磁盘空间或目录权限后重试。';
          submission.saveStatus = null;
          render();
        });
      }
      var link = document.createElement('a');
      link.href = draft.download_url + '?sha256=' + encodeURIComponent(draft.sha256);
      link.download = draft.archive_name;
      document.body.appendChild(link); link.click(); link.remove();
    }).catch(function () {
      if (state.campus.submission !== submission) return;
      submission.error = '草稿暂时无法核验，请稍后重试或重新准备。'; render();
    }).finally(function () { submission.downloadBusy = false; });
  }

  function openImageLightbox(img) {
    var existing = document.querySelector('.note-lightbox');
    if (existing) existing.remove();
    var dialog = document.createElement('dialog');
    dialog.className = 'note-lightbox';
    dialog.setAttribute('aria-label', img.alt || '课堂图片预览');
    var picture = document.createElement('img');
    picture.src = img.getAttribute('data-full-src') || img.src;
    picture.alt = img.alt || '';
    var parentCard = img.closest && img.closest('.note-visual-card');
    if (parentCard) {
      if (parentCard.classList.contains('is-board-enhanced')) picture.classList.add('is-board-enhanced');
      if (parentCard.classList.contains('is-board-inverted')) picture.classList.add('is-board-inverted');
    }
    var caption = document.createElement('p');
    caption.textContent = img.alt || '';
    dialog.append(picture, caption);
    function close() {
      if (dialog.open) dialog.close();
      dialog.remove();
    }
    dialog.addEventListener('click', close);
    dialog.addEventListener('close', close);
    document.body.appendChild(dialog);
    try { dialog.showModal(); } catch (_) { close(); }
  }

  document.addEventListener("error", function (event) {
    var img = event.target;
    if (!img || img.tagName !== "IMG" || !img.hasAttribute("data-zoomable-image")) return;
    var step = Number(img.getAttribute("data-retry-step") || 0);
    var fullSrc = img.getAttribute("data-full-src") || "";
    var currentSrc = img.getAttribute("src") || "";
    if (step === 0 && fullSrc && currentSrc !== fullSrc) {
      img.setAttribute("data-retry-step", "1");
      img.setAttribute("src", fullSrc);
      return;
    }
    if (step < 2) {
      var baseSrc = fullSrc || currentSrc.replace(/[?&]_retry=\d+/, "");
      img.setAttribute("data-retry-step", String(step + 1));
      window.setTimeout(function () {
        if (!img.isConnected) return;
        img.setAttribute("src", baseSrc + (baseSrc.indexOf("?") >= 0 ? "&" : "?") + "_retry=" + (step + 1));
      }, 900);
      return;
    }
    var card = img.closest && img.closest(".note-visual-card");
    if (card) card.classList.add("is-image-error");
  }, true);

  document.addEventListener("load", function (event) {
    var img = event.target;
    if (!img || img.tagName !== "IMG" || !img.hasAttribute("data-zoomable-image")) return;
    var card = img.closest && img.closest(".note-visual-card");
    if (card) card.classList.remove("is-image-error");
  }, true);

  function loadMcpStatus(options) {
    if (!state.activated) return Promise.resolve();
    var force = Boolean(options && options.force);
    var silent = Boolean(options && options.silent);
    var mcpEpoch = accountQuotaEpoch;
    var hadStatus = Boolean(mcpHub.status);
    if (mcpHub.loading && mcpHub.loadingEpoch === mcpEpoch) return Promise.resolve();
    if (!force && hadStatus && (Date.now() - (mcpHub.lastFetchedAt || 0)) < 60000) return Promise.resolve();
    mcpHub.loading = true;
    mcpHub.loadingEpoch = mcpEpoch;
    return requestJson('/api/mcp/status').then(function (result) {
      if (mcpEpoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body || !Array.isArray(result.body.hosts)) {
        throw new Error('本地知识库暂时无法读取，请重试。');
      }
      var nextJson = '';
      try { nextJson = JSON.stringify(result.body); } catch (_) { nextJson = ''; }
      var prevJson = mcpHub.lastJson || '';
      mcpHub.status = result.body;
      mcpHub.lastFetchedAt = Date.now();
      mcpHub.lastJson = nextJson;
      if (state.view === 'mcp-hub' && (!hadStatus || !nextJson || nextJson !== prevJson)) render();
    }).catch(function (error) {
      if (mcpEpoch !== accountQuotaEpoch) return;
      if (!silent && !mcpHub.status) {
        mcpHub.error = error.message;
        if (state.view === 'mcp-hub') render();
      }
    }).finally(function () {
      if (mcpEpoch === accountQuotaEpoch && mcpHub.loadingEpoch === mcpEpoch) mcpHub.loading = false;
    });
  }

  function prewarmMcpFromEvent(event) {
    if (!state.activated) return;
    var trigger = event.target && event.target.closest && event.target.closest('[data-action="open-mcp-hub"]');
    if (trigger) loadMcpStatus({ silent: true });
  }
  app.addEventListener("mouseover", prewarmMcpFromEvent);
  app.addEventListener("focusin", prewarmMcpFromEvent);

  document.addEventListener("click", function (event) {
    var boardToggle = event.target.closest && event.target.closest('[data-board-toggle]');
    if (boardToggle) {
      event.preventDefault();
      event.stopPropagation();
      var fig = boardToggle.closest('.note-visual-card');
      if (fig) {
        var mode = boardToggle.dataset.boardToggle;
        var clsName = mode === 'invert' ? 'is-board-inverted' : 'is-board-enhanced';
        var active = fig.classList.toggle(clsName);
        boardToggle.classList.toggle('is-active', active);
        boardToggle.setAttribute('aria-pressed', active ? 'true' : 'false');
      }
      return;
    }
    var zoomImg = event.target.closest && event.target.closest('img[data-zoomable-image]');
    if (zoomImg) {
      var errCard = zoomImg.closest && zoomImg.closest('.note-visual-card.is-image-error');
      if (errCard) {
        errCard.classList.remove('is-image-error');
        zoomImg.setAttribute('data-retry-step', '0');
        var reloadBase = zoomImg.getAttribute('data-full-src') || (zoomImg.getAttribute('src') || '').replace(/[?&]_retry=\d+/, '');
        if (reloadBase) {
          zoomImg.setAttribute('src', reloadBase + (reloadBase.indexOf('?') >= 0 ? '&' : '?') + '_retry=' + Date.now());
        }
        return;
      }
      openImageLightbox(zoomImg);
      return;
    }
    if (!event.target.closest('details, summary, a, button, input, select, textarea, label')) {
      var courseCard = event.target.closest('[data-course-card]');
      var lessonCard = event.target.closest('[data-lesson-card]');
      if (courseCard) { openCampusCourse(courseCard.dataset.courseCard); return; }
      if (lessonCard) { openCampusLesson(lessonCard.dataset.lessonCard); return; }
    }
    var target = event.target.closest("[data-action]");
    if (!target || target.disabled) return;
    var action = target.dataset.action;
    var mobileMenu = target.closest('.mobile-account-menu');
    if (mobileMenu && ["open-dashboard", "open-models", "open-mcp-hub", "campus-account-settings", "open-campus-setup"].indexOf(action) >= 0) {
      mobileMenu.open = false;
    }
    if (action === 'campus-download-draft') { event.preventDefault(); downloadAssignmentDraft(); return; }
    if (action === "recording-save-credentials") return;
    else if (action === "open-models") {
      state.view = "models";
      render();
      if (!state.models || !state.models.loaded) loadModelSettings();
      if (window.scrollTo) window.scrollTo(0, 0);
    }
    else if (action === "apply-model-preset") {
      applyModelPreset(target.dataset.preset);
    }
    else if (action === "select-asr-backend") {
      selectAsrBackend(target.dataset.backend);
    }
    else if (action === "clear-model-key") {
      clearModelKey(target.dataset.keyField);
    }
    else if (action === "test-model-connection") {
      testModelConnection(target.dataset.target);
    }
    else if (action === "save-model-settings") {
      saveModelSettings();
    }
    else if (action === "request-update") requestUpdate(target.dataset.version);
    else if (action === "revoke-notion") disconnectNotion();
    else if (action === "connect-notion") connectNotion();
    else if (action === "open-dashboard") openDashboard();
    else if (action === "toggle-course-track") {
      toggleCourseAutoTrack(target.dataset.course);
    }
    else if (action === "toggle-auto-track-panel") {
      state.autoTrack.open = !state.autoTrack.open;
      render();
    }
    else if (action === "auto-track-check-now") {
      checkAutoTrackNow();
    }
    else if (action === "auto-track-select-recordings") {
      var recCourseIds = (state.campus.courses || []).filter(function (c) { return (Number(c.recordings) || 0) > 0; }).map(function (c) { return c.id; });
      updateAutoTrackSettings({ tracked_course_ids: recCourseIds, enabled: recCourseIds.length > 0 }, "已追踪全部有课堂回放的课程（共 " + recCourseIds.length + " 门）。");
    }
    else if (action === "auto-track-select-all") {
      var allCourseIds = (state.campus.courses || []).map(function (c) { return c.id; });
      updateAutoTrackSettings({ tracked_course_ids: allCourseIds, enabled: allCourseIds.length > 0 }, "已追踪全部课程（共 " + allCourseIds.length + " 门）。");
    }
    else if (action === "auto-track-clear-all") {
      updateAutoTrackSettings({ tracked_course_ids: [], enabled: false }, "已清空自动追踪课程。");
    }
    else if (action === 'toggle-batch-organize') {
      if (state.batchOrganize && state.batchOrganize.open) { state.batchOrganize.open = false; render(); }
      else openBatchOrganize('');
    }
    else if (action === 'open-course-batch-organize') openBatchOrganize(target.dataset.course);
    else if (action === 'batch-preview-retry') openBatchOrganize(state.batchOrganize.courseId);
    else if (action === 'batch-select-pending' || action === 'batch-select-all' || action === 'batch-select-none' || action === 'batch-select-unrefined') {
      var selectMode = action.replace('batch-select-', '');
      if (selectMode === 'unrefined' && state.batchOrganize) state.batchOrganize.autoRefine = true;
      selectBatchLessons(selectMode); render();
    }
    else if (action === 'batch-organize-start') startBatchOrganize();
    else if (action === 'batch-organize-cancel') cancelBatchOrganize();
    else if (action === 'batch-organize-dismiss') dismissBatchOrganize();
    else if (action === 'batch-open-note') {
      var noteEpoch = accountQuotaEpoch;
      openCampusCourse(target.dataset.course).then(function () {
        if (noteEpoch === accountQuotaEpoch && state.campus.courseId === target.dataset.course && state.campus.course)
          openCampusLesson(target.dataset.recording);
      });
    }
    else if (action === "campus-account-settings" || action === "open-campus-setup") {
      state.view = "campus-account";
      render();
      if (state.campus.configured && state.connection.connected && !state.campus.home) loadNotionParents();
      var settingsPanel = document.querySelector(state.campus.configured ? ".home-setup" : "#recording-credentials-form");
      if (settingsPanel) settingsPanel.scrollIntoView({ block: "start" });
      if (!state.campus.configured) {
        var username = document.getElementById("recording-username");
        if (username) username.focus();
      }
    }
    else if (action === "open-campus-course") openCampusCourse(target.dataset.course);
    else if (action === "campus-select-term") { state.campus.termFilter = target.dataset.term || ""; render(); }
    else if (action === "campus-course-home") { state.campus.section = "home"; render(); }
    else if (action === "campus-course-settings") { state.campus.section = "settings"; render(); loadNotionParents(); }
    else if (action === "campus-open-center") {
      event.preventDefault();
      if (["assignments", "notices", "materials"].indexOf(target.dataset.center) < 0) return;
      state.campus.section = target.dataset.center; render();
    }
    else if (action === "campus-open-lesson") { openCampusLesson(target.dataset.recording); }
    else if (action === "campus-load-parents") loadNotionParents();
    else if (action === "campus-create-workspace") createWorkspace();
    else if (action === 'campus-replace-workspace') {
      var selected = state.campus.parents.find(function (page) {return page.id === state.campus.parentId;});
      var epoch = accountQuotaEpoch;
      confirmAction('将在「' + (selected ? selected.title : '所选页面') + '」下面新建并使用学习主页，原 Notion 页面保留。是否继续？').then(function (accepted) {
        if (accepted && epoch === accountQuotaEpoch) createWorkspace(true);
      });
    }
    else if (action === "campus-move-home") moveCampusHome();
    else if (action === "campus-open-home" && target.dataset.url) openNotionPreview(target.dataset.url);
    else if (action === "campus-save-existing-notion") saveExistingNotionLink();
    else if (action === "campus-prepare-assignment") selectCampusAssignment(target.dataset.assignment);
    else if (action === "campus-build-draft") prepareCampusAssignment();
    else if (action === "campus-analyze-requirements") analyzeCampusAssignmentRequirements();
    else if (action === "campus-apply-filename-rule") {
      state.campus.submission.form.archive_name = target.dataset.filenameTemplate;
      render();
      var archiveInput = document.querySelector('#assignment-prepare-form [name="archive_name"]');
      if (archiveInput) archiveInput.focus();
    }
    else if (action === "campus-submit-assignment") submitCampusAssignment();
    else if (action === "campus-assignment-status") loadAssignmentStatus(target.dataset.assignment);
    else if (action === "campus-load-receipts") loadCampusReceipts(state.campus.courseId);
    else if (action === "campus-recover-submission") recoverCampusSubmission(target.dataset.draft);
    else if (action === "campus-process") processCampusRecording(target.dataset.recording);
    else if (action === "campus-process-and-refine") processCampusRecording(target.dataset.recording, false, true);
    else if (action === "campus-quick-refine") {
      var quickRid = target.dataset.recording || '';
      var quickCourseId = state.campus && state.campus.courseId;
      if (quickRid && quickCourseId) {
        var quickKey = quickCourseId + ':' + quickRid;
        var existingPreview = readingPreviews[quickKey];
        if (existingPreview && existingPreview.markdown && !existingPreview.error) {
          existingPreview.open = true;
          generateRefinedLectureNote(quickKey, false, false);
        } else {
          Promise.resolve(openStudyPreview(quickRid, true)).then(function () {
            if (readingPreviews[quickKey]) generateRefinedLectureNote(quickKey, false, false);
          });
        }
      }
    }
    else if (action === "campus-regenerate" || action === "campus-term-review-regenerate") processCampusRecording(target.dataset.recording, true);
    else if (action === 'open-mcp-hub') {
      state.view = 'mcp-hub'; mcpHub.error = ''; render();
      loadMcpStatus({ silent: false });
    }
    else if (action === 'select-mcp-host') {
      mcpHub.host = target.dataset.host;
      var manualEl = document.querySelector('[data-mcp-manual-section]');
      if (mcpHub.status && Array.isArray(mcpHub.status.hosts) && manualEl) {
        var cordisEl = manualEl.querySelector('[data-persistent-disclosure="mcp-cordis-config"]');
        var cordisOpen = Boolean(cordisEl && cordisEl.open);
        var selectedHost = mcpHub.status.hosts.find(function (row) { return row.id === mcpHub.host; }) || mcpHub.status.hosts[0];
        if (selectedHost) {
          manualEl.outerHTML = mcpManualSectionMarkup(mcpHub.status, selectedHost);
          if (cordisOpen) {
            var nextCordis = document.querySelector('[data-persistent-disclosure="mcp-cordis-config"]');
            if (nextCordis) nextCordis.open = true;
          }
          return;
        }
      }
      render();
    }
    else if (['copy-mcp-snippet', 'copy-mcp-cordis', 'copy-mcp-prompt', 'copy-mcp-setup-prompt', 'copy-mcp-command', 'copy-mcp-url', 'copy-mcp-homework-prompt', 'copy-mcp-review-prompt'].indexOf(action) >= 0 && mcpHub.status) {
      var host = mcpHub.status.hosts.find(function (row) {return row.id === mcpHub.host;}) || mcpHub.status.hosts[0];
      var textToCopy = action === 'copy-mcp-url' ? (mcpHub.status.remote_mcp_url || mcpHub.status.launcher_path)
        : action === 'copy-mcp-homework-prompt' ? (host.homework_prompt || mcpHub.status.homework_prompt || mcpHub.status.sample_prompt || host.sample_prompt)
        : action === 'copy-mcp-review-prompt' ? (host.review_prompt || mcpHub.status.review_prompt || mcpHub.status.sample_prompt || host.sample_prompt)
        : (action === 'copy-mcp-prompt' || action === 'copy-mcp-setup-prompt') ? (mcpHub.status.sample_prompt || host.sample_prompt)
        : action === 'copy-mcp-command' ? host.cli_command
        : action === 'copy-mcp-cordis' ? (host.cordis_snippet || host.config_snippet)
        : host.config_snippet;
      var toastMsg = action === 'copy-mcp-cordis' ? '已复制 Cordis 插件配置，粘贴到 cordis.patch.yml 即可'
        : action === 'copy-mcp-snippet' ? ('已复制 ' + host.name + ' 配置，写入 ' + host.config_path + ' 即可')
        : action === 'copy-mcp-command' ? ('已复制 ' + host.name + ' 终端命令，在终端运行即可')
        : action === 'copy-mcp-url' ? '已复制直连地址，可粘贴到 MCP 客户端 URL 栏'
        : '已复制 Prompt，直接粘贴发给 AI 即可';
      copyStudyText(textToCopy, toastMsg);
    }
    else if (action === 'copy-assignment-mcp-prompt') {
      copyStudyText(target.dataset.prompt || '', '已复制交作业 Prompt，直接粘贴发给 AI 即可');
    }
    else if (action === 'campus-preview-course-summary') openStudyPreview('');
    else if (action === 'campus-preview-lecture-note') openStudyPreview(target.dataset.recording);
    else if (action === 'campus-toggle-preview') {readingPreviews[target.dataset.preview].open = false; render();}
    else if (action === 'campus-copy-markdown') {
      var preview = readingPreviews[target.dataset.preview];
      if (preview) {
        var useRefined = Boolean(preview.lecture && preview.noteMode === 'refined' && preview.refinedMarkdown);
        var copyBody = useRefined ? preview.refinedMarkdown : preview.markdown;
        copyStudyText(copyBody, useRefined ? '已复制精细整理讲义 Markdown' : (preview.lecture ? '已复制课堂流水账 Markdown，可直接粘贴或发送给 AI' : '已复制课程总结 Markdown'));
      }
    }
    else if (action === 'campus-refine-note') {
      generateRefinedLectureNote(target.dataset.preview, false, target.dataset.confirmedCard === 'true');
    }
    else if (action === 'campus-switch-note-mode') {
      var modePreview = readingPreviews[target.dataset.preview];
      if (modePreview) {
        modePreview.noteMode = target.dataset.mode === 'refined' ? 'refined' : 'raw';
        render();
      }
    }
    else if (action === 'campus-download-markdown' || action === 'campus-download-pdf') {
      exportSingleNotePdf(target.dataset.preview);
    }
    else if (action === 'campus-export-course-pdf' || action === 'campus-export-course-zip') {
      exportBatchNotesPdf(target.dataset.course || state.campus.courseId || '');
    }
    else if (action === 'export-all-notes-pdf' || action === 'export-all-notes-zip') {
      exportBatchNotesPdf('');
    }
    else if (action === 'campus-copy-code') {
      var codeWrap = target.closest('.study-code-wrap');
      var codeEl = codeWrap && codeWrap.querySelector('code');
      if (codeEl) copyStudyText(codeEl.textContent, '已复制代码，可直接粘贴');
    }
    else if (action === 'campus-generate-lecture-logic-map') {
      generateLectureLogicMap(target.dataset.preview, target.dataset.force === 'true');
    }
    else if (action === 'campus-switch-logic-tab') {
      var logicPreview = readingPreviews[target.dataset.preview];
      if (logicPreview) {
        logicPreview.logicTab = target.dataset.tab || 'all';
        render();
      }
    }
    else if (action === 'campus-jump-note-section') {
      var targetSection = cleanSectionName(target.dataset.section || '');
      var readerEl = target.closest('.lecture-note-reader') || document;
      var headings = Array.prototype.slice.call(readerEl.querySelectorAll('.study-markdown h2, .study-markdown h3'));
      var matched = headings.find(function (el) {
        var ds = cleanSectionName(el.getAttribute('data-note-section') || el.textContent || '');
        return ds && targetSection && (ds === targetSection || ds.indexOf(targetSection) >= 0 || targetSection.indexOf(ds) >= 0);
      });
      if (matched) {
        matched.classList.add('is-section-flash');
        if (matched.scrollIntoView) matched.scrollIntoView({ behavior: 'smooth', block: 'center' });
        setTimeout(function () { matched.classList.remove('is-section-flash'); }, 1800);
      }
    }
    else if (action === 'campus-open-slide-ref') {
      var sFile = target.dataset.slideFile || '';
      var sPage = target.dataset.slidePage || '1';
      if (sFile && state.campus.courseId && state.campus.lessonId) {
        var sUrl = '/api/campus/courses/' + encodeURIComponent(state.campus.courseId) +
          '/recordings/' + encodeURIComponent(state.campus.lessonId) +
          '/slide-page?file=' + encodeURIComponent(sFile) + '&page=' + encodeURIComponent(sPage);
        openImageLightbox({
          src: sUrl,
          alt: '原版课件：' + sFile + ' · 第 ' + sPage + ' 页',
          getAttribute: function () { return sUrl; }
        });
      }
    }
    else if (action === 'campus-zoom-mermaid') {
      var mmdFig = target.closest && target.closest('.study-mermaid-diagram');
      var svgNode = mmdFig && mmdFig.querySelector('svg');
      if (svgNode) {
        var existingDlg = document.querySelector('.note-lightbox');
        if (existingDlg) existingDlg.remove();
        var dlg = document.createElement('dialog');
        dlg.className = 'note-lightbox is-svg-lightbox';
        dlg.setAttribute('aria-label', '流程图放大预览');
        var stage = document.createElement('div');
        stage.className = 'svg-lightbox-stage';
        var svgClone = svgNode.cloneNode(true);
        svgClone.removeAttribute('style');
        svgClone.style.maxWidth = '100%';
        svgClone.style.height = 'auto';
        stage.appendChild(svgClone);
        var cap = document.createElement('p');
        cap.textContent = '点击任意处关闭预览';
        dlg.append(stage, cap);
        function closeMmdDlg() {
          if (dlg.open) dlg.close();
          dlg.remove();
        }
        dlg.addEventListener('click', closeMmdDlg);
        dlg.addEventListener('close', closeMmdDlg);
        document.body.appendChild(dlg);
        try { dlg.showModal(); } catch (_) { closeMmdDlg(); }
      }
    }
    else if (action === 'campus-generate-lecture-quiz') {
      generateLectureSelfTest(target.dataset.preview, target.dataset.force === 'true');
    }
    else if (action === 'campus-select-quiz-opt') {
      var quizPreview = readingPreviews[target.dataset.preview];
      if (quizPreview) {
        quizPreview.quizPicked = quizPreview.quizPicked || {};
        quizPreview.quizPicked[Number(target.dataset.q)] = Number(target.dataset.opt);
        render();
      }
    }
    else if (action === "campus-course-summary") startCourseSummary(target.dataset.course);
    else if (action === "campus-summaries") startCourseSummary("");
    else if (action === "campus-review-term") openTermReview(target.dataset.recording);
    else if (action === "campus-close-term-review") {
      state.termReview = { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
      render();
    }
    else if (action === "campus-submit-term-review") submitTermReview();
    else if (action === "campus-estimate-duration") estimateCampusDuration(target.dataset.recording);
    else if (action === "campus-review-handbook") reviewCampusHandbook(target);
    else if (action === "campus-open-lecture" && target.dataset.url) openNotionPreview(target.dataset.url);
    else if (action === "recording-edit-credentials") { state.recordings.editingCredentials = true; render(); }
    else if (action === "recording-clear-credentials") clearRecordingCredentials();
    else if (action === "recording-cancel-credentials") { if (state.recordings.credentialsPending) return; state.recordings.editingCredentials = false; state.recordings.credentialsError = ""; render(); }
    else if (action === "recording-sync") syncRecordingIndex();
    else if (action === "recording-sync-course") {
      if (target.dataset.course) syncRecordingIndex(target.dataset.course);
      else recordingError("请先选择这门课程对应的教学网课程。");
    }
    else if (action === "recording-cancel-sync") cancelRecordingSync();
    else if (action === "recording-cancel-background") updateBackgroundSync(false);
    else if (action === "recording-resume-background") updateBackgroundSync(true);
    else if (action === "recording-download" || action === "campus-term-review-download") downloadRecording(target.dataset.recording);
    else if (action === "toggle-local-recordings") {
      state.localRecordings.open = !state.localRecordings.open;
      if (state.localRecordings.open) loadLocalRecordings();
      else render();
    }
    else if (action === "local-storage-pick-dir") pickLocalStorageDirectory();
    else if (action === "local-storage-save-dir") {
      var dirInput = document.querySelector("[data-local-storage-dir-input]");
      updateLocalStorageSettings({ recordings_download_dir: dirInput ? dirInput.value.trim() : "" }, "已保存录像下载目录。");
    }
    else if (action === "local-storage-open-dir") openLocalStoragePath("");
    else if (action === "export-local-notes") exportLocalNotesZip();
    else if (action === "open-local-courses-dir") openLocalStoragePath("", "courses_dir");
    else if (action === "prune-stale-courses") pruneStaleLocalCourses();
    else if (action === "local-storage-reveal") openLocalStoragePath(target.dataset.recording || "");
    else if (action === "local-storage-delete-one") deleteOneLocalRecording(target.dataset.recording || "");
    else if (action === "local-storage-clear-all") clearAllLocalRecordings();
    else if (action === "recording-open-result" && target.dataset.url) {
      var localResult = target.dataset.url.match(/^\/api\/campus\/courses\/([^/]+)\/(?:recordings\/([^/]+)\/note-preview|summary-preview)$/);
      if (localResult) {
        var resultCourse = decodeURIComponent(localResult[1]);
        var resultRecording = localResult[2] ? decodeURIComponent(localResult[2]) : '';
        var resultEpoch = accountQuotaEpoch;
        state.view = 'campus-course';
        state.campus.courseId = resultCourse;
        state.campus.section = resultRecording ? 'lecture' : 'home';
        state.campus.lessonId = resultRecording;
        loadCampusCourse(resultCourse).then(function () {
          if (resultEpoch !== accountQuotaEpoch || state.campus.courseId !== resultCourse) return;
          var key = resultCourse + ':' + (resultRecording || 'summary');
          if (readingPreviews[key]) readingPreviews[key].open = false;
          openStudyPreview(resultRecording, true);
        });
      } else openNotionPreview(target.dataset.url);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.target && event.target.matches && event.target.matches('[data-reader-search]')) {
      var key = event.target.dataset.readerSearch;
      var preview = readingPreviews[key];
      if (event.key === 'Escape' && preview && (preview.query || event.target.value)) {
        event.preventDefault();
        event.target.value = '';
        preview.query = ''; preview.hitIndex = 0;
        updateReaderSearchInPlace(key, false);
        return;
      }
      if (event.key === 'Enter' && preview && preview.query && preview.query.trim()) {
        event.preventDefault();
        preview.hitIndex = (preview.hitIndex || 0) + (event.shiftKey ? -1 : 1);
        updateReaderSearchInPlace(key, true);
        return;
      }
    }
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey ||
        document.querySelector('dialog[open]') ||
        (event.target.closest && event.target.closest('input, textarea, select, [content' + 'editable]'))) return;
    if (state.view !== 'campus-course' || state.campus.section !== 'lecture' ||
        (event.key !== '[' && event.key !== ']')) return;
    var btn = document.querySelector('.breadcrumb-pager [data-lesson-nav="' + (event.key === '[' ? 'prev' : 'next') + '"]');
    if (btn && !btn.disabled) {
      event.preventDefault();
      openCampusLesson(btn.dataset.recording);
    }
  });

  function handleLiveSearchInput(target) {
    if (!target || !target.matches) return;
    if (target.matches('[data-reader-search]')) {
      var previewKey = target.dataset.readerSearch;
      if (readingPreviews[previewKey]) {
        readingPreviews[previewKey].query = target.value;
        readingPreviews[previewKey].hitIndex = 0;
        updateReaderSearchInPlace(previewKey, false);
      }
      return;
    }
    if (target.matches('[data-campus-material-filter]')) {
      state.campus.materialQuery = target.value;
      render();
      return;
    }
    if (target.matches('[data-campus-lesson-filter]')) {
      state.campus.dateQuery = target.value;
      render();
      return;
    }
    if (target.closest("#assignment-prepare-form") &&
        ["notion_url", "archive_name", "code_filename"].includes(target.name)) {
      state.campus.submission.form[target.name] = target.value;
      return;
    }
    if (target.matches("[data-campus-search], [data-campus-date]")) {
      var selector = target.matches("[data-campus-date]") ? "[data-campus-date]" : "[data-campus-search]";
      if (selector === "[data-campus-date]") state.campus.dateQuery = target.value;
      else state.campus.query = target.value;
      var start = target.selectionStart;
      render();
      var input = document.querySelector(selector);
      if (input) { input.focus(); input.setSelectionRange(start, start); }
    }
  }

  document.addEventListener("compositionstart", function (event) {
    imeComposingTarget = event.target || null;
  });

  document.addEventListener("compositionend", function (event) {
    var target = event.target;
    imeComposingTarget = null;
    if (target && target.matches) {
      handleLiveSearchInput(target);
    }
    if (renderDeferredDuringIme) {
      renderDeferredDuringIme = false;
      render();
    }
  });

  document.addEventListener("blur", function (event) {
    if (imeComposingTarget && event.target === imeComposingTarget) {
      imeComposingTarget = null;
      if (renderDeferredDuringIme) {
        renderDeferredDuringIme = false;
        render();
      }
    }
  }, true);

  document.addEventListener("input", function (event) {
    if (event.isComposing || (imeComposingTarget && event.target === imeComposingTarget)) return;
    handleLiveSearchInput(event.target);
  });
  document.addEventListener("change", function (event) {
    if (event.target.matches('[data-auto-track-course-checkbox]')) {
      toggleCourseAutoTrack(event.target.dataset.course);
      return;
    }
    if (event.target.matches('[data-auto-track-interval]')) {
      var mins = Number(event.target.value) || 120;
      updateAutoTrackSettings(
        { interval_minutes: mins },
        "已将自动巡检频率设为" + formatAutoTrackInterval(mins) + "。"
      );
      return;
    }
    if (event.target.matches('[data-auto-track-organize]')) {
      var autoOrg = Boolean(event.target.checked);
      updateAutoTrackSettings(
        { auto_organize_new_recordings: autoOrg },
        autoOrg ? "已开启：发现新课堂回放时自动转写并整理笔记。" : "已关闭自动整理新回放（仅自动巡检更新目录）。"
      );
      return;
    }
    if (event.target.matches('[data-auto-track-strategy]')) {
      var strat = event.target.value === "transcript" ? "transcript" : "topics";
      updateAutoTrackSettings(
        { notes_strategy: strat },
        "已更新新回放自动整理笔记样式。"
      );
      return;
    }
    if (event.target.matches('[data-local-storage-save-toggle]')) {
      var enabled = Boolean(event.target.checked);
      updateLocalStorageSettings(
        { save_recordings_locally: enabled },
        enabled ? "已开启：整理笔记后在本地保留原始录像。" : "已关闭：整理笔记后自动删除本地临时录像。"
      );
      return;
    }
    if (event.target.matches('[data-batch-note-style]')) {
      state.batchOrganize.notesStrategy = event.target.value; render(); return;
    }
    if (event.target.matches('[data-action="batch-toggle-auto-refine"]')) {
      if (state.batchOrganize) { state.batchOrganize.autoRefine = Boolean(event.target.checked); render(); }
      return;
    }
    if (event.target.matches('[data-action="batch-toggle-course"], [data-action="batch-toggle-lesson"]')) {
      var control = event.target;
      var batchCourse = batchCourses().find(function (course) { return course.course_id === control.dataset.course; });
      if (!batchCourse) return;
      batchCourse.lessons.forEach(function (lesson) {
        if (lesson.eligible && (!control.dataset.recording || lesson.id === control.dataset.recording))
          state.batchOrganize.selected[batchLessonKey(batchCourse.course_id, lesson.id)] = control.checked;
      });
      render(); return;
    }
    if (event.target.matches("[data-term-review-confirm]")) {
      state.termReview.confirmed = event.target.checked;
      var submit = document.querySelector('[data-action="campus-submit-term-review"]');
      if (submit) submit.disabled = !event.target.checked;
      return;
    }
    if (event.target.matches('#assignment-prepare-form [name="submission_mode"]')) {
      state.campus.submission.form = { submission_mode: event.target.value, notion_url: "", archive_name: "", code_filename: "", attachments: [] };
      state.campus.submission.error = "";
      render();
      return;
    }
    if (event.target.matches('#assignment-prepare-form [name="attachments"]')) {
      state.campus.submission.form.attachments = Array.from(event.target.files || []);
      var summary = document.querySelector("[data-assignment-attachments]");
      if (summary) summary.textContent = state.campus.submission.form.attachments.length ?
        "已选择：" + state.campus.submission.form.attachments.map(function (file) { return file.name; }).join("、") :
        "尚未选择文件";
      return;
    }
    if (event.target.matches("[data-campus-category]")) { state.campus.category = event.target.value; render(); return; }
    if (event.target.matches("[data-campus-parent]")) { state.campus.parentId = event.target.value; render(); return; }
    if (event.target.matches("[data-recording-direct-oss]")) { state.recordings.directOss = event.target.checked; return; }
    if (event.target.matches("[data-recording-note-strategy]")) {
      state.recordings.noteStrategyByKey[event.target.dataset.recordingNoteStrategy] = event.target.value;
      return;
    }
  });

  document.addEventListener("loadedmetadata", function (event) {
    if (!event.target.matches || !event.target.matches("[data-term-review-video]")) return;
    var seek = Number(event.target.dataset.seek);
    if (Number.isFinite(seek)) event.target.currentTime = Math.min(seek, event.target.duration || seek);
  }, true);
  document.addEventListener("submit", function (event) {
    event.preventDefault();
    if (event.target.id === "recording-credentials-form") saveRecordingCredentials();
    else if (event.target.id === "models-config-form") saveModelSettings();
  });

  var hiddenRecordingPollTicks = 0;
  window.setInterval(function () {
    if (document.hidden) {
      if (!(state.recordings.task.state === "running" ||
          (state.recordings.background && state.recordings.background.state === "running") ||
          (state.recordings.batch && state.recordings.batch.state === "running"))) return;
      hiddenRecordingPollTicks += 1;
      if (hiddenRecordingPollTicks % 4 !== 0) return;
    } else {
      hiddenRecordingPollTicks = 0;
    }
    pollRecordingTask();
  }, 1500);
  window.setInterval(function () {
    if (!document.hidden) {
      pollRecordingTask(true);
      triggerAutoIncrementalSync();
    }
  }, 60000);
  window.addEventListener("focus", function () {
    pollRecordingTask(true);
    triggerAutoIncrementalSync();
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      pollRecordingTask(true);
      triggerAutoIncrementalSync();
    }
  });

  render();
  loadUpdate().then(render).catch(function () {
    // A release-check failure never blocks or replaces the local panel.
  });
  Promise.all([loadModelSettings(), loadCampusCourses()])
    .then(function () {
      state.bootstrapping = false;
      render();
      loadMcpStatus({ silent: true });
      return pollRecordingTask(true)
        .then(function () {
          render();
          return triggerAutoIncrementalSync();
        });
    })
    .catch(function () {
      // an unreachable loopback API must not leave a blank surface
      state.bootstrapping = false;
      render();
    });
})();
