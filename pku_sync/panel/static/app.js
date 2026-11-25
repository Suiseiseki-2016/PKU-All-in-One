"use strict";
/* Local course workspace and account setup. */

(function () {
  var COPY = window.PANEL_COPY;
  var app = document.getElementById("app");
  var toastEl = document.getElementById("toast");
  var toastTimer = null;
  var pollTimer = null;
  var notionParentsRead = null;
  var notionParentsEpoch = 0;
  var accountQuotaEpoch = 0;
  var renderedAccountEpoch = -1;
  var renderedAccountEmail = "";
  var POLL_INTERVAL_MS = 1500;
  var POLL_MAX_ATTEMPTS = 240;
  async function confirmAction(message) {
    // Tauri replaces window.confirm with a Promise. A Promise is truthy
    // before the user decides, so native actions must await a real choice.
    if (!window.__TAURI_INTERNALS__) return (await window.confirm(message)) === true;
    if (document.querySelector('[data-product-confirmation]')) return false;
    return new Promise(function (resolve) {
      var dialog = document.createElement('dialog');
      dialog.className = 'product-confirmation';
      dialog.setAttribute('data-product-confirmation', '');
      dialog.setAttribute('role', 'alertdialog');
      dialog.setAttribute('aria-labelledby', 'product-confirmation-title');
      dialog.setAttribute('aria-describedby', 'product-confirmation-message');
      var title = document.createElement('h2');
      title.id = 'product-confirmation-title';
      title.textContent = '请确认此操作';
      var text = document.createElement('p');
      text.id = 'product-confirmation-message';
      text.textContent = message;
      var controls = document.createElement('div');
      controls.className = 'product-confirmation-actions';
      var settled = false;
      function finish(accepted) {
        if (settled) return;
        settled = true;
        if (dialog.open) dialog.close();
        dialog.remove();
        resolve(accepted === true);
      }
      ['cancel', 'confirm'].forEach(function (choice) {
        var control = document.createElement('button');
        control.type = 'button';
        control.className = choice === 'confirm' ? 'btn primary' : 'btn';
        control.setAttribute('data-confirmation-decision', choice);
        control.textContent = choice === 'confirm' ? '确认' : '取消';
        control.addEventListener('click', function () { finish(choice === 'confirm'); });
        controls.appendChild(control);
      });
      dialog.addEventListener('cancel', function (event) { event.preventDefault(); finish(false); });
      dialog.addEventListener('close', function () { finish(false); });
      dialog.append(title, text, controls);
      document.body.appendChild(dialog);
      try {
        dialog.showModal();
        controls.querySelector('[data-confirmation-decision="cancel"]').focus();
      } catch (_) { finish(false); }
    });
  }

  var state = {
    activated: true,
    bootstrapping: false,
    models: {
      loading: false,
      loaded: false,
      saving: false,
      testing: "",
      testResult: null,
      error: "",
      data: null,
      draft: null
    },
    batchOrganize: null,
    update: null,
    connection: {
      state: "disconnected",
      connected: false,
      status_label: COPY.connection.disconnected,
      error: null
    },
    view: "dashboard",
    campus: { courses: [], course: null, courseCache: {}, courseId: "", loading: false, error: "", configured: false, cookieKeyMissing: false, persistMode: "", home: null, parents: [], parentId: "", catalogAutoStarted: false, parentError: "", query: "", termFilter: "", category: "all", dateQuery: "", materialQuery: "", demoConnection: false, submission: { selectedId: "", draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } } },
    recordings: { configured: false, editingCredentials: false, loading: false, syncPending: false, backgroundPending: false, background: { state: "idle" }, batch: { state: "idle" }, error: "", task: { state: "idle" }, taskCourseId: "", noteStrategyByKey: {}, directOss: true },
    localRecordings: { open: false, loading: false, saveLocally: false, downloadDir: "", effectiveDir: "", count: 0, totalBytes: 0, items: [] },
    autoTrack: { open: false, enabled: false, trackedCourseIds: [], intervalMinutes: 120, autoOrganizeNewRecordings: true, notesStrategy: "topics", lastCheckedAt: "", lastCheckStatus: "idle", lastCheckSummary: "", nextCheckAt: "", isDue: false, recentEvents: [], busy: false },
    termReview: { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" },
    notice: "",
    noticeKind: "hint"
  };

  var GLYPH = {
    home: "⌂",
    courses: "▦",
    sync: "↻",
    notes: "✎",
    arrow: "→",
    launch: "↗",
    book: "▤",
    quiz: "✓"
  };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function template(text, values) {
    return String(text === null || text === undefined ? "" : text).replace(
      /\{(\w+)\}/g,
      function (whole, key) {
        return values && values[key] !== undefined ? String(values[key]) : "";
      }
    );
  }

  /* Multi-line approved copy keeps its line breaks without allowing markup. */
  function lines(text) {
    return String(text === null || text === undefined ? "" : text)
      .split("\n")
      .map(esc)
      .join("<br>");
  }

  function glyph(name) {
    return '<span aria-hidden="true">' + esc(GLYPH[name] || "·") + "</span>";
  }

  function button(label, action, options) {
    var opts = options || {};
    var classes = ["btn"];
    if (opts.primary) classes.push("btn-primary");
    if (opts.small) classes.push("btn-small");
    if (opts.quiet) classes.push("btn-quiet");
    var extra = "";
    if (opts.attrs) {
      for (var key in opts.attrs) {
        if (Object.prototype.hasOwnProperty.call(opts.attrs, key)) {
          extra += " " + key + '="' + esc(opts.attrs[key]) + '"';
        }
      }
    }
    return (
      '<button type="' + (opts.type === "submit" ? "submit" : "button") + '" class="' +
      classes.join(" ") +
      '" data-action="' +
      esc(action) +
      '"' +
      extra +
      (opts.disabled ? " disabled" : "") +
      ">" +
      (opts.icon ? glyph(opts.icon) : "") +
      esc(label) +
      "</button>"
    );
  }

  /* -- time readouts (never a frozen timestamp in the markup) ------------- */

  function parseTime(value) {
    if (!value) return null;
    var when = new Date(value);
    return isNaN(when.getTime()) ? null : when;
  }

  function relativeParts(iso) {
    var copy = COPY.time;
    var when = parseTime(iso);
    if (!when) return { value: copy.never, unit: "" };
    var diff = Date.now() - when.getTime();
    if (diff < 0) diff = 0;
    var minutes = Math.floor(diff / 60000);
    if (minutes < 1) return { value: copy.just_now, unit: "" };
    if (minutes < 60) return { value: String(minutes), unit: copy.minutes };
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return { value: String(hours), unit: copy.hours };
    return { value: String(Math.floor(hours / 24)), unit: copy.days };
  }

  function relativeText(iso) {
    var parts = relativeParts(iso);
    return parts.unit ? parts.value + " " + parts.unit : parts.value;
  }

  function sameDay(left, right) {
    return (
      left.getFullYear() === right.getFullYear() &&
      left.getMonth() === right.getMonth() &&
      left.getDate() === right.getDate()
    );
  }

  function friendlyTime(iso) {
    var when = parseTime(iso);
    if (!when) return "";
    var pad = function (value) {
      return (value < 10 ? "0" : "") + value;
    };
    var stamp = pad(when.getHours()) + ":" + pad(when.getMinutes());
    var now = new Date();
    if (sameDay(when, now)) return COPY.time.today + " " + stamp;
    if (sameDay(when, new Date(now.getTime() - 86400000))) {
      return COPY.time.yesterday + " " + stamp;
    }
    return (
      template(COPY.time.date, { month: when.getMonth() + 1, day: when.getDate() }) +
      " " +
      stamp
    );
  }

  function dayLabel(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
    if (!match) return "";
    return template(COPY.time.full_date, {
      year: Number(match[1]),
      month: Number(match[2]),
      day: Number(match[3])
    });
  }

  /* The lecture hint's topic: the scheduled day/period when Notion's title
     carries them, otherwise the title's own scope tail — never invented. */


  function brand(compact) {
    return (
      '<div class="' +
      (compact ? "mobile-brand" : "sidebar-brand") +
      '"><span class="brand-mark" aria-hidden="true">' +
      esc(COPY.brand.mark) +
      '</span><span class="brand-name">' +
      esc(COPY.brand.name) +
      "<small>" +
      esc(COPY.brand.note) +
      "</small></span></div>"
    );
  }

  /* -- network ------------------------------------------------------------ */

  function requestJson(url, options) {
    return fetch(url, options).then(function (response) {
      return response
        .json()
        .catch(function () {
          return null;
        })
        .then(function (body) {
          return { ok: response.ok, status: response.status, body: body };
        });
    });
  }

  function accountEmail() {
    return "";
  }

  function accountVerified() {
    return true;
  }

  function openCampusSetupIfNeeded() {
    if (!state.campus.configured) {
      state.view = "campus-account";
    }
  }

  function loadUpdate() {
    return requestJson("/api/update").then(function (result) {
      state.update = result.ok && result.body ? result.body : null;
    });
  }

  function loadConnection() {
    return requestJson("/api/connection").then(function (result) {
      if (result.ok && result.body) state.connection = result.body;
    });
  }

  /* The course screen's 课程资料概览 rows ARE the 按资料类型 groups, so the
     overview and the type view can never disagree about a count. */

  var recordingTaskRead = null;
  function pollRecordingTask(force) {
    if (!state.activated || (!force && state.recordings.task.state !== "running" &&
        state.recordings.background.state !== "running" && state.recordings.batch.state !== "running")) return;
    var epoch = accountQuotaEpoch;
    if (recordingTaskRead && recordingTaskRead.epoch === epoch) return recordingTaskRead.promise;
    var read = { epoch: epoch, promise: null,
      foreground: state.recordings.task, background: state.recordings.background, batch: state.recordings.batch };
    recordingTaskRead = read;
    read.promise = requestJson("/api/recordings/task").then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      // A start/cancel response owns newer state than this earlier observation.
      // Applying an old idle snapshot can otherwise hide a newly running task
      // and stop its scheduled polling altogether.
      if (state.recordings.task !== read.foreground || state.recordings.background !== read.background || state.recordings.batch !== read.batch) return;
      if (!result.ok || !result.body) return;
      var previous = state.recordings.task;
      var previousBackground = state.recordings.background;
      var previousBatch = state.recordings.batch;
      var foreground = Object.assign({}, result.body);
      delete foreground.background_sync;
      delete foreground.batch_organize;
      state.recordings.task = foreground;
      if (foreground.course_id) state.recordings.taskCourseId = foreground.course_id;
      state.recordings.background = result.body.background_sync || { state: "idle" };
      state.recordings.batch = result.body.batch_organize || { state: "idle" };
      var batchChanged = JSON.stringify(previousBatch) !== JSON.stringify(state.recordings.batch);
      var backgroundChanged = JSON.stringify(previousBackground) !== JSON.stringify(state.recordings.background);
      var completedCourseIds = (backgroundChanged ? (state.recordings.background.course_results || []) : []).filter(function (row) {
        var old = (previousBackground.course_results || []).find(function (entry) { return entry.course_id === row.course_id; });
        return row.state === "done" && (!old || old.state !== "done");
      }).map(function (row) { return row.course_id; });
      var completedCourse = completedCourseIds.length > 0;
      if (previous.state === 'running' && foreground.kind === 'sync' && foreground.state === 'done') {
        showToast('课程目录已更新');
      } else if (previousBackground.state === 'running' && state.recordings.background.state === 'done') {
        var bgDelta = state.recordings.background.delta_summary || '';
        if (bgDelta) showToast('课程目录已更新（' + bgDelta + '）');
        else if (!state.recordings.background.auto) showToast('课程目录已更新');
      }
      var batchNeedsRender = batchChanged && (
        previousBatch.state !== "running" || state.recordings.batch.state !== "running" || !refreshRunningBatchStatus()
      );
      var backgroundNeedsRender = backgroundChanged && (
        completedCourse || previousBackground.state !== "running" || state.recordings.background.state !== "running" || !refreshRunningBackgroundStatus()
      );
      var didFullRender = false;
      // Terminal feedback must be visible before ancillary cache/quota reads.
      if (batchNeedsRender || backgroundNeedsRender || previous.state !== foreground.state) {
        render();
        didFullRender = true;
      }
      if (previous.state === "running" && foreground.state === "failed") {
        var fgErr = campusReadError(foreground.error || "");
        showToast(fgErr || "整理未完成，请查看错误详情。");
      }
      if (previousBatch.state === "running" && (state.recordings.batch.state === "failed" || state.recordings.batch.state === "partial")) {
        if (state.recordings.batch.state === "failed") {
          showToast(state.recordings.batch.stage || "批量整理未完成，请展开查看失败原因。");
        }
      }
      if (previous.state === "running" && foreground.state !== "running" || completedCourse ||
          previousBackground.state === "running" && state.recordings.background.state !== "running" ||
          previousBatch.state === "running" && state.recordings.batch.state !== "running") {
        if (previousBatch.state === "running" && state.recordings.batch.state !== "running") state.batchOrganize = null;
        if (completedCourse || (previousBackground.state === "running" && state.recordings.background.state !== "running")) {
          state.campus.courseCache = {};
        }
        var onlyCompletedCourse = completedCourse && state.recordings.background.state === "running" &&
          !(previous.state === "running" && foreground.state !== "running") &&
          !(previousBatch.state === "running" && state.recordings.batch.state !== "running");
        var reloads = [loadCampusCourses()];
        return Promise.all(reloads).then(function () {
          if (epoch !== accountQuotaEpoch) return;
          if (state.view === "campus-course" &&
              (!onlyCompletedCourse || completedCourseIds.indexOf(state.campus.courseId) >= 0)) {
            return loadCampusCourse(state.campus.courseId).then(function () {
              var sumKey = state.campus.courseId + ":summary";
              if (readingPreviews[sumKey] && readingPreviews[sumKey].open) {
                openStudyPreview("", true, true);
              }
            });
          }
          render();
        });
      }
      if (!didFullRender && (
          previous.stage !== result.body.stage || previous.state !== result.body.state ||
          previous.stage_index !== result.body.stage_index || previous.current_item !== result.body.current_item ||
          previous.progress_done !== result.body.progress_done || previous.progress_total !== result.body.progress_total ||
          previous.progress_percent !== result.body.progress_percent ||
          previous.progress_unit !== result.body.progress_unit || previous.bytes_done !== result.body.bytes_done)) {
        if (previous.state !== "running" || result.body.state !== "running" || !refreshRunningTaskStatus()) render();
      }
    }).catch(function () {}).finally(function () {
      if (recordingTaskRead === read) recordingTaskRead = null;
    });
    return read.promise;
  }

  function refreshRunningTaskStatus() {
    var boxes = document.querySelectorAll(".recording-task");
    var markup = recordingTaskStatus();
    if (!boxes.length && !markup) return true;
    if (!boxes.length || !markup) return false;
    var template = document.createElement("template");
    template.innerHTML = markup;
    var next = template.content.firstElementChild;
    var nextText = next && next.querySelector(".recording-task-text");
    var nextProgress = next && next.querySelector(".recording-progress");
    if (!nextText || !nextText.firstChild || !nextProgress) return false;
    for (var i = 0; i < boxes.length; i += 1) {
      var currentText = boxes[i].querySelector(".recording-task-text");
      var currentProgress = boxes[i].querySelector(".recording-progress");
      if (!currentText || !currentText.firstChild || !currentProgress) {
        boxes[i].innerHTML = next.innerHTML;
        continue;
      }
      currentText.firstChild.nodeValue = nextText.firstChild.nodeValue;
      currentProgress.replaceWith(nextProgress.cloneNode(true));
    }
    return true;
  }

  function refreshRunningBatchStatus() {
    var boxes = document.querySelectorAll("[data-batch-organize]");
    var markup = batchOrganizeStatus();
    if (!boxes.length && !markup) return true;
    if (!boxes.length || !markup) return false;
    var template = document.createElement("template");
    template.innerHTML = markup;
    var next = template.content.firstElementChild;
    if (!next) return false;
    for (var i = 0; i < boxes.length; i += 1) {
      var details = boxes[i].querySelector('details[data-persistent-disclosure="batch-progress"]');
      var wasOpen = details ? details.open : false;
      boxes[i].setAttribute("data-batch-state", next.getAttribute("data-batch-state") || "");
      boxes[i].innerHTML = next.innerHTML;
      var nextDetails = boxes[i].querySelector('details[data-persistent-disclosure="batch-progress"]');
      if (nextDetails) nextDetails.open = wasOpen;
    }
    return true;
  }

  function refreshRunningBackgroundStatus() {
    var boxes = document.querySelectorAll("[data-background-sync]");
    var markup = backgroundSyncStatus();
    if (!boxes.length && !markup) return true;
    if (!boxes.length || !markup) return false;
    var template = document.createElement("template");
    template.innerHTML = markup;
    var next = template.content.firstElementChild;
    if (!next) return false;
    for (var i = 0; i < boxes.length; i += 1) {
      var details = boxes[i].querySelector("details[data-sync-details]");
      var wasOpen = details ? details.open : false;
      boxes[i].setAttribute("data-background-state", next.getAttribute("data-background-state") || "");
      boxes[i].innerHTML = next.innerHTML;
      var nextDetails = boxes[i].querySelector("details[data-sync-details]");
      if (nextDetails) nextDetails.open = wasOpen;
    }
    return true;
  }


  function applyLocalRecordingsPayload(data) {
    if (!data) return;
    state.localRecordings.saveLocally = Boolean(data.save_recordings_locally);
    state.localRecordings.downloadDir = data.recordings_download_dir || "";
    state.localRecordings.effectiveDir = data.effective_download_dir || "";
    state.localRecordings.count = Number(data.count) || 0;
    state.localRecordings.totalBytes = Number(data.total_bytes) || 0;
    state.localRecordings.items = Array.isArray(data.items) ? data.items : [];
  }

  function loadLocalRecordings() {
    var epoch = accountQuotaEpoch;
    state.localRecordings.loading = true;
    render();
    return requestJson("/api/recordings/local-storage").then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (result.ok && result.body) applyLocalRecordingsPayload(result.body);
    }).catch(function () {}).finally(function () {
      if (epoch === accountQuotaEpoch) {
        state.localRecordings.loading = false;
        render();
      }
    });
  }

  function updateLocalStorageSettings(payload, toastMessage) {
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/local-storage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "本地存储设置未保存，请检查路径权限。");
        render();
        return;
      }
      applyLocalRecordingsPayload(result.body);
      render();
      if (toastMessage) showToast(toastMessage);
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("本地存储设置保存失败，请稍后重试。");
    });
  }

  function pickLocalStorageDirectory() {
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/local-storage/pick-directory", { method: "POST" }).then(function (result) {
      if (epoch !== accountQuotaEpoch || !result.ok || !result.body) return;
      if (!result.body.cancelled && result.body.path) {
        return updateLocalStorageSettings({ recordings_download_dir: result.body.path }, "已更新录像下载目录。");
      }
    }).catch(function () {});
  }

  function openLocalStoragePath(recordingId, targetKind) {
    return requestJson("/api/recordings/local-storage/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording_id: recordingId || "", target: targetKind || "" })
    }).then(function (result) {
      if (!result.ok) showToast((result.body && result.body.detail) || "无法打开本地目录。");
    }).catch(function () {
      showToast("无法打开本地目录。");
    });
  }

  function exportLocalNotesZip() {
    showToast("正在打包课程 PDF 笔记备份…");
    return fetch("/api/campus/export-notes", { cache: "no-store" }).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          throw new Error((body && body.detail) || "当前暂无可导出的课程 PDF 笔记。");
        });
      }
      var disp = response.headers.get("Content-Disposition") || "";
      var match = disp.match(/filename="?([^";]+)"?/i);
      var filename = (match && match[1]) || "PKU-Courses-Backup.zip";
      return response.blob().then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        try { a.click(); } finally {
          a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        }
        showToast("已导出课程 PDF 笔记备份（" + filename + "）。");
      });
    }).catch(function (err) {
      showToast((err && err.message) || "导出 PDF 笔记失败。");
    });
  }

  function exportSingleNotePdf(previewKey) {
    var downloadPreview = readingPreviews[previewKey];
    if (!downloadPreview || !downloadPreview.markdown) return Promise.resolve();
    var courseId = (state.campus.course && state.campus.course.id) || state.campus.courseId || "";
    var courseTitle = ((state.campus.course && state.campus.course.title) || "课程").replace(/\([^()]*\)$/, "").trim();
    var docTitle = "课程总结";
    var recordingId = "";
    var isRefinedExport = Boolean(downloadPreview.lecture && downloadPreview.noteMode === 'refined' && downloadPreview.refinedMarkdown);
    var exportMarkdown = isRefinedExport ? downloadPreview.refinedMarkdown : downloadPreview.markdown;
    if (downloadPreview.lecture) {
      recordingId = String(previewKey || "").split(":").slice(1).join(":");
      var lesson = ((state.campus.course && state.campus.course.lessons) || []).find(function (item) {
        return item.id === recordingId;
      });
      docTitle = ((lesson && lesson.title) || "课堂笔记") + (isRefinedExport ? "-精编考点讲义" : "");
    }
    var filename = (courseTitle + "-" + docTitle + ".pdf").replace(/[/\\:*?"<>|\r\n]+/g, "-");
    showToast("正在导出笔记…");
    return fetch("/api/campus/export-pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        course_id: courseId,
        recording_id: recordingId,
        course_title: courseTitle,
        doc_title: docTitle,
        markdown: exportMarkdown,
        filename: filename
      })
    }).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          throw new Error((body && body.detail) || "导出笔记失败，请重试。");
        });
      }
      return response.blob().then(function (blob) {
        var downloadUrl = URL.createObjectURL(blob);
        var link = document.createElement("a");
        link.href = downloadUrl;
        link.download = filename;
        document.body.appendChild(link);
        try { link.click(); } finally {
          link.remove();
          setTimeout(function () { URL.revokeObjectURL(downloadUrl); }, 0);
        }
        showToast("已导出笔记");
      });
    }).catch(function (err) {
      showToast((err && err.message) || "导出 PDF 失败。");
    });
  }

  function exportBatchNotesPdf(courseId) {
    var cid = String(courseId || "").trim();
    var courseObj = cid
      ? ((state.campus.course && state.campus.course.id === cid ? state.campus.course : null) ||
         (state.campus.courses || []).find(function (c) { return c.id === cid; }))
      : null;
    var courseTitle = courseObj && courseObj.title
      ? String(courseObj.title).replace(/\s*[（(][^）)]*[）)]\s*$/, "").trim()
      : "";
    var defaultName = (courseTitle ? courseTitle + "-笔记.zip" : "笔记.zip")
      .replace(/[/\\:*?"<>|\r\n]+/g, "-");
    showToast("正在导出笔记包（" + defaultName + "）…");
    var endpoint = cid
      ? "/api/campus/courses/" + encodeURIComponent(cid) + "/export-notes-zip"
      : "/api/campus/export-notes-zip";
    return fetch(endpoint, { cache: "no-store" }).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          throw new Error((body && body.detail) || "当前暂无可导出的课程笔记。");
        });
      }
      return response.blob().then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = defaultName;
        document.body.appendChild(a);
        try { a.click(); } finally {
          a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        }
        showToast("已导出 PDF 笔记包（" + defaultName + "）");
      });
    }).catch(function (err) {
      showToast((err && err.message) || "导出 PDF 笔记包失败。");
    });
  }


  function deleteOneLocalRecording(recordingId) {
    if (!recordingId) return Promise.resolve();
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/local-storage/" + encodeURIComponent(recordingId), {
      method: "DELETE"
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "未能删除本地录像文件。");
        return;
      }
      applyLocalRecordingsPayload(result.body);
      showToast("已删除本地录像文件。");
      if (state.view === "campus-course" && state.campus.courseId) {
        return loadCampusCourse(state.campus.courseId);
      }
      render();
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("未能删除本地录像文件。");
    });
  }

  async function clearAllLocalRecordings() {
    if (!await confirmAction("确认清理本机保存的全部原始录像文件？\n（已生成的课堂笔记、转写与课件不受影响）")) return;
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/local-storage/clear", { method: "POST" }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "清理本地录像失败。");
        return;
      }
      applyLocalRecordingsPayload(result.body);
      showToast("已清理全部本地原始录像。");
      if (state.view === "campus-course" && state.campus.courseId) {
        return loadCampusCourse(state.campus.courseId);
      }
      render();
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("清理本地录像失败。");
    });
  }

  function toggleCourseAutoTrack(courseId) {
    var cid = String(courseId || "").trim();
    if (!cid) return Promise.resolve();
    var epoch = accountQuotaEpoch;
    var courseObj = (state.campus.courses || []).find(function (c) { return c.id === cid; }) ||
      (state.campus.course && state.campus.course.id === cid ? state.campus.course : null);
    var shortTitle = courseObj && courseObj.title
      ? String(courseObj.title).replace(/\([^()]*学期[^()]*\)$/, "").trim()
      : "该课程";
    return requestJson("/api/campus/auto-track/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ course_id: cid })
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "未能更新自动追踪状态。");
        return;
      }
      if (result.body.auto_track) applyAutoTrackPayload(result.body.auto_track);
      render();
      if (result.body.tracked) {
        showToast("已开启「" + shortTitle + "」自动追踪（" + formatAutoTrackInterval(state.autoTrack.intervalMinutes) + "后台巡检）。");
      } else {
        showToast("已取消「" + shortTitle + "」自动追踪。");
      }
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("未能更新自动追踪状态，请稍后重试。");
    });
  }

  function updateAutoTrackSettings(patch, toastMessage) {
    var epoch = accountQuotaEpoch;
    return requestJson("/api/campus/auto-track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch || {})
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "自动追踪设置保存失败。");
        return;
      }
      applyAutoTrackPayload(result.body);
      render();
      if (toastMessage) showToast(toastMessage);
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("自动追踪设置保存失败，请稍后重试。");
    });
  }

  function checkAutoTrackNow() {
    var epoch = accountQuotaEpoch;
    if (state.autoTrack.busy) return Promise.resolve();
    state.autoTrack.busy = true;
    render();
    return requestJson("/api/campus/auto-track/check-now", { method: "POST" }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "暂时无法启动追踪课程巡检。");
        return;
      }
      if (result.body.auto_track) applyAutoTrackPayload(result.body.auto_track);
      if (result.body.task) state.recordings.background = result.body.task;
      showToast("正在后台巡检 " + (state.autoTrack.trackedCourseIds.length || "") + " 门已追踪课程…");
      render();
      pollRecordingTask(true);
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("暂时无法启动追踪课程巡检。");
    }).finally(function () {
      if (epoch === accountQuotaEpoch) {
        state.autoTrack.busy = false;
        render();
      }
    });
  }

  function autoTrackPanel() {
    var at = state.autoTrack;
    if (!at || !at.open) return "";
    var trackedCount = at.trackedCourseIds.length;
    var syncBusy = state.recordings.syncPending || state.recordings.backgroundPending ||
      state.recordings.task.state === "running" || state.recordings.background.state === "running" ||
      (state.recordings.batch && state.recordings.batch.state === "running") || at.busy;
    var lastCheckLabel = at.lastCheckedAt ? friendlyTime(at.lastCheckedAt) : "尚未巡检";
    var nextCheckLabel = "";
    if (at.nextCheckAt && trackedCount > 0) {
      var nextTs = parseTime(at.nextCheckAt);
      if (at.isDue || (nextTs && nextTs.getTime() <= Date.now())) {
        nextCheckLabel = syncBusy ? "正在巡检中" : "打开网页即自动巡检";
      } else {
        nextCheckLabel = friendlyTime(at.nextCheckAt);
      }
    }
    var cleanSummary = String(at.lastCheckSummary || "").replace(/^已巡检\s*\d+\s*门追踪课程\s*·?\s*/, "").trim();
    var subMetaText = trackedCount > 0
      ? ("已追踪 " + trackedCount + " 门 · 上次 " + lastCheckLabel +
        (nextCheckLabel ? " · 下次 " + nextCheckLabel : "") +
        (cleanSummary ? " · " + cleanSummary : ""))
      : "选中下方课程后，打开网页将按频率静默巡检新课件、作业与回放（同班共享缓存可秒出笔记）。";
    var intervalOptions = [
      [30, "每 30 分钟"],
      [60, "每 1 小时"],
      [120, "每 2 小时（推荐）"],
      [240, "每 4 小时"],
      [360, "每 6 小时"]
    ].map(function (opt) {
      return '<option value="' + opt[0] + '"' + (Number(at.intervalMinutes) === opt[0] ? " selected" : "") + ">" + esc(opt[1]) + "</option>";
    }).join("");
    var courseChips = (state.campus.courses || []).map(function (course) {
      var checked = isCourseTracked(course.id);
      var cleanTitle = String(course.title || "").replace(/\(([^()]*学期[^()]*)\)$/, "").trim() || course.title;
      var deltaLabel = formatDeltaSummary(course.last_delta);
      var recCount = Number(course.recordings) || 0;
      return '<button type="button" class="auto-track-chip' + (checked ? ' is-tracked' : '') + '"' +
        ' data-action="toggle-course-track" data-course="' + esc(course.id) + '" data-auto-track-course-row="' + esc(course.id) + '"' +
        ' aria-pressed="' + (checked ? 'true' : 'false') + '"' +
        ' title="' + esc(cleanTitle + ' · ' + recCount + ' 节回放 · ' + (course.assignments || 0) + ' 项作业（点击切换追踪）') + '">' +
        '<span class="auto-track-chip-check" aria-hidden="true">' + (checked ? '✓' : '+') + '</span>' +
        '<span class="auto-track-chip-title">' + esc(cleanTitle) + '</span>' +
        '<span class="auto-track-chip-meta">' + (recCount > 0 ? esc(recCount + '讲') : '无回放') + '</span>' +
        (deltaLabel ? '<span class="auto-track-chip-delta">' + esc(deltaLabel) + '</span>' : '') +
        '</button>';
    }).join("");
    var eventsHtml = (at.recentEvents && at.recentEvents.length) ? (
      '<details class="auto-track-events" data-persistent-disclosure="auto-track-events">' +
      '<summary>最近巡检记录（' + esc(at.recentEvents.length) + ' 条）</summary>' +
      '<ul>' + at.recentEvents.slice(0, 6).map(function (ev) {
        return '<li><span>' + esc(friendlyTime(ev.timestamp) || ev.timestamp || "") + '</span> · <strong>' + esc(ev.summary || "") + '</strong></li>';
      }).join("") + '</ul></details>'
    ) : "";
    var noticeHtml = state.campus.cookieKeyMissing
      ? '<p class="auto-track-notice" role="status">🔑 当前浏览器尚未生成本地密钥，请在「连接设置」重新输入一次教学网密码以恢复自动巡检。</p>'
      : (!state.campus.configured
        ? '<p class="auto-track-notice" role="status">请先在「连接设置」保存教学网账号，即可启用打开网页静默自动巡检。</p>'
        : '');
    return '<section class="panel auto-track-panel" data-auto-track-panel aria-label="课程自动追踪设置">' +
      '<div class="auto-track-header">' +
      '<div class="auto-track-header-copy"><h2>⏱ 课程自动追踪</h2>' +
      '<p class="auto-track-submeta" role="status">' + esc(subMetaText) + '</p></div>' +
      '<div class="auto-track-header-actions">' +
      button(at.busy ? "巡检中…" : "立即巡检", "auto-track-check-now", {
        small: true,
        primary: true,
        disabled: trackedCount === 0 || !state.campus.configured || syncBusy || state.campus.demoConnection
      }) +
      button("收起", "toggle-auto-track-panel", { small: true, quiet: true }) +
      '</div></div>' +
      noticeHtml +
      '<div class="auto-track-toolbar">' +
      '<div class="auto-track-toolbar-left">' +
      '<label class="auto-track-field"><span>巡检频率</span><select data-auto-track-interval aria-label="自动巡检频率">' + intervalOptions + '</select></label>' +
      '<label class="auto-track-switch">' +
      '<input type="checkbox" data-auto-track-organize' + (at.autoOrganizeNewRecordings ? ' checked' : '') + '>' +
      '<span class="auto-track-switch-track" aria-hidden="true"></span>' +
      '<span class="auto-track-switch-label">新回放自动生成笔记</span></label>' +
      '</div>' +
      '<div class="auto-track-quick-actions">' +
      button("仅有回放", "auto-track-select-recordings", { small: true, quiet: true }) +
      button("全选", "auto-track-select-all", { small: true, quiet: true }) +
      (trackedCount > 0 ? button("清空", "auto-track-clear-all", { small: true, quiet: true }) : '') +
      '</div></div>' +
      '<div class="auto-track-course-chips">' + (courseChips || '<p class="form-note hint">当前暂无可选课程。</p>') + '</div>' +
      eventsHtml +
      '</section>';
  }

  function applyCampusCoursesPayload(body) {
    state.campus.courses = body.courses || [];
    if (Object.prototype.hasOwnProperty.call(body, 'cloud_web_mode')) {
      state.cloudWebMode = Boolean(body.cloud_web_mode);
    }
    if (Object.prototype.hasOwnProperty.call(body, 'notion_home')) {
      state.campus.home = body.notion_home && body.notion_home.id && body.notion_home.schema === 2 ? body.notion_home : null;
      state.campus.workspace = state.campus.home && state.campus.home.schema === 2 ? state.campus.home : null;
    }
    if (body.local_recordings) {
      applyLocalRecordingsPayload(body.local_recordings);
    }
    if (body.auto_track) {
      applyAutoTrackPayload(body.auto_track);
    }
    state.campus.cookieKeyMissing = Boolean(body.campus_cookie_key_missing);
    state.campus.persistMode = String(body.campus_persist_mode || "");
    if (state.campus.cookieKeyMissing) {
      state.campus.configured = false;
      state.recordings.configured = false;
    } else {
      state.campus.configured = Boolean(body.campus_configured) || Boolean(state.recordings.configured);
    }
    state.campus.demoConnection = Boolean(body.demo_connection);
    state.campus.staleCourses = Array.isArray(body.stale_courses) ? body.stale_courses : [];
    state.campus.catalogOutOfSync = Object.prototype.hasOwnProperty.call(body, 'catalog_out_of_sync')
      ? Boolean(body.catalog_out_of_sync)
      : state.campus.courses.some(function (c) { return Boolean(c.read_only); });
  }

  function pruneStaleLocalCourses() {
    var epoch = accountQuotaEpoch;
    return requestJson("/api/campus/prune-stale-courses", { method: "POST" }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "清理非同步课程失败，请稍后重试。");
        return;
      }
      var removed = Number(result.body.removed || 0);
      applyCampusCoursesPayload(result.body);
      render();
      showToast(removed > 0 ? "已清理 " + removed + " 门不同步的本地旧课程。" : "本地课程记录已与教学网保持同步。");
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("清理非同步课程失败，请稍后重试。");
    });
  }

  function loadCampusCourses() {
    var epoch = accountQuotaEpoch;
    state.campus.loading = true;
    return requestJson("/api/campus/courses").then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      state.campus.loading = false;
      if (!result.ok || !result.body) return;
      applyCampusCoursesPayload(result.body);
      render();
    }).catch(function () {
      if (epoch !== accountQuotaEpoch) return;
      return requestJson("/api/campus/courses").then(function (retryResult) {
        if (epoch !== accountQuotaEpoch) return;
        state.campus.loading = false;
        if (!retryResult.ok || !retryResult.body) return;
        applyCampusCoursesPayload(retryResult.body);
        render();
      }).catch(function () {
        if (epoch === accountQuotaEpoch) {
          state.campus.loading = false;
          render();
        }
      });
    });
  }

  function formatDeltaSummary(delta) {
    if (!delta || typeof delta !== "object") return "";
    var parts = [];
    if (Number(delta.added_recordings) > 0) parts.push(Number(delta.added_recordings) + " 节回放");
    if (Number(delta.added_materials) > 0) parts.push(Number(delta.added_materials) + " 份课件");
    if (Number(delta.added_assignments) > 0) parts.push(Number(delta.added_assignments) + " 项作业");
    if (Number(delta.added_announcements) > 0) parts.push(Number(delta.added_announcements) + " 条通知");
    return parts.length ? "新增 " + parts.join(" · ") : "";
  }

  var lastAutoSyncAttemptAt = 0;

  function triggerAutoIncrementalSync() {
    if (!state.activated || !state.campus.configured || state.campus.demoConnection || state.campus.catalogOutOfSync) {
      return Promise.resolve();
    }
    if (!state.campus.courses.length) return Promise.resolve();
    var bgState = (state.recordings.background && state.recordings.background.state) || "idle";
    if (state.recordings.syncPending || state.recordings.backgroundPending ||
        state.recordings.task.state === "running" ||
        bgState === "running" ||
        (state.recordings.batch && state.recordings.batch.state === "running")) {
      return Promise.resolve();
    }
    var now = Date.now();
    if (now - lastAutoSyncAttemptAt < 60000) return Promise.resolve();
    var selectiveAutoTrack = Boolean(
      state.autoTrack && state.autoTrack.enabled &&
      Array.isArray(state.autoTrack.trackedCourseIds) && state.autoTrack.trackedCourseIds.length > 0
    );
    var nextTrackTs = state.autoTrack && state.autoTrack.nextCheckAt ? parseTime(state.autoTrack.nextCheckAt) : null;
    var hasAutoTrackDue = Boolean(
      selectiveAutoTrack &&
      (state.autoTrack.isDue || !state.autoTrack.lastCheckedAt || (nextTrackTs && nextTrackTs.getTime() <= now))
    );
    var hasStaleIsoCourse = !selectiveAutoTrack && state.campus.courses.some(function (c) {
      if (!c.synced_at || String(c.synced_at).indexOf("T") === -1) return false;
      var ts = parseTime(c.synced_at);
      return ts && (now - ts.getTime() >= 15 * 60 * 1000);
    });
    if (!hasAutoTrackDue && !hasStaleIsoCourse) return Promise.resolve();
    lastAutoSyncAttemptAt = now;
    var epoch = accountQuotaEpoch;
    return requestJson("/api/campus/auto-sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ min_age_seconds: 900 })
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch || !result.ok || !result.body || !result.body.triggered) return;
      if (result.body.task) {
        state.recordings.background = result.body.task;
        render();
        pollRecordingTask(true);
      }
    }).catch(function () {});
  }

  function courseTaskBusy(courseId) {
    var background = state.recordings.background || {};
    var batch = state.recordings.batch || {};
    return state.recordings.syncPending || state.recordings.task.state === "running" ||
      batch.state === "running" && (batch.items || []).some(function (item) {
        return item.course_id === courseId && (item.state === "pending" || item.state === "running");
      }) ||
      background.state === "running" && background.current_course_id === courseId;
  }

  function campusReadError(message) {
    var value = String(message || "").trim();
    if (/课程准备读取已过期|课程快照或准备顺序已变化|课序不一致/.test(value))
      return "课程信息需要重新核对，同步暂未完成。登录状态不受影响，可返回「我的课程」更新目录后重试。";
    if (/account_unconfirmed|revision|快照.*未确认|快照.*已更新/i.test(value))
      return "账号目录版本核对未完成或已变化；已有内容保留，请更新这门课后重试。";
    if (/Traceback|HTTPException|ECONNREFUSED|ConnectError|TimeoutError|502 Bad Gateway|504 Gateway/i.test(value))
      return "服务连接暂时繁忙或中断；已有内容已保留，请稍后重试。";
    if (/未配置|API Key|api_key|openai_api_key|dashscope_api_key|faster-whisper/i.test(value))
      return value;
    // Legacy publishing tasks can still finish in the backend. Keep their
    // diagnostics readable without restoring service branding to the UI.
    return value.replace(/Notion/gi, '笔记同步服务') || "教学网连接中断；保留已有内容，请检查网络后重试。";
  }

  function rememberCampusCourse(course) {
    if (!course || !course.id) return;
    state.campus.courseCache[course.id] = { epoch: accountQuotaEpoch, body: JSON.parse(JSON.stringify(course)) };
  }

  function loadCampusCourse(courseId) {
    var epoch = accountQuotaEpoch;
    var readId = (state.campus.courseReadId || 0) + 1;
    state.campus.courseReadId = readId;
    state.campus.loading = true;
    state.campus.error = "";
    render();
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId)).then(function (result) {
      if (epoch !== accountQuotaEpoch || state.campus.courseId !== courseId || state.campus.courseReadId !== readId) return;
      state.campus.loading = false;
      if (result.ok && result.body && result.body.id === courseId) {
        state.campus.course = result.body;
        rememberCampusCourse(result.body);
        state.campus.error = "";
      } else {
        state.campus.error = campusReadError(result.body && result.body.detail);
        if (state.campus.course) state.campus.course.read_only = true;
      }
      render();
      if (result.ok) return loadCampusReceipts(courseId);
    }).catch(function () {
      if (epoch !== accountQuotaEpoch || state.campus.courseId !== courseId || state.campus.courseReadId !== readId) return;
      state.campus.loading = false;
      state.campus.error = campusReadError();
      if (state.campus.course) state.campus.course.read_only = true;
      render();
    });
  }

  function loadNotionParents(options) {
    if (state.campus.demoConnection) return Promise.resolve();
    if (!state.connection.connected) { state.campus.parentError = "请先连接 Notion。"; render(); return Promise.resolve(); }
    var epoch = notionParentsEpoch;
    var email = accountEmail();
    var selectedOnly = Boolean(options && options.selectedOnly);
    if (notionParentsRead && notionParentsRead.epoch === epoch && notionParentsRead.email === email &&
        notionParentsRead.selectedOnly === selectedOnly)
      return notionParentsRead.promise;
    state.campus.parentError = "";
    state.campus.parentsLoading = true;
    render();
    var pending = { epoch: epoch, email: email, selectedOnly: selectedOnly, promise: null };
    notionParentsRead = pending;
    pending.promise = requestJson("/api/campus/notion/parents" + (selectedOnly ? "?selected_only=true" : "")).then(function (result) {
      if (epoch !== notionParentsEpoch) return;
      if (!result.ok || !result.body) { state.campus.parentError = (result.body && result.body.detail) || "Notion 页面暂时无法读取。"; render(); return; }
      state.campus.parents = result.body.pages || [];
      state.campus.homeSync = result.body.home_sync || null;
      state.campus.home = result.body.home && result.body.home.id && result.body.home.schema === 2 ? result.body.home : null;
      state.campus.workspace = result.body.workspace && result.body.workspace.id ? result.body.workspace : null;
      var destinations = state.campus.parents.filter(function (page) { return !state.campus.home || page.id !== state.campus.home.id; });
      if (state.campus.home && destinations.some(function (page) { return page.id === state.campus.home.parent_id; }))
        state.campus.parentId = state.campus.home.parent_id;
      else if (!destinations.some(function (page) { return page.id === state.campus.parentId; }))
        state.campus.parentId = destinations.length ? destinations[0].id : "";
      render();
    }).catch(function () {
      if (epoch !== notionParentsEpoch) return;
      state.campus.parentError = "Notion 页面暂时无法读取。"; render();
    }).finally(function () {
      if (notionParentsRead === pending) {
        notionParentsRead = null;
        state.campus.parentsLoading = false;
        render();
      }
    });
    return pending.promise;
  }

  function campusCatalogReady() {
    return state.campus.courses.some(function (course) { return Boolean(course.synced_at) && !course.read_only; });
  }

  function campusSetupGuide(includeSyncAction) {
    var catalogReady = campusCatalogReady();
    var syncBusy = state.recordings.syncPending || state.recordings.backgroundPending ||
      state.recordings.task.state === "running" || state.recordings.background.state === "running";
    var modelInfo = typeof modelReadinessInfo === "function" ? modelReadinessInfo() : { ready: false };
    var steps = [
      ["教学网账号", Boolean(state.campus.configured)],
      ["课程目录", catalogReady],
      ["模型配置 (BYOK)", Boolean(modelInfo.ready)]
    ];
    var next = steps.find(function (step) { return !step[1]; });
    var nextAction = !state.campus.configured
      ? (state.view === 'campus-account' ? '' : button("设置教学网账号", "campus-account-settings", { small: true, primary: true }))
      : !catalogReady
        ? (includeSyncAction === false ? '' : button(syncBusy ? "正在更新课程目录…" : "更新课程目录", "recording-sync", { small: true, primary: true, disabled: syncBusy }))
        : !modelInfo.ready
          ? button("配置个人大模型 / ASR", "open-models", { small: true, primary: true })
          : '';
    if (!next && state.view === 'campus-account') {
      nextAction = button('进入我的课程', 'open-dashboard', { small: true, primary: true });
    }
    var nextLine = !state.campus.configured
      ? (state.view === 'campus-account' ? '' : '下一步：连接北大教学网账号，同步课程资料与课堂回放。')
      : (next ? '下一步：' + esc(next[0]) : '已就绪，可以从课程中选择录像按需生成笔记。');
    var setupFooterHtml = nextAction ? '<div class="setup-footer-actions">' + nextAction + '</div>' : '';
    return '<section class="panel campus-setup"><h2>开始使用</h2><ol class="setup-steps">' +
      steps.map(function (step, idx) {
        var stepClass = 'setup-step' + (step[1] ? ' is-done' : (step === next ? ' is-current' : ''));
        return '<li class="' + stepClass + '"><span class="setup-mark" aria-hidden="true">' + (step[1] ? '✓' : String(idx + 1)) + '</span><span>' + esc(step[0]) + '</span></li>';
      }).join("") +
      '</ol>' + (nextLine ? '<p class="setup-next-line">' + nextLine + '</p>' : '') + setupFooterHtml + '</section>';
  }

  function formatCourseTitleMarkup(title) {
    var match = String(title || '').match(/^(.*?)(\((?:\d{2}-\d{2}学年.*|.*学期.*)\))$/);
    return match ? esc(match[1]) + '<span class="course-title-term">' + esc(match[2]) + '</span>' : esc(title);
  }

  function formatLessonTitleMarkup(title, date) {
    title = String(title || '');
    return date && title.startsWith(date) && title.slice(date.length).trim()
      ? '<span class="lesson-title-date">' + esc(date) + '</span><span>' + esc(title.slice(date.length)) + '</span>'
      : esc(title);
  }

  function batchLessonKey(courseId, lessonId) { return JSON.stringify([courseId, lessonId]); }

  function batchCourses() {
    var panel = state.batchOrganize;
    return panel && panel.preview ? panel.preview.courses.filter(function (course) {
      return !panel.courseId || panel.courseId === course.course_id;
    }) : [];
  }

  function selectBatchLessons(mode) {
    var panel = state.batchOrganize;
    panel.selected = {};
    batchCourses().forEach(function (course) {
      course.lessons.forEach(function (lesson) {
        if (!lesson.eligible) return;
        if (mode === 'all' || (mode === 'pending' && lesson.needs_organize) || (mode === 'unrefined' && !lesson.refined_available)) {
          panel.selected[batchLessonKey(course.course_id, lesson.id)] = true;
        }
      });
    });
  }

  function openBatchOrganize(courseId) {
    var epoch = accountQuotaEpoch;
    var panel = { open: true, loading: true, courseId: courseId || '', selected: {}, notesStrategy: 'topics', autoRefine: false, error: '' };
    state.batchOrganize = panel;
    render();
    pollRecordingTask(true);
    return requestJson('/api/campus/batch-organize/preview').then(function (result) {
      if (epoch !== accountQuotaEpoch || state.batchOrganize !== panel) return;
      if (!result.ok || !result.body) throw new Error(result.body && result.body.detail || '暂时无法读取录像，请重试。');
      panel.preview = result.body;
      selectBatchLessons('pending');
    }).catch(function (error) {
      if (epoch === accountQuotaEpoch && state.batchOrganize === panel) panel.error = error.message;
    }).finally(function () {
      if (epoch === accountQuotaEpoch && state.batchOrganize === panel) { panel.loading = false; render(); }
    });
  }

  function selectedBatchItems() {
    var selected = state.batchOrganize && state.batchOrganize.selected || {};
    var items = [];
    batchCourses().forEach(function (course) {
      course.lessons.forEach(function (lesson) {
        if (lesson.eligible && selected[batchLessonKey(course.course_id, lesson.id)])
          items.push({ course: course, lesson: lesson });
      });
    });
    return items;
  }

  function batchOrganizePanel() {
    var panel = state.batchOrganize;
    if (!panel || !panel.open) return '';
    var header = '<section class="batch-organize-panel" aria-label="批量整理选择"><h2>一键并行整理课堂笔记</h2>' +
      '<p class="form-note">支持多门课程同时并行处理，直接调用您本机配置的语音转写与大模型服务。</p>';
    if (panel.loading) return header + '<p role="status">正在读取录像与处理预估…</p></section>';
    if (!panel.preview) return header + '<p role="alert">' + esc(panel.error) + '</p>' +
      button('重新读取', 'batch-preview-retry', {small:true}) + '</section>';
    var autoRefine = Boolean(panel.autoRefine);
    var activeModeLabel = autoRefine ? '课堂笔记 + ✨ 精细整理' : '课堂图文笔记';
    var items = selectedBatchItems();
    var coursesCount = new Set(items.map(function (item) { return item.course.course_id; })).size;
    var minutes = 0, reuse = 0, estimated = false, unrefinedCount = 0;
    items.forEach(function (item) {
      var lesson = item.lesson;
      minutes += lesson.estimated_transcribe_minutes;
      if (autoRefine && !lesson.refined_available) {
        unrefinedCount += 1;
      }
      if (lesson.transcript_available) reuse += 1;
      if (!lesson.transcript_available && lesson.duration_is_estimated) estimated = true;
    });
    var modelInfo = typeof modelReadinessInfo === 'function' ? modelReadinessInfo() : { ready: false, notesModel: '', asrLabel: '' };
    var autoRefineBox =
      '<label class="batch-auto-refine-toggle' + (autoRefine ? ' is-checked' : '') + '" data-batch-auto-refine-box>' +
      '<input type="checkbox" data-action="batch-toggle-auto-refine"' + (autoRefine ? ' checked' : '') + '>' +
      '<span><strong>✨ 同时生成 AI 精细整理讲义（与转写一次完成）</strong>' +
      '<small>自动剔除课堂闲谈、提炼核心考点公式与板书（已精编讲次自动跳过）</small></span>' +
      '</label>';
    var summary = autoRefineBox + '<div class="batch-cost-summary" data-batch-cost-estimate aria-live="polite">' +
      '<div><small>所选范围</small><strong>' + coursesCount + ' 门课程 · ' + items.length + ' 节录像</strong><span>' +
      (coursesCount > 1 ? '最多 ' + Math.min(3, coursesCount) + ' 门课并行' : '单课顺序处理') +
      (autoRefine && unrefinedCount > 0 ? ' · 含 ' + unrefinedCount + ' 讲精细整理' : '') + '</span></div>' +
      '<div><small>预计转写时长</small><strong>约 ' + minutes + ' 分钟</strong><span>' +
      (reuse ? '含 ' + reuse + ' 讲复用已有转写；' : '') + (estimated ? '未读时长讲次按标准课时估算' : '按录像实际时长计算') +
      '</span><span>语音后端：' + esc(modelInfo.asrLabel || 'BYOK') + '</span></div>' +
      '<div><small>使用模型</small><strong>' + esc(modelInfo.notesModel || '待配置') + '</strong><span>' + esc(activeModeLabel) +
      '</span><span>本机直连 · 已有笔记自动复用</span></div></div>' +
      (!modelInfo.ready ? '<p class="form-note hint" role="status">提示：当前模型或语音转写尚未配置完整，开始整理前请先前往「模型配置」填写对应 API Key。</p>' : '');
    var hasUnrefinedEligible = false;
    var courses = batchCourses().map(function (course) {
      var eligible = course.lessons.filter(function (lesson) { return lesson.eligible; });
      if (eligible.some(function (lesson) { return !lesson.refined_available; })) hasUnrefinedEligible = true;
      var count = eligible.filter(function (lesson) { return panel.selected[batchLessonKey(course.course_id, lesson.id)]; }).length;
      var attrs = ' data-course="' + esc(course.course_id) + '"';
      return '<article class="batch-course"><label class="batch-course-heading"><input type="checkbox" data-action="batch-toggle-course"' + attrs +
        (count && count < eligible.length ? ' data-batch-indeterminate' : '') +
        (count && count === eligible.length ? ' checked' : '') + (!eligible.length ? ' disabled' : '') +
        '><span><strong>' + esc(course.title) + '</strong><small>' + esc(course.term) + ' · 已选 ' + count + ' / ' + course.lessons.length +
        ' 讲（待整理 ' + course.lessons.filter(function (lesson) { return lesson.needs_organize; }).length + ' 讲）</small></span></label>' +
        (course.read_only ? '<p class="form-note">' + esc(course.read_only_message) + '</p>' : '') +
        '<details class="batch-course-lessons" data-persistent-disclosure="batch-course-' + esc(course.course_id) + '"><summary>展开选择具体讲次</summary>' +
        course.lessons.map(function (lesson) {
          var statusTag = lesson.unavailable_reason || (course.read_only ? '课程只读' : lesson.refined_available ? '✨ 已精编' : lesson.notes_available ? '已生成笔记' : lesson.transcript_available ? '已有转写' : '待整理');
          return '<label class="batch-lesson"><input type="checkbox" data-action="batch-toggle-lesson"' + attrs + ' data-recording="' + esc(lesson.id) + '"' +
            (panel.selected[batchLessonKey(course.course_id, lesson.id)] ? ' checked' : '') + (!lesson.eligible ? ' disabled' : '') +
            '><span><strong>' + esc(lesson.date) + ' ' + esc(lesson.title) + '</strong><small>' +
            esc(statusTag) +
            ' · ' + (lesson.transcript_available ? '转写 0 分钟' : '约 ' + lesson.estimated_transcribe_minutes + ' 分钟') +
            ' · ' + esc(activeModeLabel) + '</small></span></label>';
        }).join('') + '</details></article>';
    }).join('');
    var running = state.recordings.batch.state === 'running' || panel.starting;
    var startBtnText = panel.starting
      ? '正在启动…'
      : (autoRefine ? ('✨ 开始整理并精细整理（' + items.length + ' 讲）') : ('开始并行整理（' + items.length + ' 讲）'));
    return header + '<div class="batch-selection-actions">' + button('全选待整理', 'batch-select-pending', {small:true, quiet:true}) +
      (hasUnrefinedEligible ? button('✨ 全选未精编', 'batch-select-unrefined', {small:true, quiet:true}) : '') +
      button('全选全部讲次', 'batch-select-all', {small:true, quiet:true}) + button('清空选择', 'batch-select-none', {small:true, quiet:true}) + '</div>' +
      summary + '<div class="batch-course-list">' + (courses || '<p>当前没有可整理的录像。</p>') + '</div>' +
      (panel.error ? '<p role="alert">' + esc(panel.error) + '</p>' : '') +
      button(startBtnText, 'batch-organize-start', {primary:true,
        disabled: !items.length || running || state.campus.demoConnection}) + '</section>';
  }

  async function startBatchOrganize() {
    var panel = state.batchOrganize;
    if (!panel || panel.starting || state.recordings.batch.state === 'running') return;
    var rawItems = selectedBatchItems();
    var items = rawItems.map(function (item) { return {course_id:item.course.course_id, recording_id:item.lesson.id}; });
    if (!items.length) return;
    var autoRefine = Boolean(panel.autoRefine);
    var epoch = accountQuotaEpoch;
    panel.starting = true; panel.error = ''; render();
    return requestJson('/api/campus/batch-organize', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items:items, notes_strategy:panel.notesStrategy, auto_refine:autoRefine, direct_oss:true, max_parallel_courses:3})
    }).then(function (result) {
      if (!result || epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) throw new Error(result.body && result.body.detail || '批量整理未能开始，请重试。');
      state.recordings.batch = result.body;
      panel.open = false;
    }).catch(function (error) {
      if (epoch === accountQuotaEpoch) {
        panel.error = error.message;
        showToast(error.message);
        Promise.resolve(pollRecordingTask(true)).then(function () {
          if (epoch === accountQuotaEpoch && state.recordings.batch && state.recordings.batch.state === 'running') {
            panel.error = '';
            panel.open = false;
            render();
          }
        });
      }
    }).finally(function () {
      if (epoch === accountQuotaEpoch) { panel.starting = false; render(); }
    });
  }

  function cancelBatchOrganize() {
    var epoch = accountQuotaEpoch;
    return requestJson('/api/campus/batch-organize/cancel', {method:'POST'}).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) return showToast(result.body && result.body.detail || '取消请求未能送达，请重试。');
      state.recordings.batch = result.body; render();
    }).catch(function () { if (epoch === accountQuotaEpoch) showToast('取消请求未能送达，请重试。'); });
  }

  function dismissBatchOrganize() {
    var batch = state.recordings.batch;
    if (batch) {
      state.recordings.dismissedBatchId = batch.id || 'done';
      render();
    }
  }

  function batchOrganizeStatus() {
    var batch = state.recordings.batch;
    if (!batch || batch.state === 'idle') return '';
    if (batch.state !== 'running' && state.recordings.dismissedBatchId && state.recordings.dismissedBatchId === (batch.id || 'done')) return '';
    var done = (batch.items || []).filter(function (item) { return item.state === 'done'; }).length;
    var total = Math.max(1, Number(batch.progress_total) || (batch.items || []).length || 1);
    var hasPercent = typeof batch.progress_percent === 'number' && batch.progress_percent > 0;
    var progressVal = hasPercent ? Math.min(100, Math.max(0, Math.round(batch.progress_percent))) : (batch.progress_done || 0);
    var progressMax = hasPercent ? 100 : total;
    var stageText = String(batch.stage || '').trim();
    var stageAlreadyHasCount = stageText.indexOf('已完成 ' + done + ' / ' + total) >= 0 || stageText.indexOf(done + ' / ' + total) >= 0;
    var headingText = batch.state === 'running' ? '正在并行整理'
      : batch.state === 'done' ? '✓ 批量整理完成'
      : batch.state === 'partial' ? '批量整理部分完成'
      : batch.state === 'cancelled' ? '批量整理已取消'
      : batch.state === 'failed' ? '批量整理未完成'
      : '批量整理';
    var statusCopy;
    if (batch.state === 'done') {
      statusCopy = '已完成全部 ' + done + ' 讲课堂笔记整理，可展开查看各讲并直接阅读。';
    } else if (batch.state === 'running') {
      statusCopy = (stageText ? esc(stageText) + ' · ' : '') + '已完成 ' + done + ' / ' + total + ' 讲 · ' +
        (batch.active_courses || 0) + ' 门课正在处理' + (hasPercent ? ' · 总进度 ' + progressVal + '%' : '');
    } else {
      statusCopy = stageText
        ? (stageAlreadyHasCount ? esc(stageText) : esc(stageText) + ' · 已完成 ' + done + ' / ' + total + ' 讲')
        : ('已完成 ' + done + ' / ' + total + ' 讲');
    }
    var barVal = batch.state === 'running' ? progressVal : done;
    var barMax = batch.state === 'running' ? progressMax : total;
    var hasModelFailure = batch.state !== 'running' && (batch.items || []).some(function (item) {
      return item.state === 'failed' && /未配置|模型配置|API Key|api_key|openai_api_key|dashscope_api_key|faster-whisper|401|402/i.test(item.error || '');
    });
    return '<section class="batch-organize-banner" data-batch-organize data-batch-state="' + esc(batch.state) + '" aria-label="批量整理进度">' +
      '<div class="batch-status-heading"><strong>' + headingText +
      '</strong>' + (batch.state === 'running'
        ? button('取消批量整理', 'batch-organize-cancel', {small:true, quiet:true})
        : ('<div>' + (hasModelFailure ? button('前往模型配置', 'open-models', {small:true, primary:true}) + ' ' : '') + button('收起提示', 'batch-organize-dismiss', {small:true, quiet:true}) + '</div>')) +
      '</div><p role="status">' + statusCopy +
      '</p>' + (batch.state === 'done' ? '' : '<progress aria-label="批量整理进度" value="' + barVal + '" max="' + barMax + '"></progress>') +
      '<details data-persistent-disclosure="batch-progress"><summary>查看各讲进度</summary><ul class="batch-progress-items">' +
      (batch.items || []).map(function (item) {
        // Running rows carry the lecture-wide stage label; keep a stage that
        // already reads as an activity untouched instead of double-prefixing.
        var runningStage = String(item.stage || "").indexOf('正在') === 0
          ? item.stage : '正在' + (item.stage || '转写并生成笔记');
        var itemStage = item.state === 'done' ? '✓ 已完成'
          : item.state === 'pending' ? '等待中'
          : item.state === 'cancelled' ? (item.stage || '已取消')
          : item.state === 'failed' ? (item.stage || '整理未完成')
          : runningStage;
        return '<li><div><strong>' + esc(item.course_title) + ' · ' + esc(item.title) + '</strong><small>' +
          esc(itemStage) +
          (item.state === 'running' ? ' · ' + Math.round(item.progress_percent || 0) + '%' : '') + '</small>' +
          (item.error ? '<small class="batch-item-error">' + esc(item.error) + '</small>' : '') + '</div>' +
          (item.state === 'done' ? button('查看笔记', 'batch-open-note', {small:true, quiet:true, attrs:{'data-course':item.course_id, 'data-recording':item.recording_id}}) : '') + '</li>';
      }).join('') + '</ul></details></section>';
  }

  function lastCampusSync() {
    var times = state.campus.courses.map(function (course) { return course.synced_at; }).filter(function (value) { return parseTime(value); });
    times.sort(function (a, b) { return parseTime(b) - parseTime(a); });
    return times.length ? (Date.now() - parseTime(times[0]).getTime() < 60000 ? '刚刚' : friendlyTime(times[0])) : '尚未更新';
  }

  function localRecordingsNoticeBar() {
    var lr = state.localRecordings;
    if (!lr || lr.count <= 0 || lr.open) return "";
    return '<div class="local-recordings-banner" data-local-recordings-banner role="status">' +
      '<div><strong>本机已保存 ' + esc(lr.count) + ' 节课程录像</strong><span> · 共占用 ' + esc(formatTransfer(lr.totalBytes)) +
      (lr.effectiveDir ? ' · 目录：' + esc(lr.effectiveDir) : '') + '</span></div>' +
      '<div class="local-recordings-banner-actions">' +
      button("查看已下载录像", "toggle-local-recordings", { small: true }) +
      button("打开下载目录", "local-storage-open-dir", { small: true, quiet: true }) +
      button("全部清理", "local-storage-clear-all", { small: true, quiet: true }) +
      '</div></div>';
  }

  function localRecordingsPanel(alwaysVisible) {
    var lr = state.localRecordings;
    if (!alwaysVisible && (!lr || !lr.open)) return "";
    var rows = (lr.items || []).map(function (item) {
      var fileName = String(item.path || "").split(/[\\/]/).pop() || item.path || "";
      return '<li class="local-recording-item" data-local-recording-item="' + esc(item.recording_id) + '">' +
        '<div class="local-recording-info">' +
        '<strong>' + esc(item.course_title) + ' · ' + esc(item.title) + '</strong>' +
        '<small>' + esc(item.date || "日期未记录") + ' · ' + esc(formatTransfer(item.size_bytes)) +
        (fileName ? ' · <span class="local-recording-filename" title="' + esc(item.path) + '">' + esc(fileName) + '</span>' : '') +
        '<span class="local-recording-path-meta"> (' + esc(item.path) + ')</span></small>' +
        '</div>' +
        '<div class="local-recording-actions">' +
        button("在文件夹中显示", "local-storage-reveal", { small: true, quiet: true, attrs: { "data-recording": item.recording_id } }) +
        '<a class="btn btn-small btn-quiet" href="/api/recordings/' + encodeURIComponent(item.recording_id) + '/video" download>导出 MP4</a>' +
        button("删除本地文件", "local-storage-delete-one", { small: true, quiet: true, attrs: { "data-recording": item.recording_id } }) +
        '</div></li>';
    }).join("");
    return '<section class="panel local-recordings-panel" data-local-recordings-panel aria-label="已下载录像与本地存储设置">' +
      '<div class="local-recordings-header"><div><h2>已下载录像与本地存储设置</h2>' +
      '<p class="form-note">默认不保留原始录像与音频：整理笔记时仅临时提取音轨与课堂画面，整理完成后立即自动删除本地视频文件。</p></div>' +
      (!alwaysVisible ? button("收起", "toggle-local-recordings", { small: true, quiet: true }) : "") + '</div>' +
      '<div class="local-recordings-settings">' +
      '<label class="local-recordings-toggle"><input type="checkbox" data-local-storage-save-toggle' + (lr.saveLocally ? " checked" : "") + '>' +
      '<span>整理笔记后也在本地保留原始录像（默认关闭，开启后每节课约占用 500–800 MB）</span></label>' +
      '<details class="lecture-more-actions local-recordings-dir-disclosure" data-persistent-disclosure="local-recordings-dir">' +
      '<summary>更改下载保存目录（当前：' + esc(lr.effectiveDir || "默认目录") + '）</summary>' +
      '<div class="local-recordings-dir-row">' +
      '<label for="local-recordings-dir-input">录像下载保存目录</label>' +
      '<div class="local-recordings-dir-controls">' +
      '<input id="local-recordings-dir-input" type="text" class="text-input" data-local-storage-dir-input value="' + esc(lr.downloadDir) + '" placeholder="' + esc(lr.effectiveDir || "默认：数据目录下的 recordings-downloads") + '">' +
      button("选择文件夹…", "local-storage-pick-dir", { small: true, quiet: true }) +
      button("保存目录", "local-storage-save-dir", { small: true }) +
      button("打开目录", "local-storage-open-dir", { small: true, quiet: true }) +
      '</div><small class="form-note hint">当前生效目录：' + esc(lr.effectiveDir || "默认目录") + '</small></div></details></div>' +
      '<div class="local-recordings-list-section">' +
      '<div class="local-recordings-list-header"><h3>本机已保存录像（' + esc(lr.count) + ' 节 · 共 ' + esc(formatTransfer(lr.totalBytes)) + '）</h3>' +
      (lr.count > 0 ? button("清空全部本地录像", "local-storage-clear-all", { small: true, quiet: true }) : "") + '</div>' +
      (lr.count > 0 ? '<ul class="local-recordings-list">' + rows + '</ul>' : '<p class="form-note hint">当前本机没有保存任何原始录像文件，不占用额外磁盘空间。</p>') +
      '</div></section>';
  }

  function campusOverwriteExportHint(forCredentialEdit) {
    if (!state.campus.courses.length) return "";
    var isOutOfSync = Boolean(state.campus.catalogOutOfSync);
    if (!isOutOfSync && !forCredentialEdit) return "";
    var stale = state.campus.staleCourses || [];
    var staleNames = stale.slice(0, 3).map(function (item) {
      return String(item.title || item.id || "").replace(/\([^()]*学期[^()]*\)$/, "").trim();
    }).filter(Boolean);
    var detailText = stale.length > 0
      ? "检测到有 " + stale.length + " 门课程记录" + (staleNames.length ? "（" + staleNames.join("、") + (stale.length > 3 ? " 等" : "") + "）" : "") + "与当前教学网不同步。覆盖清理或更新课程目录将直接删除这些旧记录，请先按需导出备份："
      : "当前教学网课程与已有记录不同步（如换学期或更换教学网账号）。更新课程目录将直接覆盖并删除非当前教学网的旧课程记录，请先导出备份：";
    return '<div class="campus-export-hint" role="alert" data-campus-export-hint>' +
      '<div class="campus-export-hint-copy">' +
      '<strong>⚠️ 教学网课程与当前记录不同步，覆盖更新可能会导致数据丢失</strong>' +
      '<span>' + esc(detailText) + '</span>' +
      '</div>' +
      '<div class="campus-export-hint-actions">' +
      button("批量导出 PDF 笔记 (.zip)", "export-all-notes-pdf", { small: true, quiet: true }) +
      button("打开本地课程目录", "open-local-courses-dir", { small: true, quiet: true }) +
      (stale.length > 0 ? button("覆盖并清理旧课程", "prune-stale-courses", { small: true, quiet: true }) : "") +
      '</div></div>';
  }

  function campusDashboardScreen() {
    var query = state.campus.query.trim().toLowerCase();
    var visible = state.campus.courses.filter(function (course) {
      return !query || (course.title + " " + course.code + " " + course.term + " " + course.id).toLowerCase().includes(query);
    }).slice().sort(function (a, b) {
      var aHas = Number((a.recordings || 0) > 0);
      var bHas = Number((b.recordings || 0) > 0);
      return bHas - aHas;
    });
    var titleCounts = {};
    state.campus.courses.forEach(function (course) { titleCounts[course.title] = (titleCounts[course.title] || 0) + 1; });
    var items = visible.map(function (course) {
      var titleTerm = String(course.title || '').match(/\(([^()]*学期[^()]*)\)$/);
      var hasDuplicateTitle = titleCounts[course.title] > 1;
      var classLabel = hasDuplicateTitle && course.id
        ? ' · 教学班 …' + esc(course.id.slice(-6)) : '';
      var deltaLabel = formatDeltaSummary(course.last_delta);
      var deltaBadge = deltaLabel ? ' <span class="course-delta-badge" data-course-delta>' + esc(deltaLabel) + '</span>' : '';
      var tracked = isCourseTracked(course.id);
      var trackBadge = tracked
        ? ' <span class="course-track-badge" data-course-track-badge><span class="track-pulse-dot" aria-hidden="true"></span>自动追踪中</span>'
        : '';
      var trackBtn = '<button type="button" class="btn btn-small course-track-btn' + (tracked ? ' is-tracked' : '') +
        '" data-action="toggle-course-track" data-course="' + esc(course.id) + '" data-course-track-toggle="' + esc(course.id) +
        '" aria-pressed="' + (tracked ? 'true' : 'false') + '" title="' +
        (tracked ? '已开启后台自动追踪，点击取消追踪' : '开启后后台定时检查本课课件、作业与新回放') + '">' +
        (tracked ? '<span class="track-pulse-dot" aria-hidden="true"></span>已开启追踪' : '+ 自动追踪') + '</button>';
      return '<article class="course-card' + (tracked ? ' is-tracked-course' : '') + '" data-course-card="' + esc(course.id) + '"><div class="course-content"><span class="course-meta">' + esc(titleTerm ? titleTerm[1] : course.term || '本学期') + classLabel + deltaBadge + trackBadge + '</span><h3>' + formatCourseTitleMarkup(course.title) +
        '</h3><p>' + esc(course.recordings) + ' 节课 · ' + esc(course.assignments || 0) + ' 项正式作业 · ' + esc(course.announcements || 0) + ' 则通知' +
        (course.calendar_items ? ' · ' + esc(course.calendar_items) + ' 项日历事项' : '') + '</p>' +
        (course.read_only ? '<p class="course-readonly-note" role="status">' + cachedCourseNotice(course) + '</p>' : '') + '</div><div class="course-card-footer">' + trackBtn +
        button("查看课程", "open-campus-course", { small: true, quiet: true, attrs: { "data-course": course.id } }) + '</div></article>';
    }).join("");
    var credentials = state.campus.configured && !state.recordings.editingCredentials
      ? '<p>教学网账号已验证并受本机加密保护。 ' +
        button("更换账号", "recording-edit-credentials", { small: true, quiet: true }) + ' ' +
        button("清除本地密钥", "recording-clear-credentials", { small: true, quiet: true }) + '</p>' + campusOverwriteExportHint()
      : (state.campus.cookieKeyMissing
          ? '<p class="auto-track-notice" role="status">🔑 当前尚未保存本地凭据密钥，请重新输入一次教学网学号与密码。</p>' + recordingCredentialsForm()
          : recordingCredentialsForm());
    if (state.view === "campus-account") {
      return breadcrumb([{label:"我的课程", action:"open-dashboard"}, {label:"教学网连接"}]) +
        '<div class="topline"><div><h1 class="page-title">教学网连接</h1><p class="page-desc">连接北大教学网获取课程，所有课件与笔记均保存在本机。</p></div></div>' +
        campusSetupGuide() + recordingTaskStatus() + backgroundSyncStatus() +
        (state.recordings.error ? '<p role="alert">' + esc(state.recordings.error) + '</p>' : '') +
        '<section class="panel"><h2>教学网账号</h2>' + credentials + '</section>' +
        (state.campus.home || state.campus.homeSync ? campusHomeControls() : '') +
        localRecordingsPanel(true);
    }
    var empty = state.campus.courses.length ? (visible.length ? '<div class="course-grid">' + items + '</div>' : '<div class="empty-materials">没有找到匹配的课程。</div>') :
      (state.campus.loading && state.campus.configured ? '<div class="empty-materials">正在加载课程目录…</div>' :
       '<div class="empty-materials">还没有课程。连接教学网账号后，点击「更新课程目录」。</div>');
    var trackedCount = state.autoTrack ? state.autoTrack.trackedCourseIds.length : 0;
    var autoTrackOpen = Boolean(state.autoTrack && state.autoTrack.open);
    var autoTrackBtnMarkup = autoTrackOpen
      ? '收起自动追踪'
      : (trackedCount > 0
        ? '<span class="track-pulse-dot" aria-hidden="true"></span>自动追踪中 (' + esc(trackedCount) + ')'
        : '⏱ 自动追踪课程');
    var modelInfo = typeof modelReadinessInfo === 'function' ? modelReadinessInfo() : { ready: false };
    return '<div class="topline"><div><h1 class="page-title">我的课程</h1></div></div>' +
      (!(state.campus.configured && modelInfo.ready && (state.campus.courses.length || state.campus.loading)) ? campusSetupGuide(false) : '') +
      '<section class="panel" aria-label="课程列表"><div class="course-list-toolbar"><div class="course-list-meta"><span class="course-count">' + (query ? '找到 ' + visible.length : state.campus.courses.length) + ' 门课程</span><span class="course-last-sync"> · 上次更新：' + esc(lastCampusSync()) + '</span>' +
      (trackedCount > 0 ? '<span class="course-auto-track-meta"><span class="track-pulse-dot" aria-hidden="true"></span>自动追踪 ' + esc(trackedCount) + ' 门</span>' : '') +
      '</div><div class="course-list-actions">' +
      (state.campus.courses.length ? '<input class="lesson-filter-input campus-search-input" type="search" data-campus-search aria-label="查找课程" placeholder="搜索课程…" value="' + esc(state.campus.query) + '">' : '') +
      button("更新课程目录", "recording-sync", { small: true, primary: true,
        disabled: !state.campus.configured,
        attrs: { "aria-disabled": state.recordings.backgroundPending || state.recordings.syncPending || state.recordings.task.state === "running" || state.recordings.background.state === "running",
          "style": state.recordings.backgroundPending || state.recordings.syncPending || state.recordings.task.state === "running" || state.recordings.background.state === "running" ? "opacity:.55;cursor:not-allowed;transform:none" : "" }
      }) +
      (state.campus.courses.length ? '<span class="auto-track-action-wrap">' +
        '<button type="button" class="btn btn-small auto-track-toolbar-btn' + (trackedCount > 0 ? ' is-active' : '') + (autoTrackOpen ? ' is-open' : '') + '" data-action="toggle-auto-track-panel" aria-expanded="' + (autoTrackOpen ? 'true' : 'false') + '">' +
        autoTrackBtnMarkup + '</button></span>' : '') +
      (state.campus.courses.some(function (course) { return course.recordings > 0; }) ?
        button(state.batchOrganize && state.batchOrganize.open ? '收起一键整理' : '一键整理笔记', 'toggle-batch-organize',
          {small:true, quiet:Boolean(state.batchOrganize && state.batchOrganize.open)}) : '') +
      (state.campus.courses.length ? '<span class="export-all-notes-action-wrap">' + button(
        "导出全部 PDF (.zip)",
        "export-all-notes-pdf",
        { small: true, quiet: true }
      ) + '</span>' : '') +
      ((state.localRecordings.open || state.localRecordings.count > 0) ? '<span class="local-recordings-action-wrap">' + button(
        state.localRecordings.open
          ? "收起已下载录像"
          : "已下载录像 (" + state.localRecordings.count + ")",
        "toggle-local-recordings",
        { small: true, quiet: true }
      ) + '</span>' : '') +
      '</div></div>' + campusOverwriteExportHint() + localRecordingsNoticeBar() + localRecordingsPanel(false) + autoTrackPanel() + recordingTaskStatus() + backgroundSyncStatus() + batchOrganizeStatus() + batchOrganizePanel() +
       (state.campus.courses.some(function (course) { return !course.synced_at; }) ?
         '<p class="form-note hint">部分课程尚未更新，请先更新目录。</p>' : '') +
       (state.recordings.error ? '<p role="alert">' + esc(state.recordings.error) + '</p>' : '') +
      empty + '</section>';
  }

  function campusHomeControls() {
    var syncBusy = state.recordings.syncPending || state.recordings.backgroundPending || state.recordings.task.state === "running";
    if (state.campus.demoConnection) return '';
    var busy = Boolean(state.campus.workspaceBusy);
    var waitHint = busy ? '<p role="status" class="form-note">' + esc(COPY.notion.workspace_wait_hint) + '</p>' : '';
    var loading = Boolean(state.campus.parentsLoading);
    var homeSync = state.campus.homeSync;
    var blocked = homeSync && ['pulled', 'current', 'absent'].indexOf(homeSync.status) === -1;
    var header = '<div class="home-setup-header"><span class="home-notion-mark" aria-hidden="true">N</span><div><p class="eyebrow">NOTION 学习空间</p><h2>' +
      (blocked ? '恢复学习主页' : state.campus.home ? '你的学习主页' : '创建学习主页') + '</h2><p>让课程、讲次与学习记录都有自己的位置。</p></div></div>';
    var error = state.campus.parentError ? '<p role="alert" class="form-note home-setup-error">' + esc(state.campus.parentError) + '</p>' : '';
    if (!state.connection.connected) return '<section class="panel home-setup">' + header +
      '<div class="home-connect"><p>连接 Notion 后，选择一个页面作为学习主页的保存位置。</p>' +
      button("连接 Notion", "connect-notion", { primary: true }) + '</div></section>';
    if (blocked) {
      var recoveryMessages = {
        notion_reconnect_required: '账号已保存学习主页，但当前 Notion 授权无法访问。请重新连接 Notion，并授权原主页及其父页面；授权完成后会自动恢复。',
        notion_home_archived: '账号已有学习主页，但页面已归档或在回收站。请在 Notion 恢复页面后重试，软件会自动恢复主页配置。',
        notion_home_changed: '账号已有学习主页，但它在 Notion 中的位置或页面状态已变化。请核对原主页和父页面后重试。',
        notion_unavailable: '暂时无法连接 Notion，已有主页配置保留。连接恢复后重试，软件会自动恢复主页。',
        local_write_failed: '已找到账号主页，但本机配置保存失败。请检查本机存储后重试。',
        account_unavailable: '暂时无法读取账号主页配置。请检查账号登录与连接后重试。'
      };
      var recoveryOptions = state.campus.parents.map(function (page) {
        return '<option value="' + esc(page.id) + '"' + (page.id === state.campus.parentId ? ' selected' : '') + '>' + esc(page.title) + '</option>';
      }).join('');
      return '<section class="panel home-setup" data-home-recovery><div>' + header +
        '<p role="status" class="form-note">' + esc(recoveryMessages[homeSync.status] || '账号主页配置未能确认，请重新读取。') + '</p><div class="home-setup-actions">' +
        (homeSync.status === 'notion_reconnect_required' ? button('重新连接 Notion', 'connect-notion', {primary:true, disabled:loading || busy}) : '') +
        button(loading ? '正在自动恢复…' : '重试恢复主页', 'campus-load-parents', {small:true, disabled:loading || busy}) + '</div>' +
        (recoveryOptions ? '<details class="home-setup-advanced"><summary>使用新的学习主页</summary><p>原 Notion 页面保留，账号将使用新主页。</p><label>新主页保存位置<select data-campus-parent aria-label="新主页保存位置"' + (busy ? ' disabled' : '') + '>' + recoveryOptions + '</select></label>' +
          button(busy ? '正在创建并验证…' : '在所选位置新建主页', 'campus-replace-workspace', {small:true, disabled:loading || busy || state.recordings.task.state === 'running'}) + '</details>' : '') + '</div></section>';
    }
    if (loading && !state.campus.home) return '<section class="panel home-setup">' + header +
      '<p role="status" class="form-note">正在读取账号配置并自动恢复已有学习主页…</p></section>';
    var refresh = button(loading ? "正在读取页面…" : "刷新可用页面", "campus-load-parents", { small: true, quiet: true, disabled: loading || busy });
    if (state.campus.home) {
      var destinations = state.campus.parents.filter(function (page) { return page.id !== state.campus.home.id; });
      var moveOptions = destinations.map(function (page) {
        return '<option value="' + esc(page.id) + '"' + (page.id === state.campus.parentId ? ' selected' : '') + '>' + esc(page.title) + '</option>';
      }).join("");
      return '<section class="panel home-setup home-setup-ready">' + header +
        '<p class="home-ready-badge">✓ 学习主页已就绪</p>' +
        (state.campus.workspace && state.campus.workspace.active === false ? '<p role="status" class="form-note hint">新主页仍待验证，当前继续使用原主页。</p>' : '') + '<div class="home-setup-actions">' +
        button("打开主页", "campus-open-home", { primary: true, attrs: { "data-url": state.campus.home.url || "" } }) +
        (state.campus.workspace ? button(busy ? "正在更新…" : "更新目录", "campus-create-workspace", { small: true, disabled: busy || state.recordings.task.state === "running", attrs: { "aria-busy": String(busy) } }) : '') +
        button("生成全部课程总结", "campus-summaries", {
          small: true, quiet: true, disabled: busy || state.recordings.task.state === "running",
          attrs: { "data-url": state.campus.home.url || "" }
        }) +
        '</div><details class="home-setup-advanced" data-persistent-disclosure="home-location"><summary>管理保存位置</summary><div class="home-location-controls">' +
        (moveOptions ? '<label>主页所在位置<select data-campus-parent aria-label="学习主页所在位置"' + (busy ? ' disabled' : '') + '>' + moveOptions + '</select></label>' +
          button("移动现有主页", "campus-move-home", { small: true, disabled: busy || state.campus.parentId === state.campus.home.parent_id }) :
          '') + refresh + '</div>' + '</details>' + error + '</section>';
    }
    var options = state.campus.parents.map(function (page) {
      return '<option value="' + esc(page.id) + '"' + (page.id === state.campus.parentId ? ' selected' : '') + '>' + esc(page.title) + '</option>';
    }).join("");
    var selected = state.campus.parents.find(function (page) { return page.id === state.campus.parentId; });
    return '<section class="panel home-setup">' + header + '<div class="home-setup-body"><div class="home-destination">' +
      '<label for="home-parent">1 · 选择保存位置</label><p>选择已授权给应用的 Notion 页面。</p>' +
      (options ? '<select id="home-parent" data-campus-parent aria-label="Notion 父页面"' + (busy || loading ? ' disabled' : '') + '>' + options + '</select>' :
        '<div class="home-pages-empty">' + (loading ? '正在读取可用页面…' : '还没有可选择的页面，请读取页面列表或检查 Notion 授权。') + '</div>') + refresh +
      '</div><div class="home-preview"><small>主页结构预览</small><h3>我的学习空间</h3><ul><li>课程与课堂讲次</li><li>作业、通知与课程资料</li><li>课堂笔记与课程总结</li></ul></div></div>' +
      '<div class="home-setup-footer"><div><strong>2 · 创建学习主页</strong><p>' +
      (!campusCatalogReady() ? '先更新课程目录，至少一门课程读取成功后即可创建。Notion 保存位置会保留。' :
        selected ? '保存到「' + esc(selected.title) + '」下面' : '选好保存位置后即可创建') + '</p>' +
      (!campusCatalogReady() ? button(syncBusy ? '正在更新课程目录…' : '先更新课程目录', 'recording-sync', {small:true, disabled:syncBusy || !state.campus.configured}) : '') + '</div>' +
      button(busy ? "正在创建并验证…" : "创建学习主页", "campus-create-workspace", {
        primary: true, disabled: busy || loading || !state.campus.parentId || !campusCatalogReady() || state.recordings.task.state === "running", attrs: { "aria-busy": String(busy) }
      }) + '</div>' + waitHint + error + '</section>';
  }

  function directOssConsent(lectureId) {
    return '<label class="recording-oss-consent"><input type="checkbox" data-recording-direct-oss data-action="toggle-direct-oss-consent" data-testid="direct-oss-consent-' + esc(lectureId || '') + '"' +
      (state.recordings.directOss ? ' checked' : '') +
      '><span>内存流式提取音轨后直接发送至已配置的 ASR 转写接口（不保存临时视频）</span></label>';
  }

  function createWorkspace(replaceExisting) {
    if (state.campus.workspaceBusy) return;
    var parentId = replaceExisting ? state.campus.parentId : state.campus.workspace && state.campus.workspace.parent_id || state.campus.parentId;
    if (!parentId) { state.campus.parentError = "请先选择 Notion 保存位置。"; render(); return; }
    var epoch = accountQuotaEpoch;
    var updating = Boolean(state.campus.workspace);
    state.campus.workspaceBusy = true;
    state.campus.parentError = "";
    render();
    var payload = { parent_page_id: parentId };
    if (replaceExisting) payload.replace_existing = true;
    return requestJson("/api/campus/notion/workspace", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body || !result.body.id || result.body.active !== true) throw new Error(result.body && result.body.detail || "新版目录未完成验证。");
      state.campus.workspace = result.body;
      state.campus.home = result.body;
      state.campus.homeSync = {status:'pulled'};
      state.campus.parentId = result.body.parent_id;
      showToast(updating ? "学习主页目录已更新。" : "学习主页已创建并验证，可以开始整理课程。");
    }).catch(function (error) {
      if (epoch !== accountQuotaEpoch) return;
      state.campus.parentError = campusReadError(error.message || "新版目录暂时无法同步。");
    }).finally(function () { if (epoch === accountQuotaEpoch) { state.campus.workspaceBusy = false; render(); } });
  }

  function campusMaterial(item, courseId) {
    var category = item.category === "assignment" ? "作业或测验" : item.category === "information" ? "课程信息" : "资料";
    var links = (item.files || []).map(function (name, index) {
      return '<a class="material-file-link" href="/api/campus/courses/' + encodeURIComponent(courseId) +
        '/materials/' + item.index + '/files/' + index + '" download>下载 ' + esc(name) + '</a>';
    }).join(" ");
    return '<li><strong>' + esc(item.title) + '</strong> <span class="course-meta">' + category +
      '</span>' + (links ? '<div>' + links + '</div>' : item.body_text ? '<p class="material-body-text">' + esc(item.body_text) + '</p>' : '<small>教学网未提供可下载附件；可在教学网查看。</small>') + '</li>';
  }

  function assignmentUrgency(item) {
    if (!item || item.source === 'calendar') return '';
    var dueDate = parseTime(item.due_at);
    if (!dueDate) return '';
    var hoursLeft = (dueDate.getTime() - Date.now()) / 3600000;
    if (hoursLeft > 0 && hoursLeft <= 24) return 'today';
    if (hoursLeft > 24 && hoursLeft <= 72) return 'soon';
    return '';
  }

  function proCenterBadge() {
    return '';
  }

  function campusOfficialSections(course, section) {
    var lite = false;
    var tierNotice = '';
    var updated = course.synced_at_label || "尚未更新";
    var noticeState = course.section_states && course.section_states.announcements;
    var noticeStatus = course.announcements_status === "unavailable" ? "教学网未开放通知功能，仍需查看教学网。" :
      course.announcements_status === "error" ? "通知暂时读取失败，请到教学网核对。" :
      course.announcements_status === "not_synced" ? "通知尚未同步，请先更新目录。" : "教学网当前没有正式通知。";
    var noticeItems = (course.announcements || []).map(function (item) {
      return '<li><strong>' + esc(item.title) + '</strong><small>发布：' + esc(item.posted_at_label || item.posted_at || "未提供") +
        ' · 来源：教学网正式通知 · 更新：' + esc(updated) + '</small><p>' + esc(item.body_text || "正文未提供，请查看教学网。") +
        '</p><a href="' + esc(item.source_url || '#') + '" target="_blank" rel="noopener noreferrer">查看这条通知</a></li>';
    }).join("");
    var noticeWarning = course.announcements_status === "error" || course.announcements_status === "unavailable" ||
      course.announcements_status === "not_synced";
    if (noticeState) {
      noticeWarning = ["not_synced", "error", "cached"].indexOf(noticeState.state) >= 0;
      if (noticeWarning) noticeStatus = noticeState.message;
    }
    var oralNoticeGroups = Array.isArray(course.oral_notices) ? course.oral_notices : [];
    var oralNoticeBanner = !oralNoticeGroups.length
      ? '<p class="form-note hint" data-oral-notice-hint>💡 整理课堂笔记后，老师在回放中口述的随堂测验口令、DDL 变更与考点将自动汇总于此。</p>'
      : '';
    var oralNoticesHtml = oralNoticeGroups.length ? (
      '<div class="course-oral-notices-box" data-course-oral-notices>' +
      '<h3>📢 课堂回放提取的老师口述通知与考点</h3>' +
      oralNoticeGroups.map(function (grp) {
        var bullets = (grp.items || []).map(function (it) { return '<li>' + esc(it) + '</li>'; }).join('');
        return '<div class="course-oral-notice-group"><div class="course-oral-notice-head"><strong>' +
          esc((grp.date ? grp.date + ' · ' : '') + (grp.lecture_title || '')) + '</strong>' +
          (grp.lecture_id ? button('查看本讲笔记', 'campus-open-lesson', {small:true, quiet:true, attrs:{'data-recording':grp.lecture_id}}) : '') +
          '</div><ul>' + bullets + '</ul></div>';
      }).join('') + '</div>'
    ) : '';
    var notices = '<section class="panel" id="course-notices"><h2>通知中心' + proCenterBadge() + '</h2>' + tierNotice + oralNoticeBanner + oralNoticesHtml +
      (noticeWarning ? '<p class="form-note hint">' + esc(noticeStatus) + '</p>' : '') +
      (noticeItems ? '<ul class="official-list">' + noticeItems + '</ul>' :
        (noticeWarning ? '' : '<p>' + esc(noticeStatus) + '</p>')) + '</section>';
    var assignmentItems = (course.assignments || []).map(function (item) {
      var status = (state.campus.assignmentStatuses || {})[item.content_id];
      var source = item.content_id && item.source !== "calendar"
        ? '/api/campus/courses/' + encodeURIComponent(course.id) +
          '/assignments/' + encodeURIComponent(item.content_id) + '/source'
        : '';
      var deadlineMention = !item.due_at_label && !item.due_at && !item.teacher_deadline_quote && String(item.instructions || '').match(/((?:\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})[^\n。；;]{0,18}(?:前|截止|提交))/);
      var due = item.due_at_label || item.due_at || (item.teacher_deadline_quote
        ? '老师原文写 ' + item.teacher_deadline_quote + '（系统无独立截止字段，请打开原要求核对）'
        : deadlineMention ? deadlineMention[1] + '（正文提及）' : '教学网未公布');
      var urgencyKind = assignmentUrgency(item);
      var urgency = urgencyKind ? '<span class="status-badge urgent" data-deadline-urgency="' + urgencyKind + '">' +
        (urgencyKind === 'today' ? '今天截止' : '即将截止') + '</span>' : '';
      var caveats = [];
      if (item.source === 'calendar') caveats.push('来自教学网日历提醒，尚未关联到正式作业；具体要求与截止时间请到教学网课程中核对');
      else {
        if (!source) caveats.push('请到教学网课程中核对原要求');
        if (!item.content_id) caveats.push('尚无可验证的提交目标');
      }
      var sourceLinks = (item.source_links || []).filter(function (link) {
        return link && /^https:\/\/[^/]+/i.test(link.url || '') && link.label;
      }).map(function (link) {
        return '<a href="' + esc(link.url) + '" target="_blank" rel="noopener noreferrer">' +
          esc(link.label) + '</a>';
      }).join(' · ');
      var prepLabel = "准备作业文件 & AI 识别";
      return '<li><div class="official-item-header"><strong>' + esc(item.title) + '</strong>' + urgency + '<span class="status-badge ' +
        (item.source === 'calendar' ? 'pending">日历提醒' : 'indexed">正式作业') + '</span></div><small>' + (item.source === 'calendar' ? '日历时间：' : '截止：') + esc(due) +
        ' · 来源：教学网' + (item.source === "calendar" ? "日历" : "作业") +
        ' · 更新：' + esc(updated) + '</small><p>' + esc(item.instructions || "要求未提供，请打开教学网原页面核对。") +
        '</p>' +
        ((item.files || []).length ? '<p>附件：' + esc(item.files.join("、")) + '</p>' : '') +
        (sourceLinks ? '<p>老师原页面链接：' + sourceLinks + '</p>' : '') +
        '<div class="official-item-actions">' + (source ? '<a href="' + esc(source) + '" target="_blank" rel="noopener noreferrer">查看教学网原要求</a>' :
          '') +
        (source ? button(status && status.loading ? '正在读取提交状态…' : '读取教学网提交状态', 'campus-assignment-status', {
          small: true, disabled: Boolean(status && status.loading), attrs: { 'data-assignment': item.content_id } }) : '') +
        (item.content_id && item.source !== "calendar" ? button(prepLabel, "campus-prepare-assignment", { small: true, disabled: lite,
          attrs: { "data-assignment": item.content_id } }) : '') + '</div>' +
        (status ? '<p role="status">' + esc(status.label || '教学网当前提交状态未确认') +
          (status.checked_at ? ' · 核对时间：' + esc(status.checked_at) : '') + '</p>' +
          (status.error ? '<p role="alert">' + esc(status.error) + '</p>' : '') : '') +
        (caveats.length ? '<div class="official-item-footer">' + caveats.map(function (text) {
          return '<span class="official-caveat">' + esc(text) + '</span>';
        }).join('') + '</div>' : '') + '</li>';
    }).join("");
    var announcementTaskItems = (course.announcement_tasks || []).map(function (item) {
      return '<li><strong>' + esc(item.notice_title) + '</strong><small>公告待办 · 截止：' +
        esc(item.due_at_label) + '</small><p>' + esc(item.instructions) +
        '</p><a href="' + esc(item.source_url) + '" target="_blank" rel="noopener noreferrer">查看老师公告原文</a>' +
        '<small>老师公告中的交付要求；请按原文的方式完成，不代表教学网有独立提交入口。</small></li>';
    }).join("");
    var handbookItems = (course.handbook_reviews || []).map(function (item) {
      var source = '/api/campus/courses/' + encodeURIComponent(course.id) +
        '/materials/' + encodeURIComponent(item.material_index) + '/files/' + encodeURIComponent(item.file_index);
      var pages = (item.pages || []).map(function (page) {
        return '<details><summary>PDF 第 ' + esc(page.page) + ' 页 · 手册原文摘录</summary><p>' +
          esc(page.excerpt) + (page.truncated ? '…（请打开原 PDF 阅读余下内容）' : '') + '</p></details>';
      }).join('');
      var preview = (item.time_preview || []).map(function (cue) {
        return '<li><strong>PDF 第 ' + esc(cue.page) + ' 页</strong> · ' + esc(cue.text) + '</li>';
      }).join('');
      var presence = (item.presence_preview || []).map(function (cue) {
        return '<li><strong>' + esc(cue.label) + ' · PDF 第 ' + esc(cue.page) + ' 页</strong> · ' +
          esc(cue.text) + '<small>' + esc(cue.note) + '</small></li>';
      }).join('');
      return '<li><strong>' + esc(item.title) + '</strong><small>课程手册要求，需向老师或助教核对；' +
        '不是教学网正式作业，也没有已验证的提交入口或系统截止。</small><p>' + esc(item.status) +
        '</p>' + (preview ? '<p>手册原文中的时间／提交线索（未核对是否仍有效）：</p><ul>' + preview + '</ul>' : '') +
        (presence ? '<p>笔记无法代替的参与事项（到场方式待核对）：</p><ul>' + presence + '</ul>' : '') +
        pages + (item.pages && item.pages.length ? '' :
          button('读取手册并显示要求', 'campus-review-handbook', { small: true, attrs: {
            'data-material': item.material_index, 'data-file': item.file_index } })) +
        '<a href="' + esc(source) + '" download>下载课程手册 PDF（核对原页）</a></li>';
    }).join("");
    var selected = state.campus.submission.selectedId;
    var selectedItem = (course.assignments || []).find(function (item) { return item.content_id === selected; });
    var draft = state.campus.submission.draft;
    var formValues = state.campus.submission.form;
    var preparation = "";
    if (selectedItem) {
      var extracted = state.campus.submission.analysis;
      var filenameSuggestion = '';
      if (extracted && extracted.analysis && formValues.submission_mode === 'zip') {
        var filenameRule = (extracted.analysis.filename_rules || []).find(function (row) {
          return String(row.quote || '').trim();
        });
        if (filenameRule) {
          var suggestedName = String(filenameRule.quote).trim();
          var quotedName = suggestedName.match(/[“「『"'`]([^”」』"'`]+)[”」』"'`]/);
          suggestedName = quotedName ? quotedName[1] : suggestedName.replace(/^.*?(?:命名为|文件名[为是：:\s]*|命名[：:\s]+)/, '');
          suggestedName = suggestedName.replace(/^[\s“”「」『』"'`（(：:]+|[\s“”「」『』"'`）)。，,；;！!：:]+$/g, '');
          var zipEnding = suggestedName.match(/\.zip(?=$|[\s”」』"'`。，,；;！!）)])/i);
          if (zipEnding) suggestedName = suggestedName.slice(0, zipEnding.index + 4);
          if (suggestedName) {
            if (!/\.zip$/i.test(suggestedName)) suggestedName += '.zip';
            filenameSuggestion = button('套用命名规则', 'campus-apply-filename-rule', {
              small: true, quiet: true, disabled: lite, attrs: { 'data-filename-template': suggestedName }
            });
          }
        }
      }
      var analysisView = extracted && extracted.analysis ? '<div data-assignment-requirements><h4>已核对原文的交付要求</h4><ul>' +
        extracted.analysis.requirements.concat(extracted.analysis.filename_rules).map(function (row) {
          return '<li>' + esc(row.quote) + '</li>';
        }).join('') + '</ul>' + filenameSuggestion + '<p>原文指定格式：' +
        esc(extracted.analysis.file_formats.map(function (row) { return row.value; }).join('、') || '未明确，请核对') +
        '</p>' + extracted.analysis.uncertainties.map(function (item) { return '<p class="form-note hint">待核对：' + esc(item) + '</p>'; }).join('') +
        (extracted.reused ? '<p class="form-note">（复用已保存的分析结果）</p>' : '') + '</div>' : '';
      var rarHint = /\.rar\b/i.test(selectedItem.instructions || "") ?
        '<p class="form-note hint">老师要求中出现 .rar；这里生成的是 ZIP。请核对老师是否接受 ZIP，需要 RAR 时在本机按原要求制作。</p>' : '';
      preparation = '<div class="assignment-preparation"><h3>准备作业文件 · ' + esc(selectedItem.title) +
        '</h3><p>按老师要求选择本地答案文件。ZIP 模式会将所选文件打包；单文件模式保留原文件格式。在线作答或随堂考勤请使用教学网原入口。草稿只保存在本机，准备阶段不会提交。</p>' + rarHint +
        button(state.campus.submission.analysisBusy ? '正在识别要求…' : 'AI 识别作业要求', 'campus-analyze-requirements',
          { small: true, disabled: lite || !!state.campus.submission.analysisBusy }) + analysisView +
        (draft ? '<div role="status"><p>正式提交目标：' + esc(course.title) + ' · ' + esc(draft.assignment_title) +
          '</p><p>截止：' + esc(draft.due_at_label || draft.due_at || '教学网未公布，请核对原要求') +
          '</p><p>本地草稿：' + esc(draft.archive_name) + '</p><ul>' +
          draft.files.map(function (file) { return '<li>' + esc(file.name) + ' · ' + esc(formatTransfer(file.bytes)) + '</li>'; }).join("") +
          '</ul>' + (draft.warnings || []).map(function (warning) { return '<p class="form-note hint">' + esc(warning) + '</p>'; }).join("") +
          '<a href="' + esc(draft.download_url) + '" download data-action="campus-download-draft">下载并检查' + (draft.submission_mode === "single_file" ? '文件' : '压缩包') + '</a>' +
          (state.campus.submission.saveStatus ? '<p role="status" class="assignment-download-status" data-native-save="' + esc(state.campus.submission.saveStatus.state) + '" data-saved-sha256="' + esc(state.campus.submission.saveStatus.sha256 || '') + '">' + esc(state.campus.submission.saveStatus.message) + '</p>' : '') +
          '<label class="assignment-check"><input type="checkbox" id="assignment-reviewed"> 我已对照老师要求核对提交目标、文件名、文件内容和截止时间</label>' +
          button("确认提交到教学网", "campus-submit-assignment", { primary: true, small: true,
            disabled: lite || draft.state === "submitted" }) + (draft.state === "submitted" ? '<p>已发送提交请求，请在教学网查看回执。</p>' : '') +
          (draft.receipt_download_url ? '<a href="' + esc(draft.receipt_download_url) + '" download>下载教学网正式回执</a>' : '') +
          '</div>' :
          '<form id="assignment-prepare-form"><label>提交文件形式<select name="submission_mode"><option value="zip"' + (formValues.submission_mode === "zip" ? ' selected' : '') + '>ZIP 压缩包</option><option value="single_file"' + (formValues.submission_mode === "single_file" ? ' selected' : '') + '>单个原文件（PDF、Word 等）</option></select></label>' +
          (formValues.submission_mode === "zip" ?
            '<label>压缩包文件名（按老师要求）<input name="archive_name" required placeholder="学号姓名第1次作业.zip" value="' + esc(formValues.archive_name) + '"></label>' +
            '<label>补充文件（如代码、图片、已导出的 PDF）<input name="attachments" type="file" multiple></label>' :
            '<label>已导出的答案文件<input name="attachments" type="file"></label>') +
          '<p data-assignment-attachments>' + (formValues.attachments.length ? '已选择：' + esc(formValues.attachments.map(function (file) { return file.name; }).join('、')) : '尚未选择文件') + '</p>' +
          (formValues.submission_mode === "zip" ? '<p class="form-note">请加入老师要求的全部答案文件，再生成压缩包检查。</p>' :
            '<p class="form-note">请选择老师要求格式的答案文件；此处不会转换格式或改变原文件名。</p>') +
          button("生成本地草稿并预检", "campus-build-draft", { primary: true, small: true, disabled: lite }) + '</form>') +
        (state.campus.submission.error ? '<p role="alert">' + esc(state.campus.submission.error) + '</p>' : '') +
        '</div>';
    }
    var assignmentWarning = course.assignments_status === "partial" || course.assignments_status === "error" ||
      course.assignments_status === "not_synced";
    var assignmentState = course.section_states && course.section_states.assignments;
    if (assignmentState) assignmentWarning = ["not_synced", "error", "cached"].indexOf(assignmentState.state) >= 0;
    var receiptsOrMcpFooter = '<details class="submission-receipts"><summary>本机教学网回执记录</summary><p>保存的是教学网当时返回的提交历史；当前提交状态请到教学网核对。</p>' +
      (state.campus.receiptError ? '<p role="alert">' + esc(state.campus.receiptError) + '</p>' : '') +
      '<ul>' + (state.campus.receipts || []).map(function (item) {
        return '<li>' + esc(item.assignment_title) + ' · ' + esc(item.archive_name) + ' · ' + esc(item.verified_at) +
          (item.state === 'receipt_verified' ? ' · 回执已保存在本机 ' +
            button('恢复回执保存', 'campus-recover-submission', { small: true, disabled: lite, attrs: { 'data-draft': item.draft_id } }) :
            (item.download_url ? ' <a href="' + esc(item.download_url) + '" download>下载教学网正式回执</a>' : '')) + '</li>';
      }).join('') + '</ul>' + (state.campus.pendingSubmissions || []).map(function (item) {
        return '<p>' + esc(item.assignment_title) + ' · ' + esc(item.archive_name) + ' · 提交结果待核验。 ' +
          button('核对未完成记录', 'campus-recover-submission', { small: true, disabled: lite, attrs: { 'data-draft': item.draft_id } }) + '</p>';
      }).join('') + button('刷新回执记录', 'campus-load-receipts', { small: true }) + '</details>';
    var assignments = '<section class="panel" id="course-assignments"><h2>作业中心' + proCenterBadge() + '</h2>' + tierNotice +
      (assignmentWarning ? '<p class="form-note hint">' + esc(assignmentState && assignmentState.message || '作业目录尚未完整读取；请到教学网核对正式要求和截止时间。') + '</p>' : '') +
      (assignmentItems ? '<h3>教学网作业与日历事项</h3><ul class="official-list">' + assignmentItems + '</ul>' :
        (assignmentWarning ? '' : '<p>教学网正式作业索引当前为 0 项；课程手册、通知或课堂要求仍可能包含课业。</p>')) +
      (announcementTaskItems ? '<h3>老师公告中的交付要求</h3><ul class="official-list">' + announcementTaskItems + '</ul>' : '') +
      (handbookItems ? '<h3>课程手册中的待核对要求</h3><ul class="official-list">' + handbookItems + '</ul>' : '') +
      preparation + receiptsOrMcpFooter + '</section>';
    return section === "notices" ? notices : section === "assignments" ? assignments : notices + assignments;
  }

  function selectCampusAssignment(contentId) {
    state.campus.submission = { selectedId: contentId, draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } };
    render();
    var panel = document.querySelector(".assignment-preparation");
    if (panel) panel.scrollIntoView({ block: "nearest" });
  }

  function loadAssignmentStatus(contentId) {
    var courseId = state.campus.courseId;
    var statuses = state.campus.assignmentStatuses || (state.campus.assignmentStatuses = {});
    if (statuses[contentId] && statuses[contentId].loading) return;
    statuses[contentId] = { loading: true, label: '正在从教学网读取当前状态…' }; render();
    return requestJson('/api/campus/courses/' + encodeURIComponent(courseId) + '/assignments/' +
      encodeURIComponent(contentId) + '/status').then(function (result) {
      if (state.campus.courseId !== courseId || state.campus.assignmentStatuses !== statuses) return;
      statuses[contentId] = result.ok && result.body ? result.body : {
        state: 'unknown', label: '教学网当前提交状态未确认',
        error: result.body && result.body.detail || '请到教学网核对原页面。本地回执不会代替当前状态。' };
      render();
    }).catch(function () {
      if (state.campus.courseId !== courseId || state.campus.assignmentStatuses !== statuses) return;
      statuses[contentId] = { state: 'unknown', label: '教学网当前提交状态未确认',
        error: '读取失败，请到教学网核对原页面。本地回执不会代替当前状态。' }; render();
    });
  }

  function loadCampusReceipts(courseId) {
    return requestJson('/api/campus/courses/' + encodeURIComponent(courseId) + '/submission-receipts').then(function (result) {
      if (state.campus.courseId !== courseId) return;
      state.campus.receipts = result.ok && result.body ? result.body.items || [] : [];
      state.campus.pendingSubmissions = result.ok && result.body ? result.body.pending || [] : [];
      state.campus.receiptError = result.ok ? '' : '本机回执记录暂时无法读取。';
      render();
    }).catch(function () {
      if (state.campus.courseId !== courseId) return;
      state.campus.receipts = []; state.campus.pendingSubmissions = []; state.campus.receiptError = '本机回执记录暂时无法读取。'; render();
    });
  }

  function recoverCampusSubmission(draftId) {
    var courseId = state.campus.courseId;
    if (!draftId) return;
    return requestJson('/api/campus/submission-drafts/' + encodeURIComponent(draftId) + '/recover', { method: 'POST' }).then(function (result) {
      if (state.campus.courseId !== courseId) return;
      if (!result.ok || !result.body) {
        state.campus.receiptError = result.body && result.body.detail || '原提交记录未确认，禁止重复发送。';
        render(); return;
      }
      state.campus.receiptError = '';
      showToast(result.body.message);
      loadCampusReceipts(courseId);
    }).catch(function () {
      if (state.campus.courseId !== courseId) return;
      state.campus.receiptError = '核对失败，原记录已保留，请到教学网核对，避免重复提交。'; render();
    });
  }

  function prepareCampusAssignment() {
    var form = document.querySelector("#assignment-prepare-form");
    var selected = state.campus.submission.selectedId;
    if (!form || !selected || !form.reportValidity()) return;
    if (state.campus.submission.form.submission_mode === "single_file" &&
        state.campus.submission.form.attachments.length !== 1) {
      state.campus.submission.error = "请选择恰好一个已导出的答案文件。";
      render();
      return;
    }
    var courseId = state.campus.courseId;
    var data = new FormData(form);
    data.delete("attachments");
    if (state.campus.submission.form.submission_mode === "single_file") {
      data.delete("archive_name");
      data.delete("notion_url");
      data.delete("code_filename");
    }
    state.campus.submission.form.attachments.forEach(function (file) { data.append("attachments", file); });
    state.campus.submission.error = "";
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) +
      "/assignments/" + encodeURIComponent(selected) + "/prepare",
      { method: "POST", body: data }).then(function (result) {
        if (state.campus.courseId !== courseId) return;
        if (!result.ok || !result.body) {
          state.campus.submission.error = (result.body && result.body.detail) || "草稿准备失败。";
        } else {
          state.campus.submission.draft = result.body;
        }
        render();
      }).catch(function () {
        state.campus.submission.error = "草稿准备失败，请稍后重试。";
        render();
      });
  }

  async function analyzeCampusAssignmentRequirements() {
    var selected = state.campus.submission.selectedId;
    var courseId = state.campus.courseId;
    if (!selected || state.campus.submission.analysisBusy) return;
    state.campus.submission.analysisBusy = true;
    state.campus.submission.error = '';
    render();
    return requestJson('/api/campus/courses/' + encodeURIComponent(courseId) + '/assignments/' +
      encodeURIComponent(selected) + '/requirements', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true }) }).then(function (result) {
      if (state.campus.courseId !== courseId || state.campus.submission.selectedId !== selected) return;
      state.campus.submission.analysisBusy = false;
      if (!result.ok || !result.body) state.campus.submission.error = result.body && result.body.detail || '要求识别未完成，请核对原任务。';
      else {
        state.campus.submission.analysis = result.body;
        if (result.body.analysis && !state.campus.submission.form.archive_name.trim() &&
            state.campus.submission.form.attachments.length === 0) {
          var formats = (result.body.analysis.file_formats || []).map(function (f) { return String(f.value || '').toLowerCase(); });
          if (formats.some(function (f) { return /zip|rar|7z|压缩包/.test(f); })) {
            state.campus.submission.form.submission_mode = 'zip';
          } else if (formats.some(function (f) { return /pdf|doc|docx|txt|md|py|cpp|ipynb|单文件/.test(f); })) {
            state.campus.submission.form.submission_mode = 'single_file';
          }
        }
      }
      render();
    }).catch(function () {
      if (state.campus.courseId !== courseId || state.campus.submission.selectedId !== selected) return;
      state.campus.submission.analysisBusy = false;
      state.campus.submission.error = '要求识别连接中断，原调用记录已保留；再次打开可恢复同一请求。';
      render();
    });
  }

  async function submitCampusAssignment() {
    var draft = state.campus.submission.draft;
    var checked = document.querySelector("#assignment-reviewed");
    if (!draft || draft.ready === false || !checked || !checked.checked) {
      state.campus.submission.error = "请先核对生成的草稿信息，并勾选确认。";
      render();
      return;
    }
    if (!await confirmAction("确认向教学网正式提交「" + (state.campus.course && state.campus.course.title || '') + ' · ' +
      draft.assignment_title + "」？\n文件：" + draft.archive_name + '\n截止：' +
      (draft.due_at_label || draft.due_at || '教学网未公布，请核对原要求') + "\n提交后可能无法撤回。")) return;
    var courseId = state.campus.courseId;
    var contentId = state.campus.submission.selectedId;
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) +
      "/assignments/" + encodeURIComponent(contentId) + "/submit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_id: draft.draft_id, sha256: draft.sha256, confirmed: true })
      }).then(function (result) {
        if (state.campus.courseId !== courseId) return;
        if (!result.ok || !result.body) {
          state.campus.submission.error = (result.body && result.body.detail) || "提交状态未确认，请到教学网核对。";
        } else {
          draft.state = result.body.state;
          draft.receipt_download_url = result.body.receipt_download_url;
          state.campus.submission.error = "";
          showToast(result.body.message);
          loadCampusReceipts(courseId);
        }
        render();
      }).catch(function () {
        state.campus.submission.error = "提交状态未确认，请到教学网核对历史，避免重复提交。";
        render();
      });
  }

  function campusExistingNotionControl(course) {
    return '<section class="panel campus-existing-notion"><h2>已有课程笔记</h2>' +
      (course.existing_notion_url ? '<p><a href="' + esc(course.existing_notion_url) +
        '" target="_blank" rel="noopener noreferrer">打开之前整理的课程页</a></p>' :
        '<p>如果你之前在 Notion 另建过这门课，可在这里关联；原页面内容不会被改动。</p>') +
      '<label>已有 Notion 课程页链接<input id="campus-existing-notion-url" type="url" value="' +
      esc(course.existing_notion_url || "") + '" placeholder="粘贴 Notion 页面链接"></label>' +
      button("保存关联", "campus-save-existing-notion", { small: true }) + '</section>';
  }

  function saveExistingNotionLink() {
    var input = document.querySelector("#campus-existing-notion-url");
    if (!input || !input.value.trim() || !input.reportValidity()) return;
    var courseId = state.campus.courseId;
    var epoch = accountQuotaEpoch;
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) + "/existing-notion", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: input.value.trim() })
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "关联未保存。");
      if (state.campus.courseId !== courseId) return;
      state.campus.course = result.body;
      render();
      if (state.campus.home && !state.campus.demoConnection) {
        return requestJson("/api/campus/notion/sync", { method: "POST" }).then(function (started) {
          if (epoch !== accountQuotaEpoch) return;
          if (!started.ok || !started.body) {
            return showToast((started.body && started.body.detail) || "关联已保存，但目录更新未确认；请稍后重试。");
          }
          if (started.body.scope === "background") state.recordings.background = started.body;
          else {
            state.recordings.task = started.body;
            state.recordings.taskCourseId = "";
          }
          render();
        }).catch(function () {
          if (epoch === accountQuotaEpoch) showToast("关联已保存，但目录更新未确认；已有课程可继续查看，请稍后重试。");
        });
      }
    }).catch(function () { showToast("关联未保存。"); });
  }

  var readingPreviews = {};
  var mcpHub = { status: null, host: 'codex', error: '', lastFetchedAt: 0, lastJson: '', loading: false };

  function copyStudyText(text, message) {
    function fallback() {
      var field = document.createElement('textarea');
      field.value = text; field.style.position = 'fixed'; field.style.opacity = '0';
      document.body.appendChild(field); field.select();
      var copied = document.execCommand('copy'); field.remove();
      showToast(copied ? message : '复制失败，请选择文本后手动复制。');
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { showToast(message); }).catch(fallback);
    }
    fallback();
  }

  // Escape all source text first; local notes can contain arbitrary HTML.
  function formatMath(tex) {
    var symbols = {
      alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', varepsilon: 'ε',
      zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ', lambda: 'λ',
      mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ', varsigma: 'ς', tau: 'τ',
      phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
      Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π',
      Sigma: 'Σ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
      le: '≤', leq: '≤', ge: '≥', geq: '≥', ne: '≠', neq: '≠', approx: '≈', cong: '≅',
      equiv: '≡', sim: '∼', simeq: '≃', propto: '∝', times: '×', cdot: '·', div: '÷', pm: '±', mp: '∓',
      to: '→', rightarrow: '→', leftarrow: '←', Rightarrow: '⇒', Leftarrow: '⇐',
      implies: '⟹', impliedby: '⟸',
      leftrightarrow: '↔', Leftrightarrow: '⇔', iff: '⇔', maps: '↦', mapsto: '↦',
      'in': '∈', notin: '∉', ni: '∋', subset: '⊂', subseteq: '⊆', supset: '⊃', supseteq: '⊇',
      cup: '∪', cap: '∩', setminus: '∖', emptyset: '∅', varnothing: '∅',
      forall: '∀', exists: '∃', nexists: '∄', neg: '¬', lnot: '¬',
      land: '∧', lor: '∨', oplus: '⊕', bigoplus: '⊕', otimes: '⊗', bigotimes: '⊗', perp: '⊥', parallel: '∥',
      infty: '∞', partial: '∂', nabla: '∇', sum: '∑', prod: '∏', coprod: '∐', int: '∫', oint: '∮',
      dots: '…', cdots: '⋯', ldots: '…', vdots: '⋮', ddots: '⋱',
      mid: '∣', nmid: '∤', lfloor: '⌊', rfloor: '⌋', lceil: '⌈', rceil: '⌉',
      langle: '⟨', rangle: '⟩', triangle: '△', angle: '∠', circ: '∘', degree: '°',
      gcd: 'gcd', lcm: 'lcm', mod: 'mod', bmod: 'mod',
      sin: 'sin', cos: 'cos', tan: 'tan', cot: 'cot', sec: 'sec', csc: 'csc',
      log: 'log', ln: 'ln', exp: 'exp', lim: 'lim', max: 'max', min: 'min',
      sup: 'sup', inf: 'inf', det: 'det', dim: 'dim', ker: 'ker', rank: 'rank'
    };
    var blackboard = {
      R: 'ℝ', Z: 'ℤ', N: 'ℕ', Q: 'ℚ', C: 'ℂ', F: '𝔽', E: '𝔼', P: 'ℙ', H: 'ℍ'
    };
    function formatAtom(raw) {
      var a = String(raw || '').trim();
      a = a.replace(/\\mathbb\{([A-Za-z])\}/g, function (_, ch) { return blackboard[ch] || ch; })
        .replace(/\\mathcal\{([^{}]*)\}/g, '<span class="math-cal">$1</span>')
        .replace(/\\(?:mathbf|boldsymbol)\{([^{}]*)\}/g, '<strong>$1</strong>')
        .replace(/\\(?:mathit|emph)\{([^{}]*)\}/g, '<em>$1</em>')
        .replace(/\\(?:text|textrm|mathrm|operatorname)\{([^{}]*)\}/g, '$1')
        .replace(/\\(?:overline|bar)\{([^{}]*)\}/g, '<span class="math-overline">$1</span>')
        .replace(/\\hat\{([^{}]*)\}/g, '$1\u0302')
        .replace(/\\tilde\{([^{}]*)\}/g, '$1\u0303')
        .replace(/\\vec\{([^{}]*)\}/g, '$1\u20d7')
        .replace(/\\left\s*\\\}/g, '}').replace(/\\left\s*\\\{/g, '{')
        .replace(/\\right\s*\\\}/g, '}').replace(/\\right\s*\\\{/g, '{')
        .replace(/\\(?:left|right)\s*\\\|/g, '‖')
        .replace(/\\(?:left|right)\s*\./g, '')
        .replace(/\\(?:left|right)\s*([()[\]|])/g, '$1')
        .replace(/\\(?:quad|qquad)/g, '\u2003')
        .replace(/\\[,;:]/g, '\u2009')
        .replace(/\\!/g, '')
        .replace(/\\not\s*\\equiv/g, '≢')
        .replace(/\\not\s*=/g, '≠')
        .replace(/\s*(?:\\pmod|\bpmod)\s*\{([^{}]*)\}/g, ' <span class="math-mod">(mod $1)</span>')
        .replace(/\s*(?:\\pmod|\bpmod)\s+([a-zA-Z0-9+\-α-ωΑ-Ω]+)/g, ' <span class="math-mod">(mod $1)</span>')
        .replace(/\s*(?:\\bmod|\bbmod)\s*\{([^{}]*)\}/g, ' <span class="math-mod">mod $1</span>')
        .replace(/\s*(?:\\bmod|\bbmod)\s+([a-zA-Z0-9+\-α-ωΑ-Ω]+)/g, ' <span class="math-mod">mod $1</span>')
        .replace(/\s*\\mod\s*\{([^{}]*)\}/g, ' <span class="math-mod">mod $1</span>');
      for (var pass = 0; pass < 2; pass += 1) {
        a = a.replace(/\^\{([^{}]+)\}/g, '<sup>$1</sup>')
          .replace(/\^([a-zA-Z0-9+\-])/g, '<sup>$1</sup>')
          .replace(/_\{([^{}]+)\}/g, '<sub>$1</sub>')
          .replace(/_([a-zA-Z0-9+\-])/g, '<sub>$1</sub>')
          .replace(/\\sqrt\[([^\]]+)\]\s*\{([^{}]*)\}/g, '<sup>$1</sup>√($2)')
          .replace(/\\sqrt\s*\{([^{}]*)\}/g, '√($1)')
          .replace(/\\binom\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, '<span class="math-binom">(<span class="math-frac"><span class="math-num math-binom-num">$1</span><span class="math-den">$2</span></span>)</span>')
          .replace(/\\(?:d|t)?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, '<span class="math-frac"><span class="math-num">$1</span><span class="math-den">$2</span></span>');
      }
      a = a.replace(/\\([a-zA-Z]+)/g, function (m, name) { return symbols[name] || name; })
        .replace(/\^\{([^{}]+)\}/g, '<sup>$1</sup>')
        .replace(/\^([a-zA-Z0-9+\-])/g, '<sup>$1</sup>')
        .replace(/_\{([^{}]+)\}/g, '<sub>$1</sub>')
        .replace(/_([a-zA-Z0-9+\-])/g, '<sub>$1</sub>')
        .replace(/\\([{}%_&#])/g, '$1');
      return a;
    }
    function renderRows(body) {
      var rawRows = String(body || '').split(/\\\\(?:\s*\[[^\]]*\])?(?:\r?\n)?|\r?\n/);
      var rows = [];
      for (var r = 0; r < rawRows.length; r += 1) {
        var rowText = rawRows[r].replace(/\\+\s*$/, '').trim();
        if (!rowText) continue;
        var cells = rowText.split('&amp;').map(function (c) {
          return '<span class="math-env-cell">' + formatAtom(c) + '</span>';
        }).join('');
        rows.push('<span class="math-env-row">' + cells + '</span>');
      }
      return rows.join('');
    }
    var s = esc(String(tex || '').trim());
    var envs = [];
    s = s.replace(/\\?\bbegin\{(cases|aligned|align\*?|array|matrix|pmatrix|bmatrix|vmatrix)\}(?:\{[^{}]*\})?([\s\S]*?)\\?\bend\{\1\}/g, function (_, env, body) {
      var rowsHtml = renderRows(body);
      var cls = 'math-cases';
      if (env === 'aligned' || env === 'align' || env === 'align*' || env === 'array') cls = 'math-cases math-aligned';
      else if (env === 'pmatrix') cls = 'math-cases math-matrix math-pmatrix';
      else if (env === 'bmatrix') cls = 'math-cases math-matrix math-bmatrix';
      else if (env === 'vmatrix') cls = 'math-cases math-matrix math-vmatrix';
      else if (env === 'matrix') cls = 'math-cases math-matrix';
      envs.push('<span class="' + cls + '"><span class="math-cases-rows">' + rowsHtml + '</span></span>');
      return '\u0000ENV' + (envs.length - 1) + '\u0000';
    });
    if (s.indexOf('\\\\') >= 0) {
      s = '<span class="math-cases math-aligned"><span class="math-cases-rows">' + renderRows(s) + '</span></span>';
    } else {
      s = formatAtom(s);
    }
    return s.replace(/\u0000ENV(\d+)\u0000/g, function (_, idx) { return envs[Number(idx)] || ''; });
  }

  function formatInlineStudyText(value) {
    var codes = [];
    var maths = [];
    var links = [];
    var tags = [];
    var safe = String(value || '').replace(/`([^`]+)`/g, function (_, c) {
      codes.push('<code>' + esc(c) + '</code>');
      return '\u0000CODE' + (codes.length - 1) + '\u0000';
    }).replace(/\$\$([\s\S]+?)\$\$/g, function (_, m) {
      maths.push('<span class="math-inline math-inline-display">' + formatMath(m) + '</span>');
      return '\u0000MATH' + (maths.length - 1) + '\u0000';
    }).replace(/\\\[([\s\S]+?)\\\]/g, function (_, m) {
      maths.push('<span class="math-inline math-inline-display">' + formatMath(m) + '</span>');
      return '\u0000MATH' + (maths.length - 1) + '\u0000';
    }).replace(/\\\(([\s\S]+?)\\\)/g, function (_, m) {
      maths.push('<span class="math-inline">' + formatMath(m) + '</span>');
      return '\u0000MATH' + (maths.length - 1) + '\u0000';
    }).replace(/(^|[^\\])\$([^\s$][^$]*?[^\s$]|[^\s$])\$/g, function (_, pre, m) {
      maths.push('<span class="math-inline">' + formatMath(m) + '</span>');
      return pre + '\u0000MATH' + (maths.length - 1) + '\u0000';
    }).replace(/\\?\bbegin\{(cases|aligned|align\*?|array|matrix|pmatrix|bmatrix|vmatrix)\}[\s\S]*?\\?\bend\{\1\}/g, function (m) {
      maths.push('<span class="math-inline">' + formatMath(m) + '</span>');
      return '\u0000MATH' + (maths.length - 1) + '\u0000';
    }).replace(/<mention-date\s+start="(\d{4}-\d{2}-\d{2})"\s*\/>/g, function (_, d) {
      links.push('<span class="study-date-badge">📅 ' + esc(d) + '</span>');
      return '\u0000LINK' + (links.length - 1) + '\u0000';
    }).replace(/<mention-page\s+url="(https?:\/\/[^\s"<>]+)"\s*\/>/g, function (_, url) {
      links.push('<a class="study-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">关联页面 ↗</a>');
      return '\u0000LINK' + (links.length - 1) + '\u0000';
    }).replace(/\[([^\]]+)\]\((https?:\/\/[^\s"<>)]+)\)/g, function (_, label, url) {
      links.push('<a class="study-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + '</a>');
      return '\u0000LINK' + (links.length - 1) + '\u0000';
    }).replace(/\[(必考|高频考点|高频|易错陷阱|易错|核心公式|选学)\]/g, function (_, badge) {
      var cls = badge === '必考' ? 'is-must'
        : (badge === '高频考点' || badge === '高频') ? 'is-high'
        : (badge.indexOf('易错') >= 0) ? 'is-trap'
        : badge === '核心公式' ? 'is-formula' : 'is-opt';
      tags.push('<span class="exam-tag ' + cls + '">' + esc(badge) + '</span>');
      return '\u0000TAG' + (tags.length - 1) + '\u0000';
    }).replace(/\[课件(?:[：:]\s*([^\]]+?))?\s*[Pp第]\.?\s*(\d+)\s*页?\]/g, function (_, cwName, pageNum) {
      var activeCourseId = state.campus && state.campus.courseId;
      var activeLessonId = state.campus && state.campus.lessonId;
      var activeCourse = state.campus && state.campus.course;
      var activeLesson = activeCourse && Array.isArray(activeCourse.lessons)
        ? activeCourse.lessons.find(function (l) { return l.id === activeLessonId; })
        : null;
      var activePreview = activeCourseId && activeLessonId ? readingPreviews[activeCourseId + ':' + activeLessonId] : null;
      var rawCw = cwName ? cwName.trim() : '';
      var slideFile = '';
      if (rawCw && activeLesson) {
        var matchedMat = (activeLesson.materials || []).find(function (m) {
          var t = String(m.title || '');
          var f0 = (m.files && m.files[0]) ? String(m.files[0]) : '';
          return t.indexOf(rawCw) >= 0 || rawCw.indexOf(t) >= 0 || f0.indexOf(rawCw) >= 0;
        });
        if (matchedMat) slideFile = (matchedMat.files && matchedMat.files[0]) || matchedMat.title || '';
      }
      if (!slideFile && rawCw) slideFile = rawCw;
      if (!slideFile && activeLesson) {
        if (activeLesson.materials && activeLesson.materials.length) {
          var m0 = activeLesson.materials[0];
          slideFile = (m0.files && m0.files[0]) || m0.title || '';
        } else if (activeLesson.local_materials && activeLesson.local_materials.length) {
          slideFile = activeLesson.local_materials[0].filename || '';
        }
      }
      if (!slideFile && activePreview && activePreview.refinedMeta && Array.isArray(activePreview.refinedMeta.courseware) && activePreview.refinedMeta.courseware.length) {
        slideFile = activePreview.refinedMeta.courseware[0].filename || activePreview.refinedMeta.courseware[0].title || '';
      }
      var badgeText = '📄 ' + (rawCw ? rawCw + ' · ' : '课件 ') + 'P.' + pageNum;
      if (activeCourseId && activeLessonId && slideFile) {
        tags.push('<button type="button" class="exam-tag is-courseware is-clickable" data-action="campus-open-slide-ref" data-slide-file="' +
          esc(slideFile) + '" data-slide-page="' + esc(pageNum) + '" title="点击预览原版课件第 ' + esc(pageNum) + ' 页">' +
          esc(badgeText) + '</button>');
      } else {
        tags.push('<span class="exam-tag is-courseware">' + esc(badgeText) + '</span>');
      }
      return '\u0000TAG' + (tags.length - 1) + '\u0000';
    });
    return esc(safe)
      .replace(/==(?!=)([^\n]{1,140}?)==/g, '<mark class="study-key-highlight">$1</mark>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\s][^*]*?[^*\s]|[^*\s])\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/\u0000TAG(\d+)\u0000/g, function (_, idx) { return tags[Number(idx)] || ''; })
      .replace(/\u0000LINK(\d+)\u0000/g, function (_, idx) { return links[Number(idx)] || ''; })
      .replace(/\u0000MATH(\d+)\u0000/g, function (_, idx) { return maths[Number(idx)] || ''; })
      .replace(/\u0000CODE(\d+)\u0000/g, function (_, idx) { return codes[Number(idx)] || ''; });
  }

  var mermaidLoadPromise = null;
  var mermaidSeq = 0;

  function ensureMermaidLoaded() {
    if (window.mermaid && typeof window.mermaid.render === 'function') {
      return Promise.resolve(window.mermaid);
    }
    if (mermaidLoadPromise) return mermaidLoadPromise;
    mermaidLoadPromise = new Promise(function (resolve, reject) {
      function initMmd() {
        try {
          if (window.mermaid && typeof window.mermaid.initialize === 'function') {
            window.mermaid.initialize({
              startOnLoad: false,
              securityLevel: 'strict',
              suppressErrorRendering: true,
              fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
              flowchart: { htmlLabels: true, curve: 'basis', padding: 14, nodeSpacing: 36, rankSpacing: 44, useMaxWidth: true },
              sequence: { useMaxWidth: true, boxMargin: 10, messageMargin: 30 },
              theme: 'base',
              themeVariables: {
                background: '#fcfdfc',
                primaryColor: '#eef6f1',
                primaryTextColor: '#163526',
                primaryBorderColor: '#8ab89f',
                secondaryColor: '#f3f6fc',
                secondaryTextColor: '#1e293b',
                secondaryBorderColor: '#94a3b8',
                tertiaryColor: '#fdf8f2',
                tertiaryTextColor: '#43302b',
                tertiaryBorderColor: '#d6bfa9',
                lineColor: '#3d6b54',
                textColor: '#1e2f26',
                mainBkg: '#eef6f1',
                nodeBorder: '#7eb095',
                clusterBkg: '#f6faf7',
                clusterBorder: '#cde0d4',
                edgeLabelBackground: '#ffffff',
                fontSize: '13.5px'
              }
            });
            resolve(window.mermaid);
          } else {
            reject(new Error('Mermaid API unavailable'));
          }
        } catch (err) {
          reject(err);
        }
      }
      var existing = document.querySelector('script[data-pku-mermaid]');
      if (existing) {
        if (window.mermaid) { initMmd(); return; }
        existing.addEventListener('load', initMmd, { once: true });
        existing.addEventListener('error', function () {
          mermaidLoadPromise = null;
          reject(new Error('Mermaid load failed'));
        }, { once: true });
        return;
      }
      var script = document.createElement('script');
      script.src = '/app/mermaid.min.js';
      script.async = true;
      script.setAttribute('data-pku-mermaid', '1');
      script.addEventListener('load', initMmd, { once: true });
      script.addEventListener('error', function () {
        mermaidLoadPromise = null;
        reject(new Error('Mermaid load failed'));
      }, { once: true });
      document.head.appendChild(script);
    });
    return mermaidLoadPromise;
  }

  function hydrateMermaidDiagrams() {
    var pending = Array.prototype.slice.call(
      document.querySelectorAll('.study-mermaid-diagram:not([data-mermaid-rendered])')
    );
    if (!pending.length) return;
    pending.forEach(function (el) {
      el.setAttribute('data-mermaid-rendered', 'pending');
    });
    ensureMermaidLoaded().then(function (mmd) {
      return Promise.all(pending.map(function (el) {
        if (!document.contains(el)) return Promise.resolve();
        var src = el.getAttribute('data-mermaid-source') || '';
        if (!src) return Promise.resolve();
        var id = 'pku-mmd-' + (++mermaidSeq);
        return Promise.resolve(mmd.render(id, src)).then(function (res) {
          if (!document.contains(el) || !res || !res.svg) return;
          var canvas = el.querySelector('.study-mermaid-canvas');
          if (canvas) {
            canvas.innerHTML = res.svg;
            el.setAttribute('data-mermaid-rendered', '1');
            if (!el.querySelector('.mermaid-zoom-btn')) {
              var zoomBtn = document.createElement('button');
              zoomBtn.type = 'button';
              zoomBtn.className = 'mermaid-zoom-btn';
              zoomBtn.setAttribute('data-action', 'campus-zoom-mermaid');
              zoomBtn.title = '放大查看流程图';
              zoomBtn.textContent = '放大';
              el.appendChild(zoomBtn);
            }
          }
        }).catch(function () {
          el.setAttribute('data-mermaid-rendered', 'fallback');
          var stray = document.getElementById('d' + id);
          if (stray) stray.remove();
        });
      }));
    }).catch(function () {
      pending.forEach(function (el) {
        if (el.getAttribute('data-mermaid-rendered') === 'pending') {
          el.setAttribute('data-mermaid-rendered', 'fallback');
        }
      });
    });
  }

  function wrapMermaidLabel(clean, maxPerLine) {
    var text = String(clean || '').trim();
    var limit = maxPerLine || 20;
    if (text.length <= limit) return text;
    var best = -1;
    var minPos = Math.max(4, Math.floor(text.length * 0.28));
    var maxPos = Math.min(text.length - 3, Math.ceil(text.length * 0.75));
    for (var i = minPos; i <= maxPos; i += 1) {
      var ch = text.charAt(i);
      if ('：:，,（(；;／/ '.indexOf(ch) >= 0) {
        best = (ch === '（' || ch === '(') ? i : i + 1;
        if (best >= limit - 6) break;
      }
    }
    if (best < minPos || best > maxPos) best = Math.min(limit, Math.ceil(text.length / 2));
    var head = text.slice(0, best).trim();
    var tail = text.slice(best).trim();
    if (!head || !tail) return text;
    if (tail.length > limit + 6) {
      var mid = Math.ceil(tail.length / 2);
      var sub = tail.slice(0, mid).trim() + '<br/>' + tail.slice(mid).trim();
      return head + '<br/>' + sub;
    }
    return head + '<br/>' + tail;
  }

  function stripMermaidNodeWrapper(part) {
    var s = String(part || '').trim();
    var m = /^([A-Za-z][A-Za-z0-9_]{0,7})\s*(?:\[\s*"([^"\]]+)"\s*\]|\[\s*([^\]]+)\s*\]|\(\[\s*([^\]]+)\s*\]\)|\{\{\s*([^}]+)\s*\}\}|\(\(\s*([^)]+)\s*\)\)|\{\s*([^}]+)\s*\})\s*$/.exec(s);
    if (m) return (m[2] || m[3] || m[4] || m[5] || m[6] || m[7] || '').trim();
    return s;
  }

  function normalizeToMermaidCode(fenceLang, lines) {
    var cleanLines = (lines || []).map(function (l) { return String(l || '').replace(/\r$/, ''); })
      .filter(function (l) {
        var t = l.trim();
        return Boolean(t) && !/^%%/.test(t);
      });
    if (!cleanLines.length) return '';
    var first = cleanLines[0].trim();
    var hasHeader = /^(?:flowchart|graph|sequenceDiagram|stateDiagram(?:-v2)?|classDiagram|erDiagram|mindmap|pie|gantt|timeline)\b/i.test(first);
    if (hasHeader) {
      var isFlowchart = /^(?:flowchart|graph)\b/i.test(first);
      return cleanLines.map(function (line, idx) {
        var s = line;
        if (idx === 0) s = s.replace(/^(\s*)graph\b/i, '$1flowchart');
        s = s.replace(/\$([^$\n]{1,40})\$/g, '$1');
        if (isFlowchart) {
          s = s
            .replace(/\s*(-->|==>|-\.->)\s+\|([^|\n]+)\|\s*/g, function (_, arr, lbl) {
              return ' ' + arr + '|"' + lbl.trim().replace(/"/g, "'") + '"| ';
            })
            .replace(/(-->|==>|-\.->)\|(?!\s*")([^|\n]+?)\|/g, function (_, arr, lbl) {
              return arr + '|"' + lbl.trim().replace(/"/g, "'") + '"|';
            })
            .replace(/\b([A-Za-z0-9_]+)\[([^"\]\n]*[():;\/,、+\-\s（），：；=≠≤≥→⊕][^"\]\n]*)\]/g, function (_, id, lbl) {
              return id + '["' + lbl.trim().replace(/"/g, "'") + '"]';
            })
            .replace(/\b([A-Za-z0-9_]+)\{([^"\}\n]*[():;\/,、+\-\s（），：；=≠≤≥→⊕][^"\}\n]*)\}/g, function (_, id, lbl) {
              return id + '{"' + lbl.trim().replace(/"/g, "'") + '"}';
            });
        }
        return s;
      }).join('\n');
    }

    var protoRe = /^\s*(?:[①-⑳]|\d+[.)、])?\s*([A-Za-z0-9_\-\u4e00-\u9fa5]{1,14})\s*(?:-->|->>|->|→|➔|⇒)\s*([A-Za-z0-9_\-\u4e00-\u9fa5]{1,14})\s*[：:]\s*(.+)$/;
    var protoRows = [];
    cleanLines.forEach(function (raw) {
      var pm = protoRe.exec(raw.trim());
      if (pm) protoRows.push({ from: pm[1].trim(), to: pm[2].trim(), msg: pm[3].trim() });
    });
    if (cleanLines.length >= 2 && protoRows.length >= 2 && protoRows.length === cleanLines.length) {
      var pMap = {};
      var pOrder = [];
      function ensureActor(name) {
        if (!pMap[name]) {
          var pid = 'P' + pOrder.length;
          pMap[name] = pid;
          pOrder.push({ id: pid, label: name.replace(/[;:#\n]/g, ' ') });
        }
        return pMap[name];
      }
      protoRows.forEach(function (r) { ensureActor(r.from); ensureActor(r.to); });
      if (pOrder.length >= 2 && pOrder.length <= 6) {
        var seqOut = ['sequenceDiagram'];
        pOrder.forEach(function (a) { seqOut.push('  participant ' + a.id + ' as ' + a.label); });
        protoRows.forEach(function (r) {
          var cleanMsg = r.msg
            .replace(/\$([^$\n]{1,40})\$/g, '$1')
            .replace(/;/g, '；')
            .replace(/#/g, '')
            .replace(/\s+/g, ' ')
            .trim();
          cleanMsg = wrapMermaidLabel(cleanMsg, 26);
          seqOut.push('  ' + ensureActor(r.from) + '->>' + ensureActor(r.to) + ': ' + cleanMsg);
        });
        return seqOut.join('\n');
      }
    }

    var parsedRows = cleanLines.map(function (raw) {
      var line = raw.trim().replace(/\s*(?:-->|==>|--)\s*\|([^|]+)\|\s*/g, ' --> --[$1] ');
      var parts = line.split(/\s*(?:-->|==>|->|→|➔|⟹|⇒)\s*/).map(function (s) { return s.trim(); }).filter(Boolean);
      return parts.map(function (part) {
        var edgeMatch = /^--\[([^\]]+)\]\s*(.*)$/.exec(part);
        var edgeLabel = '';
        var nodeText = part;
        if (edgeMatch) {
          edgeLabel = edgeMatch[1].trim().replace(/\$([^$\n]{1,40})\$/g, '$1').replace(/"/g, "'");
          nodeText = edgeMatch[2];
        }
        return { edgeLabel: edgeLabel, nodeText: stripMermaidNodeWrapper(nodeText) };
      });
    });
    var isVerticalStepChain = parsedRows.length >= 2 && parsedRows.every(function (r) { return r.length === 1; });

    var nodeIds = {};
    var nodeSeq = 0;
    var outLines = [(isVerticalStepChain || parsedRows.length >= 3) ? 'flowchart TD' : 'flowchart LR'];
    function ensureNode(label) {
      var clean = String(label || '').trim().replace(/\$([^$\n]{1,40})\$/g, '$1').replace(/"/g, "'");
      if (!clean) return null;
      if (nodeIds[clean]) return { id: nodeIds[clean], def: nodeIds[clean] };
      var nid = 'N' + (nodeSeq++);
      nodeIds[clean] = nid;
      var wrapped = wrapMermaidLabel(clean, isVerticalStepChain ? 26 : 18);
      return { id: nid, def: nid + '["' + wrapped + '"]' };
    }
    var lastChainId = '';
    parsedRows.forEach(function (rowParts) {
      var prevId = '';
      rowParts.forEach(function (part) {
        var nodeInfo = ensureNode(part.nodeText);
        if (!nodeInfo) return;
        if (!prevId) {
          if (isVerticalStepChain && lastChainId) {
            outLines.push('  ' + lastChainId + ' --> ' + nodeInfo.def);
          } else {
            outLines.push('  ' + nodeInfo.def);
          }
          prevId = nodeInfo.id;
        } else {
          outLines.push('  ' + prevId + (part.edgeLabel ? ' -->|"' + part.edgeLabel + '"| ' : ' --> ') + nodeInfo.def);
          prevId = nodeInfo.id;
        }
      });
      if (isVerticalStepChain && prevId) lastChainId = prevId;
    });
    return outLines.length > 1 ? outLines.join('\n') : '';
  }

  function buildCleanFallbackSvg(lines) {
    var cleanLines = (lines || []).map(function (l) { return String(l || '').trim(); })
      .filter(function (l) {
        return Boolean(l) && !/^%%/.test(l) && !/^(?:flowchart|graph|sequenceDiagram|stateDiagram|classDiagram|erDiagram|direction|subgraph|participant)\b/i.test(l) && l !== '{' && l !== '}' && !/^end$/i.test(l);
      });
    var parsedRows = [];
    cleanLines.forEach(function (raw) {
      var s = raw.replace(/\s*(?:-->|==>|-\.->|->>|-->>|--)\s*\|\s*"?([^"|]+?)"?\s*\|\s*/g, ' --> ');
      var cols = s.split(/\s*(?:-->|==>|-\.->|->>|-->>|->|→|➔|⟹|⇒)\s*/).map(function (c) {
        return stripMermaidNodeWrapper(c).replace(/^["']|["']$/g, '').trim();
      }).filter(Boolean);
      if (cols.length) parsedRows.push(cols.slice(0, 5));
    });
    if (!parsedRows.length) return '';
    var rowCount = Math.min(parsedRows.length, 6);
    var maxCols = Math.max.apply(null, parsedRows.slice(0, rowCount).map(function (r) { return r.length; }));
    var isVertical = maxCols === 1 && rowCount > 1;
    var boxW = isVertical ? 280 : 164, boxH = 40, gapX = 36, gapY = isVertical ? 22 : 18, padX = 20, padY = 18;
    var viewW = Math.max(320, padX * 2 + maxCols * boxW + Math.max(0, maxCols - 1) * gapX);
    var viewH = padY * 2 + rowCount * boxH + Math.max(0, rowCount - 1) * gapY;
    var els = ['<rect x="0" y="0" width="' + viewW + '" height="' + viewH + '" rx="10" fill="#fcfdfc" />'];
    var maxChars = isVertical ? 24 : 15;
    for (var r = 0; r < rowCount; r += 1) {
      var cols = parsedRows[r];
      var y = padY + r * (boxH + gapY);
      for (var c = 0; c < cols.length; c += 1) {
        var x = padX + c * (boxW + gapX);
        var lbl = cols[c];
        if (lbl.length > maxChars) lbl = lbl.slice(0, maxChars - 1) + '…';
        els.push('<rect x="' + x + '" y="' + y + '" width="' + boxW + '" height="' + boxH + '" rx="8" fill="#eef6f1" stroke="#8ab89f" stroke-width="1.3" />');
        els.push('<text x="' + (x + boxW / 2) + '" y="' + (y + boxH / 2 + 4) + '" text-anchor="middle" font-size="12" font-weight="600" fill="#163526">' + esc(lbl) + '</text>');
        if (c < cols.length - 1) {
          var ax1 = x + boxW + 2;
          var ax2 = x + boxW + gapX - 4;
          var ay = y + boxH / 2;
          els.push('<line x1="' + ax1 + '" y1="' + ay + '" x2="' + (ax2 - 4) + '" y2="' + ay + '" stroke="#3d6b54" stroke-width="1.5" />');
          els.push('<polygon points="' + ax2 + ',' + ay + ' ' + (ax2 - 6) + ',' + (ay - 4) + ' ' + (ax2 - 6) + ',' + (ay + 4) + '" fill="#3d6b54" />');
        }
      }
      if (isVertical && r < rowCount - 1) {
        var vx = padX + boxW / 2;
        var vy1 = y + boxH + 2;
        var vy2 = y + boxH + gapY - 3;
        els.push('<line x1="' + vx + '" y1="' + vy1 + '" x2="' + vx + '" y2="' + (vy2 - 4) + '" stroke="#3d6b54" stroke-width="1.5" />');
        els.push('<polygon points="' + vx + ',' + vy2 + ' ' + (vx - 4) + ',' + (vy2 - 6) + ' ' + (vx + 4) + ',' + (vy2 - 6) + '" fill="#3d6b54" />');
      }
    }
    return '<svg class="study-mermaid-fallback" viewBox="0 0 ' + viewW + ' ' + viewH + '" role="img" aria-label="知识流程图">' + els.join('') + '</svg>';
  }

  function renderVisualDiagramHtml(lines, fenceLang) {
    var mermaidCode = normalizeToMermaidCode(fenceLang, lines);
    if (!mermaidCode) return '';
    var fallbackSvg = buildCleanFallbackSvg(lines);
    return '<figure class="study-visual-diagram study-mermaid-diagram" data-mermaid-source="' + esc(mermaidCode) + '" aria-label="知识流程图">' +
      '<div class="study-mermaid-canvas">' + fallbackSvg + '</div>' +
      '</figure>';
  }

  function cleanSectionName(raw) {
    return String(raw || '')
      .replace(/^#{1,6}\s*/, '')
      .replace(/\s*\[[0-9:–\-]+\]\s*$/, '')
      .replace(/\*\*|`/g, '')
      .trim();
  }

  function renderProofCardHtml(card) {
    if (!card || (!card.intuition && (!card.formal_steps || !card.formal_steps.length))) return '';
    var steps = Array.isArray(card.formal_steps) ? card.formal_steps : [];
    return '<aside class="note-interspersed-card note-proof-card" aria-label="直觉与形式化推导卡片">' +
      '<div class="note-card-head"><span class="note-card-badge is-proof">💡 直觉 vs 形式化推导</span>' +
      '<strong class="note-card-title">' + formatInlineStudyText(card.title || '核心推导精析') + '</strong></div>' +
      (card.intuition ? '<div class="note-proof-intuition"><span class="note-layer-tag">直觉 / Proof Idea</span>' +
        '<p>' + formatInlineStudyText(card.intuition) + '</p></div>' : '') +
      (steps.length ? '<details class="note-proof-steps" open><summary>📐 形式化推导步进（共 ' + steps.length + ' 步）</summary>' +
        '<ol class="note-proof-step-list">' + steps.map(function (st) {
          return '<li>' + formatInlineStudyText(st) + '</li>';
        }).join('') + '</ol></details>' : '') +
      '</aside>';
  }

  function renderContrastCardHtml(card) {
    if (!card || (!card.positive && !card.counterexample)) return '';
    return '<aside class="note-interspersed-card note-contrast-card" aria-label="正反例与边界条件对比卡">' +
      '<div class="note-card-head"><span class="note-card-badge is-contrast">⚖️ 正例 vs 反例/边界</span>' +
      '<strong class="note-card-title">' + formatInlineStudyText(card.title || '定理前提与边界对比') + '</strong></div>' +
      '<div class="note-contrast-grid">' +
      (card.positive ? '<div class="note-contrast-col is-pos"><span class="note-contrast-label">✅ ' +
        esc(card.positive_title || '成立条件 / 正例') + '</span><p>' + formatInlineStudyText(card.positive) + '</p></div>' : '') +
      (card.counterexample ? '<div class="note-contrast-col is-neg"><span class="note-contrast-label">⚠️ ' +
        esc(card.counter_title || '边界破坏 / 反例') + '</span><p>' + formatInlineStudyText(card.counterexample) + '</p></div>' : '') +
      '</div></aside>';
  }

  function studyMarkdown(markdown, visuals, logicMap) {
    function imageUrl(value) {
      var url = String(value || '').trim();
      if (!url || /[\s\\]/.test(url) || /^\/\//.test(url)) return '';
      try { return /^https?:$/.test(new URL(url, window.location.href).protocol) ? url : ''; }
      catch (_) { return ''; }
    }
    function card(url, caption, note, isBoardFrame, isBlackboard) {
      var source = imageUrl(url);
      if (!source) return '';
      var thumbSrc = source;
      if (source.indexOf('/frames/') >= 0 && /\.(?:jpe?g|png|webp)$/i.test(source.split('?')[0]) && source.indexOf('w=') < 0) {
        thumbSrc = source + (source.indexOf('?') >= 0 ? '&' : '?') + 'w=960';
      }
      var cls = 'note-visual-card' + (isBlackboard ? ' is-board-enhanced' : '');
      var tools = isBoardFrame ? '<span class="note-board-tools">' +
        '<button type="button" class="note-board-btn' + (isBlackboard ? ' is-active' : '') + '" data-board-toggle="enhance" aria-pressed="' + (isBlackboard ? 'true' : 'false') + '" title="增强板书粉笔字迹对比度">板书锐化</button>' +
        '<button type="button" class="note-board-btn" data-board-toggle="invert" aria-pressed="false" title="将深色黑板反色为白底黑字，适合护眼阅读与打印">反色白底</button>' +
        '</span>' : '';
      var noteHtml = note ? '<div class="note-board-caption-note">' + formatInlineStudyText(note) + '</div>' : '';
      return '<figure class="' + cls + '"><img src="' + esc(thumbSrc) + '" data-full-src="' + esc(source) + '" alt="' + esc(caption) + '" width="960" height="540" loading="lazy" decoding="async" data-zoomable-image><figcaption><div class="note-visual-caption-bar"><span>' + esc(caption) + '</span>' + tools + '</div>' + noteHtml + '</figcaption></figure>';
    }
    var usedVisualIndices = {};
    var exactHeadingSet = {};
    String(markdown || '').split('\n').forEach(function (line) {
      var match = /^#{2,3} +(.+?)\s*$/.exec(line);
      if (match) exactHeadingSet[match[1].trim()] = true;
    });
    function stripSectionPrefix(raw) {
      return cleanSectionName(raw)
        .replace(/^(?:[\u2600-\u27BF\uD83C-\uDBFF\uDC00-\uDFFF]+\s*)+/, '')
        .replace(/^(?:第\s*[一二三四五六七八九十百0-9]+\s*[章节讲部分]\s*|[一二三四五六七八九十百]+[、.．\s]\s*|\d+(?:\.\d+)*[、.．\s]\s*)/, '')
        .replace(/[\s：:、，,。·—\-（）()【】\[\]]+/g, '')
        .toLowerCase();
    }
    function sectionVisuals(heading) {
      var allVisuals = Array.isArray(visuals) ? visuals : [];
      var items = allVisuals.filter(function (v, idx) {
        if (String(v.heading || '').trim() !== heading.trim()) return false;
        usedVisualIndices[idx] = true;
        return true;
      });
      if (!items.length) {
        items = allVisuals.filter(function (v, idx) {
          if (usedVisualIndices[idx] || exactHeadingSet[String(v.heading || '').trim()]) return false;
          var name = cleanSectionName(v.heading);
          if (!name || name !== cleanSectionName(heading)) return false;
          usedVisualIndices[idx] = true;
          return true;
        });
      }
      if (!items.length && !/(?:核心考点速览|考前必看|自检清单)/.test(heading)) {
        var normTarget = stripSectionPrefix(heading);
        if (normTarget.length >= 4) {
          allVisuals.forEach(function (v, idx) {
            if (items.length >= 2 || usedVisualIndices[idx] || exactHeadingSet[String(v.heading || '').trim()]) return;
            var normSrc = stripSectionPrefix(v.heading);
            if (normSrc.length >= 4 && (normTarget === normSrc || normTarget.indexOf(normSrc) >= 0 || normSrc.indexOf(normTarget) >= 0)) {
              usedVisualIndices[idx] = true;
              items.push(v);
            }
          });
        }
      }
      if (!items.length) return '';
      return '<div class="note-visual-grid">' + items.map(function (v) {
        return card(v.frame_url, v.frame_caption || '课堂板书/投影 · ' + (v.time || ''), v.board_note, true, Boolean(v.is_blackboard)) +
          (imageUrl(v.slide_url) ? card(v.slide_url, v.slide_caption || '对应课件页', '', false, false) : v.slide_caption ? '<div class="note-slide-reference">📄 ' + esc(v.slide_caption) + '</div>' : '');
      }).join('') + '</div>';
    }
    var usedProofIdx = {};
    var usedContrastIdx = {};
    var proofCards = logicMap && Array.isArray(logicMap.proof_cards) ? logicMap.proof_cards : [];
    var contrastCards = logicMap && Array.isArray(logicMap.contrast_cards) ? logicMap.contrast_cards : [];
    function sectionMatches(targetSec, currentHeading) {
      var a = cleanSectionName(targetSec).replace(/\s+/g, '');
      var b = cleanSectionName(currentHeading).replace(/\s+/g, '');
      if (!a || !b) return false;
      return a === b || a.indexOf(b) >= 0 || b.indexOf(a) >= 0;
    }
    function sectionLogicCards(heading) {
      if (!proofCards.length && !contrastCards.length) return '';
      var chunks = [];
      proofCards.forEach(function (c, idx) {
        if (!usedProofIdx[idx] && sectionMatches(c.section, heading)) {
          usedProofIdx[idx] = true;
          chunks.push(renderProofCardHtml(c));
        }
      });
      contrastCards.forEach(function (c, idx) {
        if (!usedContrastIdx[idx] && sectionMatches(c.section, heading)) {
          usedContrastIdx[idx] = true;
          chunks.push(renderContrastCardHtml(c));
        }
      });
      return chunks.join('');
    }
    var inline = formatInlineStudyText;
    function splitTableCells(row) {
      var masked = [];
      var safeRow = String(row || '').trim().replace(/^\||\|$/g, '')
        .replace(/\\\|/g, function (m) { masked.push(m); return '\u0000P' + (masked.length - 1) + '\u0000'; })
        .replace(/`[^`\n]+`/g, function (m) { masked.push(m); return '\u0000P' + (masked.length - 1) + '\u0000'; })
        .replace(/\$[^$\n]+\$/g, function (m) { masked.push(m); return '\u0000P' + (masked.length - 1) + '\u0000'; });
      return safeRow.split('|').map(function (c) {
        return c.replace(/\u0000P(\d+)\u0000/g, function (_, idx) { return masked[Number(idx)] || ''; }).trim();
      });
    }
    function isTableSeparator(row) {
      return /^\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)+\s*\|?$/.test(String(row || '').trim());
    }
    var rawLines = String(markdown || '').split(/\r?\n/);
    var out = [];
    var fenced = false;
    var fenceLang = '';
    var diagramBuf = [];
    var mathBlock = null;
    var mathEnvEnd = null;
    var justAfterH2Heading = '';
    var openListTag = '';
    function flushList() {
      if (openListTag) {
        out.push('</' + openListTag + '>');
        openListTag = '';
      }
    }
    function isDuplicateSubheading(h2Raw, h3Raw) {
      var norm = function (v) {
        return cleanSectionName(v).replace(/[\s：:、，,。·—\-（）()【】\[\]]+/g, '').toLowerCase();
      };
      var a = norm(h2Raw);
      var b = norm(h3Raw);
      if (!a || !b) return false;
      if (a === b) return true;
      var minLen = Math.min(a.length, b.length);
      var maxLen = Math.max(a.length, b.length);
      return minLen >= 4 && (minLen / maxLen) >= 0.7 && (a.indexOf(b) >= 0 || b.indexOf(a) >= 0);
    }
    for (var i = 0; i < rawLines.length; i += 1) {
      var line = rawLines[i];
      if (!line.trim() && !fenced && mathBlock === null) continue;
      if (!/^#{1,6}\s+/.test(line)) justAfterH2Heading = '';
      var fenceMatch = /^\s*```(.*)$/.exec(line);
      if (fenceMatch) {
        flushList();
        if (fenced) {
          if (fenceLang === 'diagram' || fenceLang === 'mermaid' || fenceLang === 'flowchart') {
            var diagHtml = renderVisualDiagramHtml(diagramBuf, fenceLang);
            if (diagHtml) out.push(diagHtml);
            diagramBuf = [];
          } else {
            out.push('</code></pre></div>');
          }
          fenced = false;
          fenceLang = '';
        } else {
          fenced = true;
          fenceLang = String(fenceMatch[1] || '').trim().toLowerCase().split(/\s+/)[0] || '';
          if (fenceLang === 'diagram' || fenceLang === 'mermaid' || fenceLang === 'flowchart') {
            diagramBuf = [];
          } else {
            out.push('<div class="study-code-wrap"><button type="button" class="study-code-copy" data-action="campus-copy-code">复制代码</button><pre><code>');
          }
        }
        continue;
      }
      if (fenced) {
        if (fenceLang === 'diagram' || fenceLang === 'mermaid' || fenceLang === 'flowchart') {
          diagramBuf.push(line);
        } else {
          out.push(esc(line) + '\n');
        }
        continue;
      }
      if (mathBlock !== null) {
        if (mathEnvEnd) {
          mathBlock.push(line);
          if (mathEnvEnd.test(line)) {
            out.push('<div class="math-block">' + formatMath(mathBlock.join('\n')) + '</div>');
            mathBlock = null;
            mathEnvEnd = null;
          }
          continue;
        }
        var closeMath = /^(.*)\$\$\s*$/.exec(line);
        if (closeMath) {
          if (closeMath[1].trim()) mathBlock.push(closeMath[1]);
          out.push('<div class="math-block">' + formatMath(mathBlock.join('\n')) + '</div>');
          mathBlock = null;
        } else {
          mathBlock.push(line);
        }
        continue;
      }
      var singleMath = /^\s*\$\$(.+)\$\$\s*$/.exec(line);
      if (singleMath) {
        flushList();
        out.push('<div class="math-block">' + formatMath(singleMath[1]) + '</div>');
        continue;
      }
      var openMath = /^\s*\$\$(.*)$/.exec(line);
      if (openMath) {
        flushList();
        mathBlock = [];
        mathEnvEnd = null;
        if (openMath[1].trim()) mathBlock.push(openMath[1]);
        continue;
      }
      var openEnv = /^\s*\\?\bbegin\{(cases|aligned|align\*?|array|matrix|pmatrix|bmatrix|vmatrix)\}/.exec(line);
      if (openEnv) {
        flushList();
        var envEndRe = new RegExp('\\\\?\\bend\\{' + openEnv[1].replace('*', '\\*') + '\\}');
        if (envEndRe.test(line)) {
          out.push('<div class="math-block">' + formatMath(line) + '</div>');
        } else {
          mathBlock = [line];
          mathEnvEnd = envEndRe;
        }
        continue;
      }
      if (line.indexOf('|') >= 0 && i + 1 < rawLines.length && isTableSeparator(rawLines[i + 1])) {
        flushList();
        var headers = splitTableCells(line);
        i += 2;
        var rowsHtml = '';
        while (i < rawLines.length && rawLines[i].trim().indexOf('|') >= 0) {
          var cells = splitTableCells(rawLines[i]);
          rowsHtml += '<tr>' + headers.map(function (_, col) { return '<td>' + inline(cells[col] || '') + '</td>'; }).join('') + '</tr>';
          i += 1;
        }
        i -= 1;
        out.push('<div class="study-table-wrap"><table class="study-table"><thead><tr>' +
          headers.map(function (h) { return '<th>' + inline(h) + '</th>'; }).join('') +
          '</tr></thead><tbody>' + rowsHtml + '</tbody></table></div>');
        continue;
      }
      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flushList();
        out.push('<hr class="study-hr">');
        continue;
      }
      var slideSwitchMatch = /^\s*\*(幻灯片切换[:：]\s*.+?)\*\s*$/.exec(line);
      if (slideSwitchMatch) {
        flushList();
        out.push('<p class="note-slide-switch-meta"><span class="note-slide-switch-badge">🎞️ 课件时间点</span><span>' +
          esc(slideSwitchMatch[1].replace(/^幻灯片切换[:：]\s*/, '')) + '</span></p>');
        continue;
      }
      var heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (heading) {
        flushList();
        var rawDepth = heading[1].length;
        var level = Math.max(rawDepth, 2);
        if (rawDepth === 3 && justAfterH2Heading && isDuplicateSubheading(justAfterH2Heading, heading[2])) {
          justAfterH2Heading = '';
          var dupAttach = sectionVisuals(heading[2]) + sectionLogicCards(heading[2]);
          if (dupAttach) out.push(dupAttach);
          continue;
        }
        justAfterH2Heading = level === 2 ? heading[2] : '';
        var headingRaw = heading[2];
        var headingHtml;
        var hExtraCls = '';
        var timeSuffixMatch = /^(.*?)(\s*\[[0-9:–\-]+\])\s*$/.exec(headingRaw);
        var headingBase = (timeSuffixMatch && timeSuffixMatch[1].trim()) ? timeSuffixMatch[1] : headingRaw;
        var headingTimeHtml = (timeSuffixMatch && timeSuffixMatch[1].trim())
          ? (esc(/^(\s*)/.exec(timeSuffixMatch[2])[1]) + '<span class="note-heading-time">' + esc(timeSuffixMatch[2].trim()) + '</span>')
          : '';
        if (rawDepth === 1 && /精编(?:考点)?讲义/.test(headingRaw)) {
          var docParts = headingRaw.split(/\s*·\s*/).map(function (p) { return p.trim(); }).filter(Boolean);
          var badgePart = docParts.length >= 2 ? docParts[docParts.length - 1] : '精编考点讲义';
          var coursePart = docParts.length >= 3 ? docParts[0] : '';
          var mainPart = docParts.length >= 3
            ? docParts.slice(1, docParts.length - 1).join(' · ')
            : (docParts.length === 2 ? docParts[0] : headingRaw);
          headingHtml = '<span class="doc-title-kicker-row">' +
            (coursePart ? '<span class="doc-title-course">' + inline(coursePart) + '</span>' : '') +
            '<span class="doc-title-badge">✦ ' + inline(badgePart) + '</span></span>' +
            '<span class="doc-title-main">' + inline(mainPart) + '</span>';
        } else if (/^\d{2}:\d{2}\s*[–\-]\s*\d{2}:\d{2}$/.test(headingRaw.trim())) {
          headingHtml = '<span class="note-heading-time is-standalone">' + inline(headingRaw.trim()) + '</span>';
        } else {
          var chapMatch = (level === 2 && rawDepth > 1)
            ? /^([一二三四五六七八九十百]+|\d+)[、.．]\s*(.+)$/.exec(headingBase.trim())
            : null;
          if (chapMatch) {
            headingHtml = '<span class="note-sec-index">' + esc(chapMatch[1]) + '</span>' +
              '<span class="note-sec-title">' + inline(chapMatch[2]) + '</span>' + headingTimeHtml;
          } else {
            headingHtml = (headingTimeHtml ? '<span>' + inline(headingBase) + '</span>' + headingTimeHtml : inline(headingRaw));
          }
        }
        if (rawDepth > 1 && level === 2) {
          if (/(?:核心考点速览|本讲要点速览)/.test(headingRaw)) hExtraCls = ' is-summary-heading';
          else if (/(?:考前必看|易错陷阱|自检清单)/.test(headingRaw)) hExtraCls = ' is-pitfall-heading';
        }
        var secKey = cleanSectionName(headingRaw);
        var clsAttr = rawDepth === 1 ? ' class="note-doc-title"' : (hExtraCls ? ' class="' + hExtraCls.trim() + '"' : '');
        out.push('<h' + level + clsAttr + ' data-note-section="' + esc(secKey) + '">' + headingHtml + '</h' + level + '>' +
          (/^#{2,3}$/.test(heading[1]) ? sectionVisuals(headingRaw) + sectionLogicCards(headingRaw) : ''));
        continue;
      }
      var image = /^\s*!\[([^\]]*)\]\(([^\s]+)\)\s*$/.exec(line);
      if (image && imageUrl(image[2])) {
        flushList();
        out.push(card(image[2], image[1]));
        continue;
      }
      if (/^\s*<details>\s*$/i.test(line)) {
        flushList();
        out.push('<details class="study-details">');
        continue;
      }
      if (/^\s*<\/details>\s*$/i.test(line)) {
        flushList();
        out.push('</details>');
        continue;
      }
      var summaryMatch = /^\s*<summary>(.*)<\/summary>\s*$/i.test(line) ? /^\s*<summary>(.*)<\/summary>\s*$/i.exec(line) : null;
      if (summaryMatch) {
        flushList();
        out.push('<summary>' + inline(summaryMatch[1]) + '</summary>');
        continue;
      }
      if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
        var indentStr = (/^(\s*)/.exec(line) || ['', ''])[1].replace(/\t/g, '    ');
        var indentCols = indentStr.length;
        var sub = indentCols >= 2;
        var sub2 = indentCols >= 6;
        var ordMatch = /^\s*(\d+[.)])\s+(.*)$/.exec(line);
        var wantListTag = ordMatch ? 'ol' : 'ul';
        if (!openListTag) {
          openListTag = wantListTag;
          out.push(openListTag === 'ol' ? '<ol class="study-list is-ordered-list">' : '<ul class="study-list">');
        } else if (!sub && openListTag !== wantListTag) {
          flushList();
          openListTag = wantListTag;
          out.push(openListTag === 'ol' ? '<ol class="study-list is-ordered-list">' : '<ul class="study-list">');
        }
        if (ordMatch) {
          var ordCls = 'is-ordered' + (sub ? ' study-subitem' : '') + (sub2 ? ' study-subitem-2' : '');
          out.push('<li class="' + ordCls + '"><span class="study-list-num">' + esc(ordMatch[1]) + '</span><span class="study-list-body">' + inline(ordMatch[2]) + '</span></li>');
        } else {
          var ulCls = sub ? (' class="study-subitem' + (sub2 ? ' study-subitem-2' : '') + '"') : '';
          out.push('<li' + ulCls + '>' + inline(line.replace(/^\s*[-*+]\s+/, '')) + '</li>');
        }
        continue;
      }
      if (/^>\s?/.test(line)) {
        flushList();
        var rawQuoteLines = [line.replace(/^>\s?/, '').trim()];
        while (i + 1 < rawLines.length && /^>\s?/.test(rawLines[i + 1])) {
          i += 1;
          rawQuoteLines.push(rawLines[i].replace(/^>\s?/, '').trim());
        }
        function isCalloutHeaderLine(ql) {
          return /^(?:\[![a-z]+\]|(?:\*\*\s*)?【[^】]{1,18}】)/i.test(String(ql || '').trim());
        }
        var quoteSegments = [];
        var curSeg = [];
        for (var qk = 0; qk < rawQuoteLines.length; qk += 1) {
          var qlText = rawQuoteLines[qk];
          if (!qlText) continue;
          if (curSeg.length > 0 && isCalloutHeaderLine(qlText)) {
            quoteSegments.push(curSeg);
            curSeg = [qlText];
          } else {
            curSeg.push(qlText);
          }
        }
        if (curSeg.length) quoteSegments.push(curSeg);

        function splitTopLevelLogicSteps(raw) {
          var s = String(raw || '').trim();
          if (!s) return [];
          var steps = [];
          var start = 0;
          var depth = 0;
          var inCode = false;
          var inInlineMath = false;
          var inDisplayMath = false;
          var opens = '（([{【《「';
          var closes = '）)]}】》」';
          var i = 0;
          while (i < s.length) {
            var ch = s[i];
            if (ch === '\\' && i + 1 < s.length) {
              i += 2;
              continue;
            }
            if (ch === '`') {
              if (!inInlineMath && !inDisplayMath) inCode = !inCode;
              i += 1;
              continue;
            }
            if (!inCode && ch === '$') {
              if (s.slice(i, i + 2) === '$$' && !inInlineMath) {
                inDisplayMath = !inDisplayMath;
                i += 2;
                continue;
              }
              if (!inDisplayMath) {
                inInlineMath = !inInlineMath;
                i += 1;
                continue;
              }
            }
            if (!inCode && !inInlineMath && !inDisplayMath) {
              if (opens.indexOf(ch) !== -1) {
                depth += 1;
                i += 1;
                continue;
              }
              if (closes.indexOf(ch) !== -1) {
                if (depth > 0) depth -= 1;
                i += 1;
                continue;
              }
              if (depth === 0) {
                var tail = s.slice(i);
                var arrMatch = /^(?:==>|-->|->|➔|→|⟹|⇒)/.exec(tail);
                if (arrMatch) {
                  var part = s.slice(start, i).trim();
                  if (part) steps.push(part);
                  i += arrMatch[0].length;
                  start = i;
                  continue;
                }
              }
            }
            i += 1;
          }
          var last = s.slice(start).trim();
          if (last) steps.push(last);
          return steps;
        }

        quoteSegments.forEach(function (quoteLines) {
          var joinedQuote = quoteLines.join(' ').trim();
          var logicCallout = /^(?:\[!logic\]|\*\*【?(?:逻辑线|推理线|推导链|逻辑主线)】?\*\*[:：]?)\s*(.+)$/i.exec(joinedQuote);
          if (logicCallout || (quoteLines.length === 1 && /(?:==>|➔)/.test(joinedQuote) && splitTopLevelLogicSteps(joinedQuote).length >= 3)) {
            var chainRaw = logicCallout ? logicCallout[1] : joinedQuote;
            var chainSteps = splitTopLevelLogicSteps(chainRaw);
            if (chainSteps.length >= 2) {
              out.push('<div class="note-inline-logic-chain" aria-label="推导逻辑链"><span class="note-inline-logic-badge">🧭 逻辑链</span><div class="note-inline-logic-steps">' +
                chainSteps.map(function (st, sIdx) {
                  return '<span class="note-inline-logic-item"><span class="note-inline-logic-step">' + inline(st) + '</span>' +
                    (sIdx < chainSteps.length - 1 ? '<span class="note-inline-logic-arrow" aria-hidden="true">➔</span>' : '') + '</span>';
                }).join('') + '</div></div>');
              return;
            }
          }
          var qFirst = quoteLines[0] || '';
          var rawTag = '';
          var calloutSubtitle = '';
          var calloutRest = '';
          var matchedCallout = false;
          var mGfm = /^\[!([a-z]+)\]\s*(.*)$/i.exec(qFirst);
          var mZhExact = !mGfm ? /^\*\*【([^】]{1,18})】\*\*[:：]?\s*(.*)$/.exec(qFirst) : null;
          var mZhWithSub = (!mGfm && !mZhExact) ? /^\*\*【([^】]{1,18})】\s*([^*]{1,100}?)\*\*[:：]?\s*(.*)$/.exec(qFirst) : null;
          var mZhPlain = (!mGfm && !mZhExact && !mZhWithSub) ? /^【([^】]{1,18})】[:：]?\s*(.*)$/.exec(qFirst) : null;
          if (mGfm) {
            matchedCallout = true; rawTag = mGfm[1]; calloutRest = mGfm[2] || '';
          } else if (mZhExact) {
            matchedCallout = true; rawTag = mZhExact[1]; calloutRest = mZhExact[2] || '';
          } else if (mZhWithSub) {
            matchedCallout = true; rawTag = mZhWithSub[1]; calloutSubtitle = (mZhWithSub[2] || '').trim(); calloutRest = mZhWithSub[3] || '';
          } else if (mZhPlain) {
            matchedCallout = true; rawTag = mZhPlain[1]; calloutRest = mZhPlain[2] || '';
          }
          if (matchedCallout) {
            var lowTag = String(rawTag || '').toLowerCase();
            var matchedZh = mGfm ? '' : rawTag;
            var calloutKind = 'theorem';
            var calloutLabel = matchedZh ? ('📘 ' + matchedZh) : '📘 定义与定理';
            if (/(?:exam|考点|必考|考法|得分)/.test(lowTag)) {
              calloutKind = 'exam'; calloutLabel = matchedZh ? ('🎯 ' + matchedZh) : '🎯 核心考点';
            } else if (/(?:intuition|直觉|思想|口诀|巧思|记忆)/.test(lowTag)) {
              calloutKind = 'intuition'; calloutLabel = matchedZh ? ('💡 ' + matchedZh) : '💡 直觉与口诀';
            } else if (/(?:proof|formula|核心公式|公式|证明|推导|步骤|原则|结论)/.test(lowTag)) {
              calloutKind = 'proof'; calloutLabel = matchedZh ? ('📐 ' + matchedZh) : '📐 核心公式与推导';
            } else if (/(?:trap|counter|contrast|易错|反例|边界|陷阱|注意|对比)/.test(lowTag)) {
              calloutKind = 'contrast'; calloutLabel = matchedZh ? ('⚠️ ' + matchedZh) : '⚠️ 易错与边界';
            } else if (/(?:example|例题|案例)/.test(lowTag)) {
              calloutKind = 'example'; calloutLabel = matchedZh ? ('📝 ' + matchedZh) : '📝 典型例题';
            }
            var bodyLines = [];
            var firstBody = (calloutSubtitle ? '**' + calloutSubtitle + '** ' : '') + calloutRest;
            if (firstBody.trim()) bodyLines.push(firstBody.trim());
            for (var qi = 1; qi < quoteLines.length; qi += 1) bodyLines.push(quoteLines[qi]);
            var bodyHtml = bodyLines.map(function (bl) {
              var pureMath = /^\s*\$\$([^$]+)\$\$\s*$/.exec(bl);
              if (pureMath) return '<div class="math-block is-in-callout">' + formatMath(pureMath[1]) + '</div>';
              if (/^[-*+]\s+/.test(bl)) return '<p class="note-callout-bullet">• ' + inline(bl.replace(/^[-*+]\s+/, '')) + '</p>';
              return '<p>' + inline(bl) + '</p>';
            }).join('');
            var compactCls = bodyLines.length <= 1 ? ' is-compact' : '';
            out.push('<aside class="note-inline-callout is-' + calloutKind + compactCls + '"><span class="note-inline-callout-tag">' +
              esc(calloutLabel) + '</span><div class="note-inline-callout-body">' + bodyHtml + '</div></aside>');
            return;
          }
          out.push('<blockquote>' + quoteLines.map(function (ql) { return '<p>' + inline(ql) + '</p>'; }).join('') + '</blockquote>');
        });
        continue;
      }
      if (line.trim()) {
        flushList();
        var isLeadLabel = /^\s*\*\*[^*]+[:：]\*\*\s*$/.test(line) || /^\s*\*\*[^*]+\*\*\s*[:：]\s*$/.test(line);
        out.push('<p' + (isLeadLabel ? ' class="study-lead-label"' : '') + '>' + inline(line) + '</p>');
      }
    }
    flushList();
    if (mathBlock !== null) out.push('<div class="math-block">' + formatMath(mathBlock.join('\n')) + '</div>');
    if (fenced && (fenceLang === 'diagram' || fenceLang === 'mermaid' || fenceLang === 'flowchart') && diagramBuf.length) {
      var tailDiag = renderVisualDiagramHtml(diagramBuf, fenceLang);
      if (tailDiag) out.push(tailDiag);
      fenced = false;
    }
    proofCards.forEach(function (c, idx) {
      if (!usedProofIdx[idx]) out.push(renderProofCardHtml(c));
    });
    contrastCards.forEach(function (c, idx) {
      if (!usedContrastIdx[idx]) out.push(renderContrastCardHtml(c));
    });
    return out.join('') + (fenced ? '</code></pre></div>' : '');
  }

  function highlightStudyText(html, query, hitIndex) {
    if (!query) return { html: html, count: 0 };
    var template = document.createElement('template');
    template.innerHTML = html;
    var walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    var pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    var count = 0;
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      var fragment = document.createDocumentFragment();
      var offset = 0;
      var match;
      pattern.lastIndex = 0;
      while ((match = pattern.exec(text))) {
        fragment.appendChild(document.createTextNode(text.slice(offset, match.index)));
        var mark = document.createElement('mark');
        mark.className = 'study-search-hit';
        mark.textContent = match[0];
        fragment.appendChild(mark);
        offset = match.index + match[0].length;
        count += 1;
      }
      if (offset) {
        fragment.appendChild(document.createTextNode(text.slice(offset)));
        node.replaceWith(fragment);
      }
    });
    if (count > 0) {
      var activeMark = template.content.querySelectorAll('mark.study-search-hit')[(((hitIndex || 0) % count) + count) % count];
      activeMark.classList.add('study-search-hit-active');
      activeMark.dataset.activeSearchHit = 'true';
    }
    return { html: template.innerHTML, count: count };
  }

  function lectureLogicMapBox(key, item) {
    if (!item || !item.lecture || !item.markdown || item.loading || item.error) return '';
    var map = item.logicMap;
    var chain = map && Array.isArray(map.logic_chain) ? map.logic_chain : [];
    var mindmap = map && Array.isArray(map.mindmap) ? map.mindmap : [];
    var proofCount = (map && Array.isArray(map.proof_cards) ? map.proof_cards.length : 0) +
      (map && Array.isArray(map.contrast_cards) ? map.contrast_cards.length : 0);
    var hasMap = chain.length > 0 || mindmap.length > 0;
    var activeTab = item.logicTab || 'all';
    var bodyHtml = '';
    if (item.logicMapLoading) {
      bodyHtml = '<p class="lecture-logic-status" role="status">正在基于本讲笔记提炼推导逻辑链与思维导图…</p>';
    } else if (hasMap) {
      var thesisHtml = map.core_thesis
        ? '<div class="lecture-logic-thesis"><span class="lecture-logic-thesis-tag">核心主线</span><p>' + formatInlineStudyText(map.core_thesis) + '</p></div>'
        : '';
      var tabsHtml = '<div class="lecture-logic-tabs" role="tablist">' +
        [{ id: 'all', label: '全景视图' }, { id: 'chain', label: '🔗 推导逻辑链 (' + chain.length + ')' }, { id: 'mindmap', label: '🌿 章节思维导图 (' + mindmap.length + ')' }].map(function (t) {
          return '<button type="button" class="lecture-logic-tab' + (activeTab === t.id ? ' is-active' : '') + '" data-action="campus-switch-logic-tab" data-preview="' + esc(key) + '" data-tab="' + t.id + '">' + esc(t.label) + '</button>';
        }).join('') +
        (proofCount > 0 ? '<span class="lecture-logic-interspersed-hint">✨ 已在下方章节穿插 ' + proofCount + ' 张推导/正反例卡</span>' : '') +
        '</div>';
      var chainHtml = (activeTab === 'all' || activeTab === 'chain') && chain.length
        ? '<div class="logic-chain-track" aria-label="本讲推导逻辑链">' + chain.map(function (step, idx) {
          var sec = cleanSectionName(step.section || step.title || '');
          return '<div class="logic-chain-step">' +
            '<div class="logic-step-top"><span class="logic-step-stage">' + esc((idx + 1) + '. ' + (step.stage || '推导节点')) + '</span>' +
            (sec ? '<button type="button" class="logic-step-jump" data-action="campus-jump-note-section" data-preview="' + esc(key) + '" data-section="' + esc(sec) + '" title="定位到章节：' + esc(sec) + '">📍 定位</button>' : '') +
            '</div>' +
            '<strong class="logic-step-title">' + formatInlineStudyText(step.title || '') + '</strong>' +
            (step.detail ? '<p class="logic-step-detail">' + formatInlineStudyText(step.detail) + '</p>' : '') +
            '</div>' +
            (idx < chain.length - 1 ? '<span class="logic-chain-arrow" aria-hidden="true">➔</span>' : '');
        }).join('') + '</div>'
        : '';
      var mindmapHtml = (activeTab === 'all' || activeTab === 'mindmap') && mindmap.length
        ? '<div class="mindmap-tree" aria-label="本讲交互式思维导图">' + mindmap.map(function (branch) {
          var sec = cleanSectionName(branch.section || branch.topic || '');
          var children = Array.isArray(branch.children) ? branch.children : [];
          return '<details class="mindmap-branch" open>' +
            '<summary class="mindmap-branch-head">' +
            '<span class="mindmap-branch-topic">' + formatInlineStudyText(branch.topic || '') + '</span>' +
            (branch.summary ? '<small class="mindmap-branch-summary">' + formatInlineStudyText(branch.summary) + '</small>' : '') +
            (sec ? '<button type="button" class="logic-step-jump" data-action="campus-jump-note-section" data-preview="' + esc(key) + '" data-section="' + esc(sec) + '">📍 跳至章节</button>' : '') +
            '</summary>' +
            (children.length ? '<ul class="mindmap-children">' + children.map(function (ch) {
              return '<li class="mindmap-leaf"><span class="mindmap-leaf-label">' + formatInlineStudyText(ch.label || '') + '</span>' +
                (ch.detail ? '<span class="mindmap-leaf-detail">' + formatInlineStudyText(ch.detail) + '</span>' : '') + '</li>';
            }).join('') + '</ul>' : '') +
            '</details>';
        }).join('') + '</div>'
        : '';
      bodyHtml = thesisHtml + tabsHtml + chainHtml + mindmapHtml;
    }
    var isCompact = !hasMap && !item.logicMapLoading && !item.logicMapError;
    return '<section class="lecture-logic-map-card' + (isCompact ? ' is-compact' : '') + '" aria-label="本讲逻辑主线与思维导图">' +
      '<div class="lecture-quiz-head">' +
      '<div><span class="lecture-quiz-title">🧭 <strong>逻辑主线与思维导图</strong></span>' +
      (hasMap ? '<span class="lecture-quiz-badge">点击节点定位正文</span>' : '<span class="lecture-logic-inline-hint">提炼推导链、章节导图与正反例卡片</span>') + '</div>' +
      button(item.logicMapLoading ? '正在提炼…' : hasMap ? '重新提炼' : '✨ 提炼逻辑线与导图',
        'campus-generate-lecture-logic-map', {
          small: true,
          quiet: true,
          disabled: Boolean(item.logicMapLoading),
          attrs: { 'data-preview': key, 'data-force': hasMap ? 'true' : 'false' }
        }) +
      '</div>' +
      (item.logicMapError ? '<p class="lecture-quiz-error" role="alert">' + esc(item.logicMapError) + '</p>' : '') +
      bodyHtml +
      '</section>';
  }

  function generateLectureLogicMap(key, force) {
    var item = readingPreviews[key];
    if (!item || item.logicMapLoading) return;
    var parts = String(key || '').split(':');
    var courseId = parts[0];
    var recording = parts.slice(1).join(':');
    if (!courseId || !recording || recording === 'summary') return;
    item.logicMapLoading = true;
    item.logicMapError = '';
    render();
    var url = '/api/campus/courses/' + encodeURIComponent(courseId) + '/recordings/' +
      encodeURIComponent(recording) + '/lecture-logic-map' + (force ? '?force=true' : '');
    return requestJson(url, { method: 'POST' }).then(function (result) {
      item.logicMapLoading = false;
      if (!result.ok || !result.body) {
        item.logicMapError = (result.body && result.body.detail) || '逻辑主线与思维导图提炼未完成，请检查模型配置后重试。';
        showToast(item.logicMapError);
      } else {
        item.logicMap = result.body;
        item.logicTab = 'all';
      }
      render();
    }).catch(function () {
      item.logicMapLoading = false;
      item.logicMapError = '逻辑主线与思维导图提炼失败，请稍后重试。';
      showToast(item.logicMapError);
      render();
    });
  }

  function lectureSelfTestBox(key, item) {
    if (!item || !item.lecture || !item.markdown || item.loading || item.error) return '';
    var quiz = item.quiz;
    var questions = quiz && Array.isArray(quiz.questions) ? quiz.questions : [];
    var picked = item.quizPicked || {};
    var bodyHtml = '';
    if (item.quizLoading) {
      bodyHtml = '<p class="lecture-quiz-status" role="status">正在基于本讲课堂笔记生成 3 道随堂自测选择题…</p>';
    } else if (questions.length > 0) {
      bodyHtml = '<div class="lecture-quiz-list">' + questions.map(function (q, qIdx) {
        var opts = Array.isArray(q.options) ? q.options : [];
        var chosenIdx = typeof picked[qIdx] === 'number' ? picked[qIdx] : -1;
        var chosenOpt = chosenIdx >= 0 ? opts[chosenIdx] : null;
        return '<div class="lecture-quiz-item" data-quiz-q="' + qIdx + '">' +
          '<p class="lecture-quiz-q"><strong>' + (qIdx + 1) + '.</strong> ' + formatInlineStudyText(q.question || '') + '</p>' +
          '<div class="lecture-quiz-options">' + opts.map(function (opt, oIdx) {
            var stateCls = '';
            if (chosenIdx === oIdx) stateCls = opt.correct ? ' is-correct' : ' is-wrong';
            return '<button type="button" class="lecture-quiz-opt' + stateCls + '" data-action="campus-select-quiz-opt" data-preview="' + esc(key) + '" data-q="' + qIdx + '" data-opt="' + oIdx + '">' +
              '<strong>' + esc(opt.label || String.fromCharCode(65 + oIdx)) + '.</strong> ' + formatInlineStudyText(opt.text || '') + '</button>';
          }).join('') + '</div>' +
          (chosenOpt ? '<div class="lecture-quiz-feedback ' + (chosenOpt.correct ? 'ok' : 'err') + '">' +
            (chosenOpt.correct ? '✅ ' : '💡 ') + formatInlineStudyText(chosenOpt.explanation || '') + '</div>' : '') +
          '</div>';
      }).join('') + '</div>';
    }
    var isCompact = !questions.length && !item.quizLoading && !item.quizError;
    return '<section class="lecture-self-test-card' + (isCompact ? ' is-compact' : '') + '" aria-label="本讲随堂自测">' +
      '<div class="lecture-quiz-head">' +
      '<div><span class="lecture-quiz-title">🧠 <strong>本讲随堂自测</strong></span>' +
      (questions.length ? '<span class="lecture-quiz-badge">点击选项查看解析</span>' : '<span class="lecture-logic-inline-hint">基于本讲考点与易错点生成 3 道单选题</span>') + '</div>' +
      button(item.quizLoading ? '正在出题…' : questions.length ? '换一组自测题' : '✨ 生成随堂自测',
        'campus-generate-lecture-quiz', {
          small: true,
          quiet: true,
          disabled: Boolean(item.quizLoading),
          attrs: { 'data-preview': key, 'data-force': questions.length ? 'true' : 'false' }
        }) +
      '</div>' +
      (item.quizError ? '<p class="lecture-quiz-error" role="alert">' + esc(item.quizError) + '</p>' : '') +
      bodyHtml +
      '</section>';
  }

  function generateLectureSelfTest(key, force) {
    var item = readingPreviews[key];
    if (!item || item.quizLoading) return;
    var parts = String(key || '').split(':');
    var courseId = parts[0];
    var recording = parts.slice(1).join(':');
    if (!courseId || !recording || recording === 'summary') return;
    item.quizLoading = true;
    item.quizError = '';
    render();
    var url = '/api/campus/courses/' + encodeURIComponent(courseId) + '/recordings/' +
      encodeURIComponent(recording) + '/lecture-quiz' + (force ? '?force=true' : '');
    return requestJson(url, { method: 'POST' }).then(function (result) {
      item.quizLoading = false;
      if (!result.ok || !result.body) {
        item.quizError = (result.body && result.body.detail) || '随堂自测生成未完成，请检查模型配置后重试。';
        showToast(item.quizError);
      } else {
        item.quiz = result.body;
        item.quizPicked = {};
      }
      render();
    }).catch(function () {
      item.quizLoading = false;
      item.quizError = '随堂自测生成失败，请稍后重试。';
      showToast(item.quizError);
      render();
    });
  }

  function buildRefinedOutlineBar(key, markdown) {
    var sections = [];
    var chapNum = 0;
    String(markdown || '').split(/\r?\n/).forEach(function (line) {
      var m = /^##\s+(.+?)\s*$/.exec(line.trim());
      if (!m) return;
      var sec = cleanSectionName(m[1]);
      if (!sec) return;
      var cleanTitle = sec
        .replace(/^(?:[\u2600-\u27BF\uD83C-\uDBFF\uDC00-\uDFFF]+\s*)+/, '')
        .replace(/^(?:第\s*[一二三四五六七八九十百0-9]+\s*[章节讲部分]\s*|[一二三四五六七八九十百]+[、.．]\s*|\d+(?:\.\d+)*[、.．]\s*)/, '')
        .trim() || sec;
      var kind = 'chap';
      var idxLabel = '';
      if (/(?:核心考点速览|本讲要点速览|导读)/.test(sec)) {
        kind = 'lead';
        idxLabel = '导读';
      } else if (/(?:考前必看|易错陷阱|自检清单)/.test(sec)) {
        kind = 'tail';
        idxLabel = '自检';
      } else {
        chapNum += 1;
        idxLabel = chapNum < 10 ? ('0' + chapNum) : String(chapNum);
      }
      sections.push({ raw: sec, kind: kind, idxLabel: idxLabel, cleanTitle: cleanTitle });
    });
    if (!sections.length) return '';
    var rows = Math.max(1, Math.ceil(sections.length / 2));
    return '<details class="refined-toc-card" data-persistent-disclosure="refined-toc-' + esc(key) + '" open>' +
      '<summary class="refined-toc-head">' +
        '<span class="refined-toc-title-wrap">' +
          '<svg class="refined-toc-icon" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M2.5 4h11M2.5 8h11M2.5 12h7.5"/></svg>' +
          '<span class="refined-outline-label">知识点目录</span>' +
          '<span class="refined-toc-count">共 ' + sections.length + ' 节</span>' +
        '</span>' +
        '<span class="refined-toc-toggle" aria-hidden="true"></span>' +
      '</summary>' +
      '<nav class="refined-outline-bar' + (sections.length >= 2 ? ' is-two-col' : '') + '" style="--toc-rows:' + rows + '" aria-label="知识点目录">' +
      sections.map(function (s) {
        var idxCls = 'toc-idx' + (s.kind !== 'chap' ? (' is-' + s.kind) : '');
        return '<button type="button" class="refined-outline-pill" data-action="campus-jump-note-section" data-preview="' +
          esc(key) + '" data-section="' + esc(s.raw) + '" title="' + esc(s.cleanTitle) + '">' +
          '<span class="' + idxCls + '">' + esc(s.idxLabel) + '</span>' +
          '<span class="toc-text">' + esc(s.cleanTitle) + '</span>' +
          '</button>';
      }).join('') +
      '</nav></details>';
  }

  async function generateRefinedLectureNote(key, force, skipConfirm) {
    var item = readingPreviews[key];
    if (!item || item.refining) return;
    var parts = String(key || '').split(':');
    var courseId = parts[0];
    var recording = parts.slice(1).join(':');
    if (!courseId || !recording || recording === 'summary') return;
    if (item.refinedAvailable && !force) {
      item.noteMode = 'refined';
      render();
      showToast('本讲已完成精细整理，已为您切换阅读。');
      return;
    }
    item.refining = true;
    item.refineError = '';
    render();
    var url = '/api/campus/courses/' + encodeURIComponent(courseId) + '/recordings/' +
      encodeURIComponent(recording) + '/refine-note' + (force ? '?force=true' : '');
    return requestJson(url, { method: 'POST' }).then(function (result) {
      item.refining = false;
      if (!result.ok || !result.body) {
        item.refineError = (result.body && result.body.detail) || '精细整理讲义暂未完成，请检查模型配置后重试。';
        showToast(item.refineError);
      } else {
        item.refinedAvailable = Boolean(result.body.refined_available || result.body.refined_markdown);
        item.refinedMarkdown = result.body.refined_markdown || '';
        item.refinedMeta = result.body.refined_meta || null;
        item.refineEstimate = result.body.refine_estimate || item.refineEstimate;
        if (item.refinedAvailable) item.noteMode = 'refined';
        var toastMsg = result.body.reused
          ? '已切换至精细整理讲义'
          : '✨ 精细整理讲义已就绪';
        showToast(toastMsg);
      }
      render();
    }).catch(function () {
      item.refining = false;
      item.refineError = '精细整理请求失败，请检查网络或模型配置后重试。';
      showToast(item.refineError);
      render();
    });
  }

  function previewReaderSignature(key, item, lecture) {
    if (!item || !item.open) return '';
    var recId = String(key || '').split(':').slice(1).join(':');
    var course = state.campus && state.campus.course;
    var lesson = lecture && course && Array.isArray(course.lessons)
      ? course.lessons.find(function (l) { return l.id === recId; })
      : null;
    var isRefinedActive = Boolean(lecture && item.noteMode === 'refined' && item.refinedAvailable && item.refinedMarkdown);
    return JSON.stringify([
      Boolean(lecture), Boolean(item.loading), String(item.error || ''),
      isRefinedActive ? Boolean(item.markdown) : String(item.markdown || ''), String(item.refinedMarkdown || ''),
      Boolean(item.refinedAvailable), String(item.noteMode || 'raw'),
      Boolean(item.refining), String(item.refineError || ''),
      item.refineEstimate || null,
      item.refinedMeta || null,
      lesson ? ((lesson.materials || []).length + ':' + (lesson.local_materials || []).length) : '',
      item.visuals || [],
      Boolean(item.framesExtracting),
      isRefinedActive ? false : Boolean(item.streaming),
      isRefinedActive ? 0 : (Number(item.streamDoneSections) || 0),
      isRefinedActive ? false : Boolean(item.visualsPending),
      String(item.query || '').trim(), Number(item.hitIndex) || 0,
      item.logicMap || null, String(item.logicTab || 'all'),
      Boolean(item.logicMapLoading), String(item.logicMapError || ''),
      item.quiz || null, item.quizPicked || {},
      Boolean(item.quizLoading), String(item.quizError || '')
    ]);
  }

  function previewMarkdownSignature(item) {
    if (!item || !item.open || item.loading || item.error) return '';
    var course = state.campus && state.campus.course;
    var activeLesson = course && Array.isArray(course.lessons)
      ? course.lessons.find(function (l) { return l.id === state.campus.lessonId; })
      : null;
    var isRefinedActive = Boolean(item.lecture && item.noteMode === 'refined' && item.refinedAvailable && item.refinedMarkdown);
    return JSON.stringify([
      isRefinedActive ? Boolean(item.markdown) : String(item.markdown || ''), String(item.refinedMarkdown || ''),
      String(item.noteMode || 'raw'),
      item.refinedMeta || null,
      item.visuals || [],
      String(item.query || '').trim(), Number(item.hitIndex) || 0,
      isRefinedActive ? null : (item.logicMap || null)
    ]);
  }

  function lectureOralHighlightsBox(key, item) {
    var highlights = Array.isArray(item.oralHighlights) ? item.oralHighlights : [];
    if (!highlights.length) return '';
    var courseTitle = (state.campus.course && state.campus.course.title) || '本课程';
    var recId = String(key || '').split(':').slice(1).join(':');
    var lesson = ((state.campus.course && state.campus.course.lessons) || []).find(function (l) { return l.id === recId; });
    var lessonTitle = (lesson && lesson.title) || '本讲';
    var askPrompt = '请通过 pku-sync MCP 读取《' + courseTitle + '》「' + lessonTitle + '」的笔记与重点。';
    return '<aside class="lecture-oral-highlights-box" data-lecture-oral-highlights>' +
      '<div class="lecture-oral-highlights-head">' +
      '<strong>📢 本讲重点与老师口述提示</strong>' +
      button('复制 AI 追问 Prompt', 'copy-assignment-mcp-prompt', {small:true, quiet:true, attrs:{'data-prompt':askPrompt}}) +
      '</div>' +
      '<ul>' +
      highlights.map(function (h) { return '<li>' + formatInlineStudyText(h) + '</li>'; }).join('') +
      '</ul></aside>';
  }

  function renderPreviewStudyContent(key, item, lecture) {
    var query = (item.query || '').trim();
    var hasRefined = Boolean(lecture && item.refinedAvailable && item.refinedMarkdown);
    var currentMode = (lecture && item.noteMode === 'refined') ? 'refined' : 'raw';
    var isRefinedView = Boolean(lecture && currentMode === 'refined' && hasRefined);
    var isRefinedPendingView = Boolean(lecture && currentMode === 'refined' && !hasRefined);
    var activeMd = isRefinedView ? item.refinedMarkdown : (item.markdown || '本讲尚无笔记，整理完成后即可阅读。');
    var activeVisuals = (isRefinedView && item.refinedMarkdown.indexOf('![') >= 0) ? [] : item.visuals;
    var content = isRefinedPendingView
      ? { html: '', count: 0 }
      : highlightStudyText(studyMarkdown(activeMd, activeVisuals, null), query, item.hitIndex);
    var outlineBar = isRefinedView ? buildRefinedOutlineBar(key, activeMd) : '';
    var bodyHtml = content.html;
    if (isRefinedView && outlineBar) {
      var docTitleClose = /(<h2\b[^>]*\bnote-doc-title\b[^>]*>[\s\S]*?<\/h2>)/.exec(bodyHtml);
      if (docTitleClose) {
        bodyHtml = bodyHtml.replace(docTitleClose[1], docTitleClose[1] + outlineBar);
      } else {
        bodyHtml = outlineBar + bodyHtml;
      }
    }
    return {
      query: query,
      hasRefined: hasRefined,
      currentMode: currentMode,
      isRefinedView: isRefinedView,
      isRefinedPendingView: isRefinedPendingView,
      content: content,
      bodyHtml: bodyHtml
    };
  }

  function previewReader(key, lecture) {
    var item = readingPreviews[key];
    if (!item || !item.open) return '';
    var view = renderPreviewStudyContent(key, item, lecture);
    var query = view.query;
    var hasRefined = view.hasRefined;
    var currentMode = view.currentMode;
    var isRefinedView = view.isRefinedView;
    var isRefinedPendingView = view.isRefinedPendingView;
    var content = view.content;
    var est = item.refineEstimate || {};
    var estChars = Number(est.raw_chars || (item.markdown ? item.markdown.length : 0));
    var lenLabel = estChars >= 10000 ? ('约 ' + (estChars / 10000).toFixed(1) + ' 万字')
      : (estChars >= 1000 ? ('约 ' + (Math.round(estChars / 100) / 10) + 'k 字') : ('约 ' + estChars + ' 字'));
    var refineBar = '';
    if (lecture && item.markdown && !item.loading && !item.error) {
      var refinedSub = item.refining ? '正在生成…' : (hasRefined ? '去闲谈 · 考点讲义' : '去闲谈提纯讲义');
      var actionRight = '';
      if (hasRefined) {
        actionRight = '<div class="reader-refine-actions">' +
          '<span class="refined-badge-note">' + (isRefinedView ? '✓ 已剔除闲谈 · 提炼考点与板书' : '当前显示按时间记录的课堂原话') + '</span>' +
          '</div>';
      } else if (!isRefinedPendingView) {
        actionRight = '<div class="reader-refine-actions">' +
          button(item.refining ? '正在精细整理…' : '✨ 一键精细整理', 'campus-refine-note', {
            small: true, primary: true, disabled: Boolean(item.refining),
            attrs: { 'data-preview': key, 'data-force': 'false' }
          }) +
          '</div>';
      } else {
        actionRight = '<div class="reader-refine-actions"><span class="refined-badge-note">' + esc(lenLabel) + '</span></div>';
      }
      var showUnrefinedUpsell = !hasRefined && !isRefinedPendingView;
      refineBar = '<div class="reader-refine-bar' + (showUnrefinedUpsell ? ' is-unrefined' : '') + '" role="region" aria-label="笔记阅读模式切换">' +
        '<div class="note-mode-switch" role="tablist" aria-label="笔记模式">' +
        '<button type="button" role="tab" aria-selected="' + (currentMode === 'raw' ? 'true' : 'false') + '" class="note-mode-pill' + (currentMode === 'raw' ? ' is-active' : '') + '" data-action="campus-switch-note-mode" data-preview="' + esc(key) + '" data-mode="raw"><span class="note-mode-title">📝 课堂流水账</span><small class="note-mode-sub">按时间记录原话</small></button>' +
        '<button type="button" role="tab" aria-selected="' + (currentMode === 'refined' ? 'true' : 'false') + '" class="note-mode-pill' + (currentMode === 'refined' ? ' is-active' : '') + '" data-action="campus-switch-note-mode" data-preview="' + esc(key) + '" data-mode="refined"><span class="note-mode-title">✨ 精细整理</span><small class="note-mode-sub' + (hasRefined ? ' is-ready' : '') + '">' + esc(refinedSub) + '</small></button>' +
        '</div>' +
        actionRight +
        (showUnrefinedUpsell
          ? '<p class="reader-refine-upsell-note" data-reader-refine-upsell><strong>✨ 想要更精炼的考点讲义？</strong>使用您配置的模型自动剔除课堂闲谈、提炼公式推导与板书（生成后保存于本机）。</p>'
          : '') +
        '</div>';
    }
    var coursewareBar = '';
    var refiningNotice = lecture && item.refining
      ? '<div class="note-frames-extracting-banner" role="status">✨ 正在调用本地配置的模型剔除课堂闲谈、提炼核心考点并融合课堂投影与板书（' + lenLabel + '）…</div>'
      : '';
    var refineErrorBanner = lecture && item.refineError
      ? '<p class="lecture-quiz-error" role="alert">' + esc(item.refineError) + '</p>'
      : '';
    var refinedPendingCard = isRefinedPendingView
      ? '<div class="refined-empty-card" data-refined-empty-card>' +
        '<h4>✨ 将本讲课堂流水账精细整理为考点讲义</h4>' +
        '<div class="refined-empty-features">' +
        '<span>✂️ 剔除老师寒暄与闲谈</span>' +
        '<span>🎯 提炼核心考点、公式与框图</span>' +
        '<span>🖼️ 融合课堂投影画面与板书</span>' +
        '</div>' +
        '<p>本讲课堂流水账共 ' + esc(lenLabel) + '，点击下方按钮将调用您配置的对话模型提炼保存。</p>' +
        '<div class="refined-empty-actions">' +
        button(item.refining ? '正在生成精细整理…' : '✨ 立即生成精细整理', 'campus-refine-note', {
          primary: true, disabled: Boolean(item.refining),
          attrs: { 'data-preview': key, 'data-force': 'false', 'data-confirmed-card': 'true' }
        }) +
        button('先看课堂流水账', 'campus-switch-note-mode', {
          quiet: true,
          attrs: { 'data-preview': key, 'data-mode': 'raw' }
        }) +
        '</div></div>'
      : '';
    var streamingNotice = lecture && (item.streaming || item.visualsPending)
      ? '<div class="note-frames-extracting-banner" role="status" data-note-stream-banner>' +
        (item.streaming ? '⚡ 正在流式加载课堂笔记（已就绪 ' + (Number(item.streamDoneSections) || 0) + ' / ' + (Number(item.streamTotalSections) || 0) + ' 节）…' : '🖼️ 笔记正文已就绪，正在流式匹配课堂板书与投影画面…') + '</div>'
      : '';
    var extractingNotice = lecture && item.framesExtracting
      ? '<div class="note-frames-extracting-banner" role="status">🖼️ 正在后台从课堂录像补齐板书与投影关键帧，提取完成后将自动刷新显示…</div>'
      : '';
    var toolbarHtml = (item.markdown && !isRefinedPendingView)
      ? '<div class="reader-toolbar">' +
        button(isRefinedView ? '导出精细整理 PDF' : '导出 PDF', 'campus-download-markdown', {small:true, quiet:true, attrs:{'data-preview':key}}) +
        '<input type="search" class="reader-search-input" data-reader-search="' + esc(key) + '" aria-label="搜索笔记内容" placeholder="搜索笔记内容" value="' + esc(item.query || '') + '">' +
        (query ? '<small class="reader-search-count" data-reader-search-count>' + (content.count ? '找到 ' + content.count + ' 处' : '无匹配') + '</small>' : '') + '</div>'
      : '';
    var bodyHtml = view.bodyHtml;
    return '<section class="lecture-note-reader' + (isRefinedView ? ' is-refined-reader' : '') + '" data-reader-key="' + esc(key) + '" aria-label="' + (lecture ? '本讲课堂笔记' : '课程总结预览') + '">' +
      refineBar + coursewareBar + refiningNotice + refineErrorBanner + toolbarHtml +
      (item.loading ? '<p role="status">正在读取本地笔记…</p>' : item.error ? '<p role="alert">' + esc(item.error) + '</p>' :
        isRefinedPendingView ? refinedPendingCard :
        ((lecture ? (isRefinedView ? extractingNotice : (lectureOralHighlightsBox(key, item) + streamingNotice + extractingNotice)) : '') +
        '<div class="study-markdown' + (isRefinedView ? ' is-refined' : '') + '">' + bodyHtml + '</div>')) + '</section>';
  }

  function updateReaderSearchInPlace(key, scrollActive) {
    var item = readingPreviews[key];
    var readerEl = Array.from(document.querySelectorAll('.lecture-note-reader[data-reader-key]')).find(function (el) {
      return el.dataset.readerKey === key;
    });
    if (!item || !item.open || !readerEl) {
      render();
      return;
    }
    var lecture = !/:summary$/.test(key);
    var view = renderPreviewStudyContent(key, item, lecture);
    var mdEl = readerEl.querySelector('.study-markdown');
    if (!mdEl || view.isRefinedPendingView || item.loading || item.error) {
      render();
      return;
    }
    mdEl.innerHTML = view.bodyHtml;
    readerEl._readerSig = previewReaderSignature(key, item, lecture);
    readerEl._markdownSig = previewMarkdownSignature(item);
    var toolbarEl = readerEl.querySelector('.reader-toolbar');
    var countEl = toolbarEl && toolbarEl.querySelector('[data-reader-search-count]');
    if (view.query) {
      if (!countEl && toolbarEl) {
        countEl = document.createElement('small');
        countEl.className = 'reader-search-count';
        countEl.setAttribute('data-reader-search-count', '');
        toolbarEl.appendChild(countEl);
      }
      if (countEl) {
        countEl.textContent = view.content.count ? ('找到 ' + view.content.count + ' 处') : '无匹配';
      }
    } else if (countEl) {
      countEl.remove();
    }
    hydrateMermaidDiagrams();
    if (scrollActive) {
      var activeMark = readerEl.querySelector('mark.study-search-hit-active');
      if (activeMark && activeMark.scrollIntoView) {
        activeMark.scrollIntoView({ block: 'center' });
      }
    }
  }

  function openStudyPreview(recording, forceOpen, silentRefresh) {
    var epoch = accountQuotaEpoch;
    var courseId = state.campus.courseId;
    var key = courseId + ':' + (recording || 'summary');
    var existing = readingPreviews[key];
    if (!forceOpen && existing && existing.open && !existing.loading && !existing.error) {
      existing.open = false; render(); return Promise.resolve();
    }
    var item;
    if (silentRefresh && existing && existing.open && existing.markdown && !existing.error) {
      item = existing;
    } else {
      item = readingPreviews[key] = {
        open:true, loading:true, markdown:'', refinedMarkdown:'', refinedAvailable:false,
        refinedMeta:null, refineEstimate:null, noteMode:'raw', refining:false, refineError:'',
        lecture:Boolean(recording), hitIndex:0, quiz:null, quizPicked:{}, quizLoading:false, quizError:'',
        logicMap:null, logicTab:'all', logicMapLoading:false, logicMapError:'', oralHighlights:[], framesExtracting:false
      };
      render();
    }
    function pollKeyframes() {
      if (recording && item.framesExtracting && item.open) {
        var pollCount = Number(item._keyframePollCount || 0);
        if (pollCount < 18) {
          item._keyframePollCount = pollCount + 1;
          window.setTimeout(function () {
            if (epoch === accountQuotaEpoch && readingPreviews[key] === item && item.open && state.campus.courseId === courseId) {
              openStudyPreview(recording, true, true);
            }
          }, 8000);
        }
      }
    }
    var url = '/api/campus/courses/' + encodeURIComponent(courseId) + (recording ? '/recordings/' + encodeURIComponent(recording) + '/note-preview' : '/summary-preview');
    var previewRequest = recording
      ? fetch(url, {cache: 'no-store', headers: {'Accept': 'application/x-ndjson, application/json', 'X-Note-Stream': '1'}})
      : requestJson(url);
    return previewRequest.then(async function (response) {
      if (epoch !== accountQuotaEpoch) return;
      if (recording && !response.ok) {
        var errorBody = await response.json();
        throw new Error(errorBody && errorBody.detail || '暂时无法读取笔记，请重试。');
      }
      if (recording && response.headers && (response.headers.get('content-type') || '').indexOf('application/x-ndjson') >= 0 && response.body && typeof response.body.getReader === 'function') {
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var pending = '';
        function nextFrame() {
          if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
            return new Promise(function (resolve) { window.requestAnimationFrame(resolve); });
          }
          return Promise.resolve();
        }
        function applyStreamMsg(msg) {
          if (!msg || epoch !== accountQuotaEpoch || readingPreviews[key] !== item) return false;
          if (msg.stage === 'init') {
            item.markdown = msg.markdown_chunk || '';
            item.refinedAvailable = Boolean(msg.refined_available);
            item.refinedMarkdown = msg.refined_markdown || '';
            item.refinedMeta = msg.refined_meta || null;
            item.refineEstimate = msg.refine_estimate || null;
            if (!silentRefresh) item.noteMode = item.refinedAvailable ? 'refined' : 'raw';
            item.visuals = msg.visuals || [];
            item.oralHighlights = msg.oral_highlights || msg.clues || [];
            item.streaming = (msg.total_sections || 0) > (msg.done_sections || 0);
            item.streamDoneSections = msg.done_sections || 0;
            item.streamTotalSections = msg.total_sections || 0;
            item.visualsPending = Boolean(msg.visuals_pending);
            item.framesExtracting = false;
            item.loading = false;
            return true;
          } else if (msg.stage === 'section') {
            item.markdown += msg.markdown_chunk || '';
            item.streamDoneSections = msg.done_sections || item.streamDoneSections;
            item.streaming = item.streamDoneSections < item.streamTotalSections;
            return true;
          } else if (msg.stage === 'addons') {
            item.quiz = msg.quiz || null;
            item.logicMap = msg.logic_map || null;
            return true;
          } else if (msg.stage === 'done') {
            item.streaming = false;
            item.visualsPending = false;
            item.visuals = msg.visuals || item.visuals || [];
            item.framesExtracting = Boolean(msg['keyframe' + 's_extracting']);
            return true;
          }
          return false;
        }
        async function processLines(rawLines) {
          var dirty = false;
          var sawDone = false;
          for (var li = 0; li < rawLines.length; li += 1) {
            var line = rawLines[li];
            if (!line || !line.trim()) continue;
            if (epoch !== accountQuotaEpoch || readingPreviews[key] !== item) return;
            var msg = JSON.parse(line);
            if (!applyStreamMsg(msg)) continue;
            dirty = true;
            if (msg.stage === 'init') {
              render();
              dirty = false;
              await nextFrame();
            } else if (msg.stage === 'done') {
              sawDone = true;
            }
          }
          if (dirty && epoch === accountQuotaEpoch && readingPreviews[key] === item) {
            render();
            await nextFrame();
          }
          if (sawDone && epoch === accountQuotaEpoch && readingPreviews[key] === item) {
            pollKeyframes();
          }
        }
        try {
          while (true) {
            var part = await reader.read();
            if (epoch !== accountQuotaEpoch || readingPreviews[key] !== item) {
              await reader.cancel();
              return;
            }
            pending += part.done ? decoder.decode() : decoder.decode(part.value, {stream: true});
            var lines = pending.split('\n');
            pending = lines.pop();
            if (lines.length) await processLines(lines);
            if (part.done) {
              if (pending && pending.trim()) await processLines([pending]);
              break;
            }
          }
        } finally {
          reader.releaseLock();
        }
        return;
      }
      var result = recording ? {ok: response.ok, body: await response.json()} : response;
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) throw new Error(result.body && result.body.detail || '暂时无法读取笔记，请重试。');
      item.streaming = false; item.visualsPending = false;
      item.markdown = result.body.markdown;
      item.refinedAvailable = Boolean(result.body.refined_available);
      item.refinedMarkdown = result.body.refined_markdown || '';
      item.refinedMeta = result.body.refined_meta || null;
      item.refineEstimate = result.body.refine_estimate || null;
      if (!silentRefresh) item.noteMode = item.refinedAvailable ? 'refined' : 'raw';
      item.visuals = result.body.visuals || []; item.framesExtracting = Boolean(result.body['keyframe' + 's_extracting']); item.quiz = result.body.quiz || null; item.logicMap = result.body.logic_map || null; item.oralHighlights = result.body.oral_highlights || result.body.clues || []; item.loading = false; render();
      pollKeyframes();
    }).catch(function (error) {
      if (epoch !== accountQuotaEpoch) return;
      item.streaming = false; item.visualsPending = false;
      if (silentRefresh && item.markdown) { render(); return; }
      item.loading = false;
      if (forceOpen) item.open = false;
      else item.error = error.message;
      render();
    });
  }

  function mcpManualSectionMarkup(data, host) {
    return '<div class="mcp-section mcp-section-manual" data-mcp-manual-section>' +
      '<div class="mcp-section-head"><h2>' + (data.cloud_web_mode ? '方式二 · 手动配置或 CLI 接入' : '步骤一 · 选择 AI 客户端并写入本地配置') + '</h2><span class="mcp-section-badge">按客户端格式切换</span></div>' +
      '<div class="mcp-hosts" aria-label="选择 AI 工具">' + data.hosts.map(function (row) {
        return button(row.name, 'select-mcp-host', {small:true, primary:row.id === host.id, attrs:{'data-host':row.id, 'aria-pressed':String(row.id === host.id)}});
      }).join('') + '</div>' +
      '<div class="mcp-host-panel" data-active-host="' + esc(host.id) + '">' +
      '<div class="mcp-host-panel-head"><strong class="mcp-host-title">' + esc(host.name) + '</strong>' +
      ((host.format_label || host.badge) ? '<span class="mcp-format-badge">' + esc(host.format_label || host.badge) + '</span>' : '') +
      '</div>' +
      (host.description ? '<p class="mcp-host-guide">' + esc(host.description) + '</p>' : '') +
      '<div class="reader-toolbar mcp-hub-toolbar mcp-host-toolbar">' +
      (host.cli_command ? button('复制终端命令', 'copy-mcp-command', {small:true, primary:!data.cloud_web_mode}) : '') +
      button('复制配置', 'copy-mcp-snippet', {small:true, primary:!data.cloud_web_mode && !host.cli_command, quiet:Boolean(host.cli_command)}) +
      (host.cordis_snippet ? button('复制 Cordis 配置', 'copy-mcp-cordis', {small:true, quiet:true}) : '') +
      '</div>' +
      (host.cli_command ? '<div class="mcp-code-block"><p class="mcp-config-path">终端一键添加</p><pre><code>' + esc(host.cli_command) + '</code></pre></div>' : '') +
      '<div class="mcp-code-block"><p class="mcp-config-path">配置文件：<code>' + esc(host.config_path) + '</code></p><pre><code>' + esc(host.config_snippet) + '</code></pre></div>' +
      (host.cordis_snippet ? '<details class="lecture-more-actions mcp-secondary-disclosure" data-persistent-disclosure="mcp-cordis-config"><summary>可选：Cordis 插件配置（profiles/web/cordis.patch.yml）</summary><pre><code>' + esc(host.cordis_snippet) + '</code></pre></details>' : '') +
      '</div>' +
      '</div>';
  }

  function mcpHubScreen() {
    var data = mcpHub.status;
    if (!data) return '<section class="panel"><h1>AI 工具连接</h1><p role="status">' + esc(mcpHub.error || '正在读取本地知识库…') + '</p>' +
      (mcpHub.error ? button('重试', 'open-mcp-hub', {small:true}) : '') + '</section>';
    var host = data.hosts.find(function (row) { return row.id === mcpHub.host; }) || data.hosts[0];
    var tools = Array.isArray(data.tools_summary) ? data.tools_summary : [];
    var universalPrompt = data.sample_prompt || host.sample_prompt || '';
    var promptSection = '<div class="mcp-section mcp-section-pairing">' +
      '<div class="mcp-section-head"><h2>步骤二 · 发送测试 Prompt</h2><span class="mcp-section-badge">所有 AI 客户端通用</span></div>' +
      '<p class="mcp-section-desc">保存上方配置并重启 AI 工具后，发送下方任意一句即可开始检索课程笔记与作业。</p>' +
      '<blockquote>' + esc(universalPrompt) + '</blockquote>' +
      '<div class="reader-toolbar mcp-hub-toolbar">' +
      button('复制测试 Prompt', 'copy-mcp-prompt', {small:true, primary:true}) +
      (host.homework_prompt || data.homework_prompt ? button('复制作业 Prompt', 'copy-mcp-homework-prompt', {small:true, quiet:true}) : '') +
      '</div></div>';
    var manualSection = mcpManualSectionMarkup(data, host);
    var toolsSection = tools.length
      ? '<details class="lecture-more-actions mcp-tools-disclosure" data-persistent-disclosure="mcp-tools-list">' +
        '<summary>查看 AI 自动发现的 ' + tools.length + ' 个工具列表 (tools/list)</summary>' +
        '<ul class="mcp-tools-list">' + tools.map(function (t) {
          return '<li data-mcp-tool-name="' + esc(t.name) + '" data-mcp-tool-tier="open" data-mcp-tool-locked="false"><code>' + esc(t.name) + '</code> <span class="mcp-section-badge">本地可用</span> — ' + esc(t.desc) + '</li>';
        }).join('') + '</ul>' +
        '</details>'
      : '';
    var leadCopy = '已就绪 <strong>' + esc(data.courses_count) + '</strong> 门课程 · <strong>' + esc(data.notes_count) + '</strong> 篇笔记 · AI 接入后自动发现全部 <strong>' + (tools.length || 7) + '</strong> 个本地 MCP 工具（全开源开放，无任何权限限制）。';
    return '<section class="panel mcp-hub">' +
      '<p class="eyebrow">MCP · AI 自动发现工具 (tools/list)</p>' +
      '<h1>AI 工具连接</h1>' +
      '<p class="mcp-hub-lead">' + leadCopy + '</p>' +
      manualSection + promptSection +
      toolsSection +
      '</section>';
  }

  function openCampusLesson(recordingId) {
    state.campus.section = 'lecture';
    state.campus.lessonId = recordingId;
    var lesson = state.campus.course && state.campus.course.lessons.find(function (item) { return item.id === recordingId; });
    if (lesson && lesson.notes_available) {
      showToast('已打开「' + (lesson.title || '本讲') + '」课堂笔记与老师口述提示');
      openStudyPreview(recordingId, true);
    } else {
      if (lesson) {
        showToast('「' + (lesson.title || '本讲') + '」尚未生成笔记，点击「✨ 转写并精细整理」或「生成基础笔记」即可提取老师口述考点与板书');
      }
      render();
    }
    setTimeout(function () {
      var el = document.getElementById('course-lessons');
      if (el && el.scrollIntoView) el.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }, 30);
  }

  function campusCourseScreen() {
    var course = state.campus.course;
    var section = state.campus.section === "quizzes" ? "home" : state.campus.section || "home";
    var activeIndex = (section === "lecture" && course && Array.isArray(course.lessons))
      ? course.lessons.findIndex(function (lesson) { return lesson.id === state.campus.lessonId; })
      : -1;
    var activeLesson = activeIndex >= 0 ? course.lessons[activeIndex] : null;
    var cleanCourseCrumb = course ? String(course.title || '').replace(/\s*[（(][^）)]*学年[^）)]*[）)]\s*$/, '').trim() || course.title : "教学网课程";
    var lectureCrumb = activeLesson ? ('第 ' + (activeIndex + 1) + ' 节课' + (activeLesson.date ? ' · ' + activeLesson.date : '')) : "讲次";
    var sectionLabels = { assignments: "作业中心", notices: "通知中心", quizzes: "小测中心", materials: "课程资料", settings: "课程设置", lecture: lectureCrumb };
    var trail = [{ label: "我的课程", action: "open-dashboard" }, { label: cleanCourseCrumb, action: section !== "home" ? "campus-course-home" : undefined }];
    if (section !== "home") trail.push({ label: sectionLabels[section] || "课程" });
    var pager = '';
    if (section === "lecture" && course && course.lessons.length > 1 && activeIndex >= 0) {
      pager = '<div class="breadcrumb-pager">' +
        button("上一讲", "campus-open-lesson", { small: true, quiet: true, disabled: activeIndex <= 0, attrs: { "data-recording": activeIndex > 0 ? course.lessons[activeIndex - 1].id : "", "data-lesson-nav": "prev", "title": "上一讲 ([)" } }) +
        button("下一讲", "campus-open-lesson", { small: true, quiet: true, disabled: activeIndex >= course.lessons.length - 1, attrs: { "data-recording": activeIndex < course.lessons.length - 1 ? course.lessons[activeIndex + 1].id : "", "data-lesson-nav": "next", "title": "下一讲 (])" } }) + '</div>';
    }
    var header = breadcrumb(trail, pager);
    // Keep the current course visible while a background refresh replaces it.
    // Replacing a long page with the short loading panel makes the browser
    // clamp the scroll container to zero, which feels like a random jump.
    if (state.campus.loading && !course) return header + '<section class="panel" role="status">正在读取课程目录…</section>';
    if (!course) return header + '<section class="panel" role="alert">' + esc(state.campus.error || "课程尚未同步。") + '</section>';
    var needsTranscription = false;
    var lessonQuery = state.campus.dateQuery.trim().toLowerCase();
    var showLessonFilter = section === "home" && course.lessons.length >= 4;
    var lessons = course.lessons.filter(function (lesson) {
      if (section === "lecture") return lesson.id === state.campus.lessonId;
      return !lessonQuery || [lesson.date || "", lesson.title || "", lesson.note_topic || "",
        (lesson.materials || []).map(function (m) { return m.title || ""; }).join(" ")
      ].join(" ").toLowerCase().includes(lessonQuery);
    }).map(function (lesson, index) {
      var topicLabel = lesson.note_topic || ((lesson.materials && lesson.materials.length) ? '课件：' + lesson.materials.map(function (m) { return m.title; }).join('、') : '');
      var topicLine = topicLabel ? '<p class="lesson-topic-line">' + esc(topicLabel) + '</p>' : '';
      var oralBadge = (lesson.oral_highlights && lesson.oral_highlights.length)
        ? ' <span class="status-badge indexed" style="background:rgba(139,0,18,0.08);color:#8b0012">📢 ' + esc(lesson.oral_highlights.length) + ' 条重点提示</span>'
        : '';
      if (section !== "lecture") {
        var refineTag = lesson.refined_available
          ? '<span class="status-badge indexed refined-ready-badge">✨ 已精细整理</span>'
          : (lesson.notes_available ? '<span class="status-badge refine-hint-badge">可精细整理</span>' : '');
        return '<article class="campus-lesson-page" data-lesson-card="' + esc(lesson.id) + '"><div><small>第 ' + esc(course.lessons.indexOf(lesson) + 1) + ' 节课 · ' + esc(lesson.date || "日期未提供") + '</small>' +
          '<h3>' + formatLessonTitleMarkup(lesson.title, lesson.date) + '</h3>' + topicLine + '<div class="lesson-status">' + (lesson.notes_available ? '<span class="recording-published">已有课堂笔记</span>' :
            lesson.transcript_available ? '<span class="status-badge indexed">已生成转写</span>' : '<span class="status-badge reading">待整理笔记</span>') +
          refineTag +
          oralBadge +
          ((lesson.materials || []).length ? '<small class="lesson-material-count">已关联 ' + esc(lesson.materials.length) + ' 份课件</small>' : '') + '</div></div>' +
          button("进入讲次", "campus-open-lesson", { small: true, attrs: { "data-recording": lesson.id } }) + '</article>';
      }
      var publishExisting = Boolean(lesson.publish_ready && !lesson.video_available);
      var canRegenerate = Boolean(lesson.transcript_available && lesson.notes_available);
      var publishSaved = publishExisting && !canRegenerate;
      needsTranscription = needsTranscription || (!lesson.transcript_available && !publishSaved);
      var details = lesson.duration_seconds ? ' · 约 ' + Math.ceil(lesson.duration_seconds / 60) +
        ' 分钟' : '';
      if (publishSaved) details += ' · 本机已有笔记与课堂画面';
      else if (lesson.transcript_available && !lesson.notes_available) details += ' · 已生成转写';
      if (lesson.video_available && Number(lesson.video_size) > 0) details += ' · 本地视频 ' + formatTransfer(lesson.video_size);
      else if (!lesson.duration_seconds && !lesson.unavailable_reason) details += ' · 在线回放';
      var metaText = lesson.title && lesson.date && String(lesson.title).indexOf(lesson.date) === 0
        ? details.replace(/^ · /, '') : (lesson.date || "日期未记录") + details;
      if (lesson.unavailable_reason) metaText += (metaText ? ' · ' : '') + lesson.unavailable_reason;
      var lessonPreview = readingPreviews[course.id + ':' + lesson.id];
      var lessonPreviewLoading = Boolean(lessonPreview && lessonPreview.open && lessonPreview.loading);
      var lessonPreviewOpen = Boolean(lessonPreview && lessonPreview.open && !lessonPreview.error);
      var blocked = lesson.unavailable_reason || (course.read_only ? course.read_only_message || "教学网尚未完整读取，旧目录只读。" : courseTaskBusy(course.id) ? "这门课正在更新或处理，请等待当前任务完成。" : state.campus.demoConnection ? "演示模式不会实际整理，请在正式程序中登录。" : "");
      var review = state.termReview.courseId === course.id && state.termReview.recordingId === lesson.id ? termReviewPanel(lesson) : "";
      var secondaryButtons = (
        (lesson.video_available
          ? '<a class="btn btn-small btn-quiet" href="/api/recordings/' + encodeURIComponent(lesson.id) + '/video" download>下载原始录像</a>' +
            button("在文件夹中显示", "local-storage-reveal", { small: true, quiet: true, attrs: { "data-recording": lesson.id } }) +
            button("删除本地录像", "local-storage-delete-one", { small: true, quiet: true, attrs: { "data-recording": lesson.id } })
          : button("下载原始录像", "recording-download", { small: true, quiet: true,
              disabled: !state.campus.configured || courseTaskBusy(course.id) || Boolean(lesson.unavailable_reason),
              attrs: { "data-recording": lesson.id } })) +
        (lesson.transcript_available || lesson.notes_ready ? button("核对术语", "campus-review-term", {
          small: true, quiet: true, attrs: { "data-recording": lesson.id, "aria-expanded": review ? "true" : "false" }
        }) : ""));
      var emptyNotePrompt = !lesson.notes_available ? (
        '<aside class="lecture-empty-note-prompt" data-lesson-empty-prompt>' +
        '<strong>💡 本讲尚未生成课堂笔记</strong>' +
        '<span>点击上方「✨ 转写并精细整理」可一次性产出浓缩考点讲义，或点「生成基础笔记」仅整理课堂流水账。</span>' +
        '</aside>'
      ) : '';
      return '<article class="recording-row"><div class="recording-row-head"><span class="course-meta">第 ' + esc(course.lessons.indexOf(lesson) + 1) + ' 节课</span><h3>' + formatLessonTitleMarkup(lesson.title, lesson.date) +
        '</h3>' + topicLine + (metaText ? '<p>' + esc(metaText) + '</p>' : '') +
        '</div><div class="recording-row-actions">' +
        '<div class="recording-primary-bar">' +
        (lesson.notes_available
          ? button(lessonPreviewLoading ? '正在加载笔记…' : lessonPreviewOpen ? '收起本讲笔记' : '阅读本讲笔记',
              'campus-preview-lecture-note', {small:true, primary:lesson.refined_available && !lessonPreviewOpen, quiet:!lesson.refined_available || lessonPreviewOpen, disabled:lessonPreviewLoading, attrs:{'data-recording':lesson.id}}) +
            (!lesson.refined_available && !lessonPreviewOpen
              ? button("✨ 一键精细整理", "campus-quick-refine", { small: true, primary: true, attrs: { "data-recording": lesson.id } })
              : (lesson.refined_available ? '<span class="status-badge indexed refined-ready-badge">✨ 已精细整理</span>' : ''))
          : (button("✨ 转写并精细整理", "campus-process-and-refine", { small: true, primary: true,
                disabled: Boolean(blocked),
                attrs: { "data-recording": lesson.id } }) +
              button("生成基础笔记", "campus-process", { small: true, quiet: true,
                disabled: Boolean(blocked),
                attrs: { "data-recording": lesson.id } }) +
              (blocked ? '<small class="action-reason">' + esc(blocked === (course.read_only_message || '教学网尚未完整读取，旧目录只读。') ? '请先更新这门课' : blocked) + '</small>' : ''))) +
        '</div>' +
        (secondaryButtons ? '<details class="lecture-more-actions" data-persistent-disclosure="lecture-tools-' + esc(lesson.id) + '"><summary>更多工具</summary><div class="recording-secondary-bar">' + secondaryButtons + '</div></details>' : '') +
        (lesson.transcript_reviewed_pending ? '<small class="recording-published">转写术语已修正，可在「更多工具 → 核对术语」中更新笔记。</small>' : "") +
        '</div>' + emptyNotePrompt + previewReader(course.id + ':' + lesson.id, true) + review + '</article>';
    }).join("");
    var materialQuery = section === "materials" ? (state.campus.materialQuery || '').trim().toLowerCase() : '';
    function matchesMaterial(item, lesson) {
      return (state.campus.category === "all" || item.category === state.campus.category) &&
        (!materialQuery || [item.title || '', item.body_text || '', lesson && lesson.title || '',
          (item.files || []).join(' ')].join(' ').toLowerCase().includes(materialQuery));
    }
    var pendingItems = (course.unassigned_materials || []).filter(function (item) {
      return matchesMaterial(item);
    });
    var infoItems = pendingItems.filter(function (item) { return item.category === "information"; });
    var slideItems = pendingItems.filter(function (item) { return item.category !== "information"; });
    var infoSection = infoItems.length ? '<section class="panel" data-materials-info><h2>课程大纲与基本信息</h2><p>教学网提供的课程介绍、教学大纲与考核说明。</p><ul>' +
      infoItems.map(function (item) { return campusMaterial(item, course.id); }).join("") + '</ul></section>' : '';
    var hasLessons = course.lessons.length > 0;
    var pending = (slideItems.length || !materialQuery && !infoItems.length) ? '<section class="panel" data-materials-unassigned><h2>' + (hasLessons ? '课件与资料' : '课程内容') + '</h2><p>教学网提供的课件、资料、作业和课程信息，可直接下载有附件的内容。</p><ul>' +
      (slideItems.length ? slideItems.map(function (item) { return campusMaterial(item, course.id); }).join("") : '<li>' + esc(course.section_states && course.section_states.materials && ['not_synced', 'error', 'cached'].indexOf(course.section_states.materials.state) >= 0 ? course.section_states.materials.message : '当前分类暂无内容。') + '</li>') + '</ul></section>' : '';
    var summary = course.summary && course.summary.title ? course.summary : null;
    var summaryRegenBtn = button(summary ? "重新生成课程总结" : "生成课程总结", "campus-course-summary", {
      small: true, quiet: Boolean(summary),
      disabled: courseTaskBusy(course.id) || state.campus.demoConnection,
      attrs: { "data-course": course.id }
    });
    var summaryControl = '<div class="course-summary-control">' +
      (summary && summary.title ? '<small title="' + esc(summary.title) + '">' + esc(summary.title) + '</small>' : "") +
      summaryRegenBtn + '</div>';
    var summarySpotlightControl = '<div class="course-summary-control">' + summaryRegenBtn + '</div>';
    var courseTracked = Boolean(course.tracked || isCourseTracked(course.id));
    var hero = '<section class="course-hero' + (section !== 'home' ? ' course-hero-compact' : '') + '" data-course-id="' + esc(course.id) + '"><div><span class="course-kicker">教学网课程</span><h1>' + formatCourseTitleMarkup(course.title) +
      '</h1><small>' + esc(course.term || '') + '</small></div><div class="hero-actions">' +
      '<button type="button" class="btn btn-small course-track-btn' + (courseTracked ? ' is-tracked' : '') +
      '" data-action="toggle-course-track" data-course="' + esc(course.id) + '" data-course-track-toggle="' + esc(course.id) +
      '" aria-pressed="' + (courseTracked ? 'true' : 'false') + '" title="' +
        (courseTracked ? '已开启后台自动追踪，点击取消追踪' : '开启后后台定时检查本课课件、作业与新回放') + '">' +
        (courseTracked ? '<span class="track-pulse-dot" aria-hidden="true"></span>已自动追踪' : '⏱ 自动追踪本课') + '</button>' +
        button("更新这门课", "recording-sync-course", { small: true,
          // Preserve real double-click input; the task guard rejects repeats.
          disabled: !state.campus.configured,
          attrs: { "data-course": course.id,
            "aria-disabled": courseTaskBusy(course.id),
            "style": courseTaskBusy(course.id) ? "opacity:.55;cursor:not-allowed;transform:none" : "" }
        }) + (course.lessons.length > 1 ? button('批量整理本课', 'open-course-batch-organize',
          {small:true, quiet:true, attrs:{'data-course':course.id}}) : '')
      button('导出本课笔记 (.zip)', 'campus-export-course-pdf',
        {small:true, quiet:true, attrs:{'data-course':course.id}}) + '</div></section>';
    var notice = course.read_only ? '<section class="panel" role="status">' + cachedCourseNotice(course) + '</section>' : '';
    if (state.campus.loading) notice += '<p role="status">正在读取本课最新缓存；已有内容可继续浏览。</p>';
    if (state.campus.error) notice += '<p role="alert">' + esc(state.campus.error) + '</p>';
    if (state.recordings.error && (!state.recordings.errorCourseId || state.recordings.errorCourseId === course.id))
      notice += '<p role="alert">' + esc(state.recordings.error) + '</p>';
    var batchBanner = (section === "lecture" && state.recordings.batch && state.recordings.batch.state === "done") ? "" : batchOrganizeStatus();
    notice += backgroundSyncStatus() + batchBanner + batchOrganizePanel();
    if (section === "assignments" || section === "notices") return header + hero + notice + recordingTaskStatus() + campusOfficialSections(course, section);
    var cachedSummary = readingPreviews[course.id + ':summary'];
    var hasSummary = Boolean(summary || course.has_summary || (course.has_summary === undefined && (course.lessons || []).some(function (lesson) { return lesson.notes_available; })) || (cachedSummary && cachedSummary.markdown && !cachedSummary.error));
    var summaryPreviewOpen = Boolean(cachedSummary && cachedSummary.open);
    var summaryReadControl = hasSummary ? button(summaryPreviewOpen ? '收起课程总结' : '阅读课程总结',
      'campus-preview-course-summary', {small:true, primary:!summaryPreviewOpen, quiet:summaryPreviewOpen,
        attrs:{'data-course':course.id}}) : '<small class="form-note hint">暂未生成课程总结</small>';
    if (section === "materials") {
      var materialCount = (course.lessons || []).reduce(function (count, lesson) {
        return count + (lesson.materials || []).length;
      }, (course.unassigned_materials || []).length);
      var materialFilter = materialCount >= 4 ? '<div class="section-label materials-filter-bar"><h2>课程资料</h2><input type="search" class="lesson-filter-input" data-campus-material-filter aria-label="筛选课程资料" placeholder="搜索标题、内容或附件名" value="' + esc(state.campus.materialQuery || '') + '"></div>' : '';
      var linkedMaterials = (course.lessons || []).map(function (lesson) {
        var items = (lesson.materials || []).filter(function (item) {
          return matchesMaterial(item, lesson);
        });
        return items.length ? '<section class="panel" data-materials-linked><h2>' + esc(lesson.title) + '</h2><ul>' +
          items.map(function (item) { return campusMaterial(item, course.id); }).join('') + '</ul></section>' : '';
      }).join('');
      var emptyMaterials = materialQuery && !linkedMaterials && !infoItems.length && !slideItems.length ?
        '<div class="empty-materials">没有找到匹配的课程资料。</div>' : '';
      return header + hero + notice + recordingTaskStatus() + '<section class="panel materials-summary-panel"><div class="materials-summary-copy"><strong>课程总结</strong><span>汇总课堂笔记、核心知识点与作业要求</span></div>' + summaryControl +
        summaryReadControl +
        previewReader(course.id + ':summary', false) + '</section>' + materialFilter + linkedMaterials + infoSection + pending + emptyMaterials;
    }
    if (section === "settings") return header + hero + notice + recordingTaskStatus() + '<details class="panel campus-settings" open><summary>课程笔记与 Notion 主页设置</summary>' + campusHomeControls() + campusExistingNotionControl(course) + '</details>';
    if (section === "lecture") return header + hero + notice + '<section class="panel" id="course-lessons">' + recordingTaskStatus() + lessons + '</section>';
    var centers = [['assignments', '作业中心'], ['notices', '通知中心'], ['materials', '课程资料']].map(function (entry) {
      var count = entry[0] === 'assignments' ? (course.assignments || []).length + ' 项记录' :
        entry[0] === 'notices' ? (course.announcements || []).length + ' 则通知' :
          ((course.all_materials || []).length || (course.unassigned_materials || []).length) + ' 份资料';
      var urgentCount = entry[0] === 'assignments' ? (course.assignments || []).filter(function (item) {
        return assignmentUrgency(item) !== '';
      }).length : 0;
      if (urgentCount > 0) count += ' · ' + urgentCount + ' 项即将截止';
      return '<a href="#course-' + entry[0] + '" data-action="campus-open-center" data-center="' + entry[0] + '"' +
        (urgentCount > 0 ? ' data-urgent-assignments="' + urgentCount + '"' : '') + '><span class="center-title">' + esc(entry[1]) + (entry[0] !== 'materials' ? proCenterBadge() : '') + '</span><small class="center-count">' + esc(count) + '</small><span class="center-arrow" aria-hidden="true">↗</span></a>';
    }).join('');
    var activeTaskNotice = state.recordings.task.state === "running" || state.recordings.task.state === "failed" ||
      ["done", "partial", "cancelled"].indexOf(state.recordings.task.state) >= 0 && ["sync", "summary"].indexOf(state.recordings.task.kind) >= 0
      ? recordingTaskStatus() : "";
    var historical = (course.historical_lessons || []).filter(function (lesson) {
      return lesson.historical === true && lesson.read_only === true && lesson.processing_allowed === false &&
        lesson.course_id === course.id && lesson.history_current_revision === course.catalog_revision;
    }).map(function (lesson) {
      return '<article class="campus-lesson-page" data-history-source="' + esc(lesson.source_id) + '"><div><small>历史讲次 · 只读</small>' +
        '<h3>' + esc(lesson.title) + '</h3><p>原记录时间：' + esc(lesson.recorded_at) + '</p></div>' +
        '<small>历史学习内容保存在本机。</small>' + '</article>';
    }).join('');
    var historicalDirectory = historical ? '<section id="course-history" class="campus-lesson-directory"><div class="section-label"><h2>历史讲次</h2><small>只读</small></div>' +
      '<p>这些讲次已不在教学网现行目录中，原页面和学习内容会保留。</p><div class="campus-lesson-pages">' + historical + '</div></section>' : '';
    var spotlight = '<section class="panel course-summary-spotlight" aria-label="课程总结">' +
      '<div class="summary-spotlight-bar">' +
      '<div class="summary-spotlight-header"><h2>课程总结</h2><span class="summary-progress">已整理 ' +
      course.lessons.filter(function (lesson) {return lesson.notes_available;}).length + ' / ' + course.lessons.length + ' 讲课堂笔记</span>' +
      (summary && summary.title ? '<small class="summary-spotlight-meta" title="' + esc(summary.title) + '">' + esc(summary.title) + '</small>' : '') +
      '</div>' +
      '<div class="summary-spotlight-actions">' + summaryReadControl + summarySpotlightControl + '</div>' +
      '</div>' + previewReader(course.id + ':summary', false) + '</section>';
    return header + hero + spotlight + notice + activeTaskNotice + '<nav class="campus-centers" aria-label="课程内容">' + centers + '</nav>' +
      '<section id="course-lessons" class="campus-lesson-directory"><div class="section-label"><h2>每节课</h2><small>' + esc(course.lessons.length) + ' 节课</small>' +
      (showLessonFilter ? '<input type="search" class="lesson-filter-input" data-campus-lesson-filter aria-label="筛选讲次" placeholder="搜索日期或主题" value="' + esc(state.campus.dateQuery || "") + '">' : '') + '</div>' +
      (showLessonFilter && lessonQuery && !lessons ? '<div class="empty-materials">没有找到匹配的讲次。</div>' : hasLessons ? '<div class="campus-lesson-pages">' + lessons + '</div>' : '<p>' + esc(course.section_states && course.section_states.recordings &&
        course.section_states.recordings.state !== "empty" ? course.section_states.recordings.message : "这门课程暂时没有录像，可进入课程资料查看文件。") + '</p>') + '</section>' + historicalDirectory;
  }

  var campusReviewsRead = null;

  function openCampusCourse(courseId) {
    state.view = "campus-course";
    state.campus.section = "home";
    state.campus.lessonId = "";
    state.campus.dateQuery = "";
    state.campus.materialQuery = "";
    state.campus.courseId = courseId;
    rememberCampusCourse(state.campus.course);
    var cached = state.campus.courseCache[courseId];
    state.campus.course = cached && cached.epoch === accountQuotaEpoch ? JSON.parse(JSON.stringify(cached.body)) : null;
    var listed = state.campus.courses.find(function (course) { return course.id === courseId; });
    if (state.campus.course && listed && listed.catalog_revision &&
        listed.catalog_revision !== state.campus.course.catalog_revision) {
      state.campus.course.read_only = true;
      state.campus.course.read_only_message = "课程目录已有更新；先显示旧内容，请等待本课缓存读取完成。";
    }
    state.campus.receipts = []; state.campus.pendingSubmissions = []; state.campus.receiptError = '';
    state.campus.assignmentStatuses = {};
    state.termReview = { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
    state.campus.submission = { selectedId: "", draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } };
    return loadCampusCourse(courseId);
  }

  function termReviewPanel(lesson) {
    var review = state.termReview;
    var termTarget = (lesson && lesson.transcript_review && lesson.transcript_review.target) || review.target || {};
    var candidateTerm = termTarget.candidate_term || lesson.candidate_term || "RSA";
    var ambiguousTerm = termTarget.ambiguous_term || lesson.ambiguous_term || "RAC";
    var content = "";
    if (review.status === "loading") content = '<p role="status">正在查找需要核对的片段…</p>';
    else if (review.status === "none") content = lesson.transcript_reviewed_pending
      ? '<p>这节录像的转写已修正。可点击下方按钮更新本讲笔记。</p>' +
        '<div class="reader-toolbar">' + button("更新本讲笔记", "campus-term-review-regenerate", { small: true, primary: true, attrs: { "data-recording": lesson.id } }) + '</div>'
      : '<p>这节录像目前没有待核对的术语。</p>';
    else if (review.status === "corrected") content = '<p role="status">已修正转写中的术语。可点击下方按钮更新本讲笔记。</p>' +
      '<div class="reader-toolbar">' + button("更新本讲笔记", "campus-term-review-regenerate", { small: true, primary: true, attrs: { "data-recording": lesson.id } }) + '</div>';
    else if (review.target) {
      var target = review.target;
      content = '<p>自动转写对这一处术语不确定，可能把 ' + esc(candidateTerm) + ' 识别成 ' + esc(ambiguousTerm) + '。请听原声并阅读上下文；只有确认老师说的是 ' + esc(candidateTerm) + '，才勾选并提交。</p>' +
        '<blockquote class="term-review-excerpt">' + esc(target.excerpt) + '</blockquote>' +
        '<p class="term-review-time">建议核听 ' + esc(target.start_label || "") + '–' + esc(target.end_label || "") + '</p>' +
        (review.videoAvailable && target.video_url
          ? '<video controls preload="metadata" playsinline data-term-review-video data-seek="' + esc(Math.max(0, Number(target.start) - 8) || 0) + '" src="' + esc(target.video_url) + '">浏览器无法播放这节录像。</video>'
          : '<p>这节录像尚未保存在电脑上。请先使用上方的录像下载操作，再回来核听。</p>' +
            (lesson.video_available ? '<div class="reader-toolbar">' + button("下载本讲录像以便核听", "campus-term-review-download", { small: true, attrs: { "data-recording": lesson.id } }) + '</div>' : '')) +
        (review.videoAvailable && target.video_url ? '<label class="term-review-confirm"><input type="checkbox" data-term-review-confirm' + (review.confirmed ? ' checked' : '') + (review.status === "submitting" ? ' disabled' : '') + '>我已听原声，确认老师说的是 ' + esc(candidateTerm) + '。</label>' +
          button("确认修正为 " + candidateTerm, "campus-submit-term-review", { small: true, primary: true, disabled: !review.confirmed || review.status === "submitting" }) : "");
    }
    return '<section class="term-review-panel" aria-label="' + esc(lesson.title) + '术语核对"><div class="term-review-heading"><h4>核对术语</h4>' +
      button("收起", "campus-close-term-review", { small: true, quiet: true }) + '</div>' + content +
      (review.error ? '<p class="term-review-error" role="alert">' + esc(review.error) + '</p>' : '') + '</section>';
  }

  function reviewCampusHandbook(target) {
    var courseId = state.campus.courseId;
    var url = '/api/campus/courses/' + encodeURIComponent(courseId) +
      '/materials/' + encodeURIComponent(target.dataset.material) +
      '/files/' + encodeURIComponent(target.dataset.file);
    target.disabled = true;
    target.textContent = '正在读取手册…';
    return fetch(url, { cache: 'no-store' }).then(function (response) {
      if (!response.ok) return response.json().catch(function () { return {}; }).then(function (body) {
        throw new Error(body.detail || '手册暂时无法读取，请稍后再试。');
      });
      var cached = response.headers.get('X-Handbook-Cached') === 'true';
      return response.blob().then(function () {
        if (!cached) throw new Error('已取得附件，但它不是可读取的 PDF；请下载原文件人工核对。');
        return loadCampusCourse(courseId).then(function () {
          if (state.campus.courseId !== courseId) return;
          if (state.campus.error || !state.campus.course) {
            throw new Error(state.campus.error || '课程页暂时无法刷新，请稍后重试。');
          }
          var card = (state.campus.course.handbook_reviews || []).find(function (item) {
            return String(item.material_index) === String(target.dataset.material) &&
              String(item.file_index) === String(target.dataset.file);
          });
          showToast(card && card.pages && card.pages.length ?
            '已读取手册；请对照原 PDF 和老师最新通知核对。' :
            'PDF 已保存，但未提取出考核正文；请打开原文件人工核对。');
        });
      });
    }).catch(function (error) {
      showToast(error.message || '手册暂时无法读取，请稍后再试。');
    }).finally(function () {
      if (document.contains(target)) { target.disabled = false; target.textContent = '读取手册并显示要求'; }
    });
  }

  async function moveCampusHome() {
    if (!state.campus.home || !state.campus.parentId) return showToast("请先选择新的 Notion 父页面。");
    if (state.campus.parentId === state.campus.home.parent_id) return;
    if (!await confirmAction("将现有学习主页及其中的课程内容一起移动到所选页面？")) return;
    return requestJson("/api/campus/notion/workspace/move", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_page_id: state.campus.parentId }) }).then(function (result) {
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "学习主页没有移动成功。");
      state.campus.home = result.body;
      render();
      showToast("学习主页已移动，原有内容保留。");
    });
  }

  async function downloadRecording(recordingId) {
    return requestJson("/api/recordings/" + encodeURIComponent(recordingId) + "/download", { method: "POST" }).then(function (result) {
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "录像下载没有开始。");
      state.recordings.task = result.body;
      state.recordings.taskCourseId = state.campus.courseId;
      render();
    });
  }

  function estimateCampusDuration(recordingId) {
    var courseId = state.campus.courseId;
    var target = document.querySelector('[data-action="campus-estimate-duration"][data-recording="' + recordingId + '"]');
    if (target) { target.disabled = true; target.textContent = "正在读取…"; }
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) + "/recordings/" +
      encodeURIComponent(recordingId) + "/duration", { method: "POST" }).then(function (result) {
      if (!result.ok || !result.body) { showToast((result.body && result.body.detail) || "暂时无法读取时长。"); return; }
      if (state.campus.courseId !== courseId) return;
      var lesson = state.campus.course && state.campus.course.lessons.find(function (item) { return item.id === recordingId; });
      if (lesson) lesson.duration_seconds = result.body.duration_seconds;
      if (!result.body.duration_seconds) showToast("教学网仍未提供可用时长，请在确认整理前留意剩余额度。");
      render();
    }).catch(function () { showToast("暂时无法读取时长。"); }).finally(function () {
      if (target && target.isConnected) { target.disabled = false; target.textContent = "读取时长"; }
    });
  }

  async function startCourseSummary(courseId) {
    if (courseTaskBusy(courseId)) {
      return showToast("请等待当前任务完成后再生成课程总结。");
    }
    var path = courseId
      ? "/api/campus/courses/" + encodeURIComponent(courseId) + "/summary"
      : "/api/campus/notion/summaries";
    return requestJson(path, { method: "POST" }).then(function (result) {
      if (!result.ok || !result.body) {
        return showToast((result.body && result.body.detail) || "课程总结没有开始。");
      }
      state.recordings.task = result.body;
      state.recordings.taskCourseId = courseId || "";
      render();
    }).catch(function () { showToast("课程总结没有开始。"); });
  }

  async function processCampusRecording(recordingId, regenerate, autoRefine) {
    var course = state.campus.course;
    var lesson = course && course.lessons.find(function (item) { return item.id === recordingId; });
    if (!lesson) return;
    var canRegenerate = Boolean(lesson.transcript_available && lesson.notes_available);
    var reuseExisting = Boolean(lesson.publish_ready && !lesson.video_available && !canRegenerate && !regenerate);
    var directOss = Boolean(state.recordings.directOss !== false && !lesson.transcript_available && !regenerate && !reuseExisting);
    var notesStrategy = state.recordings.noteStrategyByKey[recordingId] || "topics";
    var wantAutoRefine = Boolean(autoRefine);
    return requestJson("/api/campus/courses/" + encodeURIComponent(course.id) + "/recordings/" + encodeURIComponent(recordingId) + "/process",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.assign({ direct_oss: reuseExisting ? false : directOss, regenerate: Boolean(regenerate), reuse_existing: reuseExisting, auto_refine: wantAutoRefine }, reuseExisting ? {} : { notes_strategy: notesStrategy })) }).then(function (result) {
        if (!result.ok || !result.body) {
          var detail = (result.body && result.body.detail) || "整理没有开始，请检查模型配置后重试。";
          showToast(detail);
          return;
        }
        state.recordings.task = result.body;
        state.recordings.directOss = true;
        state.recordings.taskCourseId = course.id;
        render();
      }).catch(function () {
        showToast("网络异常，整理未能启动，请稍后重试。");
      });
  }

  /* -- model settings & shell ------------------------------------------- */

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
