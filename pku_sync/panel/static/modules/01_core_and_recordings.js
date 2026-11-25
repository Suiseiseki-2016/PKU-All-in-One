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

