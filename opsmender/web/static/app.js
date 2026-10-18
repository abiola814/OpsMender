// OpsMender UI behaviour. Plain JS, no dependencies.
(function () {
  "use strict";

  // Auto-refresh pages that show live data, unless the user is typing or the tab is hidden.
  const refresh = parseInt(document.body.dataset.autorefresh || "0", 10);
  if (refresh > 0) {
    setInterval(function () {
      const active = document.activeElement;
      const typing = active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName);
      const opened = document.querySelector("details[open], pre.logs:not([hidden])");
      if (!document.hidden && !typing && !opened) window.location.reload();
    }, refresh * 1000);
  }

  // Copy buttons: <button data-copy="#selector">
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const target = document.querySelector(btn.dataset.copy);
      if (!target) return;
      navigator.clipboard.writeText(target.textContent.trim()).then(function () {
        const old = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = old; }, 1500);
      });
    });
  });

  // Long-running forms: show a busy label and prevent double submits.
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      const btn = form.querySelector("button[type=submit]");
      if (btn) { btn.disabled = true; btn.textContent = form.dataset.busy; }
    });
  });

  // AI provider presets: show the model hint and default base URL for the chosen provider.
  const select = document.querySelector("[data-provider-select]");
  if (select) {
    const model = document.querySelector("[data-model-input]");
    const help = document.querySelector("[data-model-help]");
    const base = document.querySelector("[data-base-url-input]");
    const update = function () {
      const opt = select.selectedOptions[0];
      if (!opt || !opt.value) { help.textContent = "Rule-based diagnosis only."; return; }
      model.placeholder = opt.dataset.modelHint || "";
      base.placeholder = opt.dataset.baseUrl || "https://…/v1";
      help.textContent = "Use the exact model ID from your provider (" + (opt.dataset.modelHint || "see provider docs") + ").";
    };
    select.addEventListener("change", update);
    update();
  }

  // On-demand log viewer on resource pages.
  const loader = document.querySelector("[data-load-logs]");
  if (loader) {
    const out = document.getElementById("log-output");
    const hint = document.getElementById("log-hint");
    const source = document.getElementById("log-source");
    const analysis = document.getElementById("log-analysis");
    const el = function (tag, cls, text) {
      const node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = text;
      return node;
    };
    loader.addEventListener("click", function () {
      loader.disabled = true;
      loader.textContent = "Loading…";
      fetch(loader.dataset.loadLogs, { credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (data) {
          analysis.replaceChildren();
          source.textContent = data.source || "";
          if (!data.logs) { hint.textContent = data.hint || "No logs."; out.hidden = true; return; }
          hint.textContent = "";
          out.textContent = data.logs;
          out.hidden = false;
          out.scrollTop = out.scrollHeight;
          const findings = (data.analysis && data.analysis.findings) || [];
          findings.slice(0, 3).forEach(function (f) {
            const box = el("div", "finding " + f.severity);
            box.appendChild(el("div", "ftitle", f.title + " · " + f.matches + (f.matches === 1 ? " line" : " lines")));
            box.appendChild(el("div", "dim", f.explanation));
            const ul = el("ul");
            f.suggestions.forEach(function (s) { ul.appendChild(el("li", "", s)); });
            box.appendChild(ul);
            analysis.appendChild(box);
          });
          if (findings.length) analysis.appendChild(el("div", "", "")).style.height = "12px";
        })
        .catch(function (err) { hint.textContent = "Couldn't load logs: " + err.message; })
        .finally(function () { loader.disabled = false; loader.textContent = "Reload logs"; });
    });
  }
})();
