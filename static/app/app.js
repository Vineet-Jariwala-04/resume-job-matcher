(function () {
  // ---- theme (same "theme" key as the login / signup pages) ----
  var root = document.documentElement, toggle = document.getElementById("themeToggle");
  var saved = localStorage.getItem("theme") || "light";
  function apply(t) { root.setAttribute("data-theme", t); if (toggle) toggle.checked = (t === "dark"); }
  apply(saved);
  if (toggle) toggle.addEventListener("change", function () {
    var t = toggle.checked ? "dark" : "light"; apply(t); localStorage.setItem("theme", t);
  });

  // ---- tiny helpers (all text goes in via textContent, never innerHTML) ----
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  var level = function (n) { return n >= 70 ? "good" : n >= 40 ? "mid" : "low"; };
  function postJSON(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/login"; return new Promise(function () {}); }
        return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, data: d }; });
      });
  }

  // ---- progress bar ------------------------------------------------------------
  // createProgress(host) draws a progress card inside `host`; trackJob() feeds it from /jobs/<id>.
  function createProgress(host) {
    var head = el("div", "progress-head"), title = el("b", "", "Working..."), pct = el("span", "progress-pct", "0%");
    head.appendChild(title); head.appendChild(pct);
    var bar = el("div", "bar"), fill = el("i");
    bar.setAttribute("role", "progressbar"); bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100");
    bar.appendChild(fill);
    var msg = el("p", "muted small progress-msg"), steps = el("ol", "psteps");
    host.textContent = "";
    [head, bar, msg, steps].forEach(function (n) { host.appendChild(n); });

    var shown = 0, target = 0, ceiling = 0, timer = null, stepEls = [];
    function paint() {
      var v = Math.max(0, Math.min(100, shown));
      fill.style.width = v + "%"; pct.textContent = Math.round(v) + "%"; bar.setAttribute("aria-valuenow", String(Math.round(v)));
    }
    function run() {
      clearInterval(timer);
      // glide toward the next stage's start so the bar keeps moving during long AI steps
      timer = setInterval(function () {
        var goal = Math.max(target, Math.min(ceiling - 1, 97));
        if (shown < target) shown += Math.max(1, (target - shown) * 0.25);
        else if (shown < goal) shown += (goal - shown) * 0.02;
        if (shown > 100) shown = 100;
        paint();
      }, 250);
    }
    return {
      start: function (text, stages) {
        title.textContent = text; msg.textContent = "Starting..."; shown = 0; target = 0; ceiling = 0; paint();
        steps.textContent = ""; stepEls = stages.map(function (label) {
          var li = el("li", "pstep"); li.appendChild(el("span", "dot")); li.appendChild(el("span", "", label));
          steps.appendChild(li); return li;
        });
        host.hidden = false; run();
      },
      update: function (s) {
        target = Math.max(target, s.percent || 0); ceiling = s.next_percent || 98;
        var queued = s.status === "queued";
        msg.textContent = queued && s.queue_position
          ? "Another analysis is running. You are number " + s.queue_position + " in the queue - it will start automatically."
          : (s.message || "");
        stepEls.forEach(function (li, i) {
          li.className = "pstep" + (queued ? "" : i < s.stage ? " done" : i === s.stage ? " active" : "");
          var dot = li.firstChild; dot.textContent = (!queued && i < s.stage) ? "\u2713" : "";
        });
      },
      finish: function () {
        clearInterval(timer); shown = 100; paint(); msg.textContent = "Done";
        stepEls.forEach(function (li) { li.className = "pstep done"; li.firstChild.textContent = "\u2713"; });
        setTimeout(function () { host.hidden = true; }, 600);
      },
      hide: function () { clearInterval(timer); host.hidden = true; }
    };
  }

  function trackJob(jobId, progress, onDone, onError) {
    var failures = 0, stopped = false;
    function next() { if (!stopped) setTimeout(tick, 800); }
    function tick() {
      fetch("/jobs/" + encodeURIComponent(jobId), { cache: "no-store" })
        .then(function (r) {
          if (r.status === 401) { window.location.href = "/login"; return null; }
          return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, d: d }; });
        })
        .then(function (x) {
          if (!x) return;
          failures = 0;
          if (!x.ok) { stopped = true; progress.hide(); onError(x.d.error || "Something went wrong."); return; }
          progress.update(x.d);
          if (x.d.status === "done") { stopped = true; progress.finish(); onDone(x.d.result); }
          else if (x.d.status === "error") { stopped = true; progress.hide(); onError(x.d.error || "Something went wrong."); }
          else next();
        })
        .catch(function (err) {
          failures++;
          if (failures >= 5) { stopped = true; progress.hide(); onError("Lost connection to the server: " + err.message); } else next();
        });
    }
    tick();
  }

  // ---- ATS resume card ---------------------------------------------------------
  function scoreRing(label, value, small) {
    var wrap = el("div", "score"), ring = el("div", "ring" + (small ? " small" : ""));
    ring.dataset.level = level(value); ring.style.setProperty("--pct", value);
    ring.appendChild(el("span", "", value + "%"));
    wrap.appendChild(ring); wrap.appendChild(el("div", "score-label", label));
    return wrap;
  }
  function listBlock(title, items, cls) {
    var box = el("div", "ibox"); box.appendChild(el("h4", "", title));
    var ul = el("ul", cls || "");
    items.forEach(function (t) { ul.appendChild(el("li", "", t)); });
    box.appendChild(ul); return box;
  }

  function renderImproved(mount, historyId, imp, previewHtml, onRebuild) {
    mount.textContent = "";
    mount.appendChild(el("h3", "", "Your ATS-optimized resume is ready"));
    var b = imp.ats.before, a = imp.ats.after;
    var cmp = el("div", "compare");
    cmp.appendChild(scoreRing("ATS score before", b.overall, true));
    cmp.appendChild(el("div", "arrow", "\u2192"));
    cmp.appendChild(scoreRing("ATS score after", a.overall, true));
    var detail = el("div", "compare-detail");
    if (a.keywords !== null && a.keywords !== undefined) detail.appendChild(el("p", "", "Job keywords found: " + b.keywords + "% \u2192 " + a.keywords + "%"));
    detail.appendChild(el("p", "", "Format checks passed: " + b.format + "% \u2192 " + a.format + "%"));
    detail.appendChild(el("p", "muted small", "An estimate by this tool (60% keywords, 40% format). Real ATS software varies."));
    cmp.appendChild(detail); mount.appendChild(cmp);

    var dl = el("div", "btn-row");
    [["docx", "Download Word (.docx)"], ["pdf", "Download PDF"]].forEach(function (f) {
      var link = el("a", "btn inline", f[1]); link.href = "/history/" + historyId + "/resume." + f[0]; dl.appendChild(link);
    });
    var again = el("button", "btn inline secondary", "Change confirmed skills"); again.type = "button";
    again.addEventListener("click", onRebuild); dl.appendChild(again);
    mount.appendChild(dl);

    mount.appendChild(listBlock("What changed", imp.changes));
    var reqs = (imp.requirements || []).map(function (r) { return r.label + ": " + r.detail; });
    if (reqs.length) mount.appendChild(listBlock("Job requirements check", reqs));
    var miss = imp.missing || { skills: [], languages: [] };
    if (miss.skills.length || miss.languages.length) {
      var box = el("div", "ibox"); box.appendChild(el("h4", "", "Still missing (NOT added to your resume)"));
      box.appendChild(el("p", "muted small", "The job asks for these but they are not in your resume. Add them only if they are true - go back and tick them, or learn them first."));
      var chips = el("div", "chips");
      miss.skills.forEach(function (s) { chips.appendChild(el("span", "chip bad", s.skill)); });
      miss.languages.forEach(function (l) { chips.appendChild(el("span", "chip bad", "Language: " + l)); });
      box.appendChild(chips); mount.appendChild(box);
    }
    if ((imp.tips || []).length) mount.appendChild(listBlock("Writing tips for your original resume", imp.tips));

    mount.appendChild(el("h4", "preview-title", "Preview"));
    var paper = el("div", "resume-preview"); paper.innerHTML = previewHtml || "";   // escaped on the server
    mount.appendChild(paper);
  }

  function renderChecklist(mount, historyId, info, hasImproved, onDone) {
    mount.textContent = "";
    mount.appendChild(el("h3", "", "Make an ATS-optimized resume for this job"));
    mount.appendChild(el("p", "muted", "The AI rebuilds your resume in an ATS-safe layout, uses the job's wording and checks it against the job description. " +
      "It never invents experience: it only uses what is already in your resume plus the skills you confirm below."));
    var skills = info.missing.skills || [], langs = info.missing.languages || [], boxes = [];
    if (skills.length || langs.length) {
      mount.appendChild(el("h4", "", "The job asks for these, but your resume does not show them"));
      mount.appendChild(el("p", "muted small", "Tick ONLY the ones you genuinely have. They will be added to your new resume. Anything you leave unticked is not added."));
      var groups = {};
      skills.forEach(function (s) { (groups[s.category] = groups[s.category] || []).push(s.skill); });
      if (langs.length) groups["Spoken languages"] = langs;
      Object.keys(groups).forEach(function (cat) {
        var fs = el("fieldset", "skill-group"); fs.appendChild(el("legend", "", cat));
        groups[cat].forEach(function (name) {
          var lab = el("label", "skill-chip"), cb = document.createElement("input");
          cb.type = "checkbox"; cb.value = name; boxes.push(cb);
          lab.appendChild(cb); lab.appendChild(el("span", "", name)); fs.appendChild(lab);
        });
        mount.appendChild(fs);
      });
    } else {
      mount.appendChild(el("p", "alert info", "Your resume already shows every skill the job asks for."));
    }
    var row = el("div", "form-actions left");
    var go = el("button", "btn inline", "Create my ATS resume"); go.type = "button";
    row.appendChild(go);
    if (hasImproved) {
      var cancel = el("button", "btn inline secondary", "Cancel"); cancel.type = "button";
      cancel.addEventListener("click", function () { renderImproved(mount, historyId, info.improved, info.preview_html, function () { renderChecklist(mount, historyId, info, true, onDone); }); });
      row.appendChild(cancel);
    }
    mount.appendChild(row);
    var progressHost = el("div", "card progress-card"); progressHost.hidden = true; mount.appendChild(progressHost);
    var errorBox = el("div", "alert error"); errorBox.hidden = true; mount.appendChild(errorBox);

    go.addEventListener("click", function () {
      errorBox.hidden = true; go.disabled = true;
      var confirmed = boxes.filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
      var progress = createProgress(progressHost);
      postJSON("/history/" + historyId + "/improve", { confirmed: confirmed }).then(function (res) {
        if (!res.ok) { go.disabled = false; errorBox.textContent = res.data.error || "Could not start."; errorBox.hidden = false; return; }
        progress.start("Building your ATS resume", res.data.stages);
        trackJob(res.data.job_id, progress, function (result) {
          info.improved = result.improved; info.preview_html = result.preview_html;
          renderImproved(mount, historyId, result.improved, result.preview_html, function () { renderChecklist(mount, historyId, info, true, onDone); });
          if (onDone) onDone();
        }, function (message) { go.disabled = false; errorBox.textContent = message; errorBox.hidden = false; });
      });
    });
  }

  function mountImprove(mount, historyId, onDone) {
    mount.textContent = "";
    mount.appendChild(el("p", "muted", "Loading..."));
    fetch("/history/" + historyId + "/improve-info", { cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/login"; return null; }
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (x) {
        if (!x) return;
        if (!x.ok) { mount.textContent = ""; mount.appendChild(el("p", "alert error", x.d.error || "Could not load.")); return; }
        if (x.d.improved) renderImproved(mount, historyId, x.d.improved, x.d.preview_html, function () { renderChecklist(mount, historyId, x.d, true, onDone); });
        else renderChecklist(mount, historyId, x.d, false, onDone);
      })
      .catch(function (err) { mount.textContent = ""; mount.appendChild(el("p", "alert error", "Could not load: " + err.message)); });
  }

  // ---- result decoration (match rings etc.) -----------------------------------
  window.decorateResults = function (rootEl) {
    rootEl.querySelectorAll(".arrow-heading").forEach(function (h) { h.textContent = h.textContent.replace(/^\s*\u2192\s*/, ""); });
    rootEl.querySelectorAll(".match-analysis-value").forEach(function (e) {
      var n = parseInt(e.textContent, 10); if (isNaN(n)) return;
      e.classList.add("ring"); e.dataset.level = level(n); e.style.setProperty("--pct", n);
      e.innerHTML = "<span>" + n + "%</span>";
    });
    rootEl.querySelectorAll(".ats-score-box").forEach(function (box) {
      var v = box.querySelector(".ats-score-value"), n = parseInt(v && v.textContent, 10);
      if (!isNaN(n)) { box.style.setProperty("--pct", n); box.dataset.level = level(n); }
    });
  };
  window.AIOT = { createProgress: createProgress, trackJob: trackJob, mountImprove: mountImprove };

  // ---- match page ----
  var form = document.getElementById("matchForm");
  if (form) {
    var matchBtn = document.getElementById("matchBtn");
    var statusBox = document.getElementById("status");
    var resultsSection = document.getElementById("resultsSection");
    var resultsDiv = document.getElementById("results");
    var downloadBtn = document.getElementById("downloadBtn");
    var improveSection = document.getElementById("improveSection");
    var progress = createProgress(document.getElementById("progressHost"));
    var lastMarkdown = "";

    var fmtSize = function (b) { return b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB"; };
    document.querySelectorAll(".dropzone").forEach(function (dz) {
      var input = dz.querySelector("input"), nameEl = dz.querySelector(".dz-file");
      var show = function () {
        var f = input.files[0];
        nameEl.textContent = f ? f.name + " (" + fmtSize(f.size) + ")" : "";
        dz.classList.toggle("has-file", !!f);
      };
      input.addEventListener("change", show);
      ["dragenter", "dragover"].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("drag"); }); });
      ["dragleave", "drop"].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("drag"); }); });
      dz.addEventListener("drop", function (e) { if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); } });
    });

    var showStatus = function (message, isError) {
      statusBox.hidden = false;
      statusBox.className = "alert " + (isError ? "error" : "info");
      statusBox.textContent = message;
    };
    var hideStatus = function () { statusBox.hidden = true; };

    form.addEventListener("submit", async function (e) {
      e.preventDefault(); hideStatus(); resultsSection.hidden = true; improveSection.hidden = true;
      var resumeFile = document.getElementById("resume").files[0];
      var jobFile = document.getElementById("job").files[0];
      if (!resumeFile || !jobFile) { showStatus("Please upload both a resume and a job description.", true); return; }

      var formData = new FormData();
      formData.append("resume", resumeFile); formData.append("job", jobFile);
      matchBtn.disabled = true;
      try {
        var response = await fetch("/match", { method: "POST", body: formData });
        if (response.status === 401) { window.location.href = "/login"; return; }
        var data = await response.json().catch(function () { return {}; });
        if (!response.ok) { showStatus(data.error || "Something went wrong.", true); matchBtn.disabled = false; return; }
        progress.start("Analysing your resume", data.stages);
        trackJob(data.job_id, progress, function (result) {
          resultsDiv.innerHTML = result.result_html;       // sanitised on the server
          window.decorateResults(resultsDiv);
          lastMarkdown = result.result_markdown || "";
          resultsSection.hidden = false;
          improveSection.hidden = false;
          mountImprove(document.getElementById("improveMount"), result.history_id);
          resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
          matchBtn.disabled = false;
        }, function (message) { showStatus(message, true); matchBtn.disabled = false; });
      } catch (err) {
        showStatus("Could not reach the server: " + err.message, true); matchBtn.disabled = false;
      }
    });

    downloadBtn.addEventListener("click", async function () {
      if (!lastMarkdown) return;
      downloadBtn.disabled = true;
      var label = downloadBtn.querySelector("span"), original = label.textContent;
      label.textContent = "Preparing PDF...";
      try {
        var response = await fetch("/download-pdf", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ markdown: lastMarkdown }),
        });
        if (response.status === 401) { window.location.href = "/login"; return; }
        if (!response.ok) {
          var d = await response.json().catch(function () { return {}; });
          showStatus(d.error || "Could not generate the PDF.", true); return;
        }
        var blob = await response.blob(), url = URL.createObjectURL(blob), a = document.createElement("a");
        a.href = url; a.download = "resume_match_report.pdf";
        document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
      } catch (err) {
        showStatus("Could not reach the server: " + err.message, true);
      } finally { downloadBtn.disabled = false; label.textContent = original; }
    });
  }

  // ---- history pages ----
  var detailMount = document.getElementById("improveMount");
  if (detailMount && detailMount.dataset.historyId) {
    var res = document.getElementById("results");
    if (res) window.decorateResults(res);
    mountImprove(detailMount, detailMount.dataset.historyId);
  }
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });
  document.querySelectorAll(".hist-card .ring").forEach(function (r) { /* ring colours come from data-level */ });

  // ---- admin table search ----
  var search = document.getElementById("userSearch");
  if (search) {
    var rows = Array.prototype.slice.call(document.querySelectorAll("#userTable tbody tr"));
    var count = document.getElementById("rowCount"), none = document.getElementById("noRows");
    search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase(), shown = 0;
      rows.forEach(function (r) { var ok = !q || r.textContent.toLowerCase().indexOf(q) !== -1; r.hidden = !ok; if (ok) shown++; });
      count.textContent = "(" + shown + ")"; none.hidden = shown !== 0;
    });
  }
})();
