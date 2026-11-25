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

