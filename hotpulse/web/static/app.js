// HotPulse: small progressive enhancements. The site works without JavaScript.
(function () {
  "use strict";
  var HP = window.HP || { i18n: {} };
  var KEY = "hp-saved";

  function store(get, value) {
    try {
      if (get) return JSON.parse(localStorage.getItem(KEY) || "{}");
      localStorage.setItem(KEY, JSON.stringify(value));
    } catch (e) { return {}; }
  }

  // ---- theme toggle -------------------------------------------------------
  var toggle = document.querySelector("[data-theme-toggle]");
  if (toggle) toggle.addEventListener("click", function () {
    var root = document.documentElement;
    var dark = root.dataset.theme ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("hp-theme", root.dataset.theme); } catch (e) {}
  });

  // ---- bookmarks (stay in this browser) ----------------------------------
  function refreshSaved() {
    var saved = store(true) || {};
    var n = Object.keys(saved).length;
    document.querySelectorAll("[data-saved-count]").forEach(function (el) { el.textContent = n ? String(n) : ""; });
    document.querySelectorAll("[data-save]").forEach(function (btn) {
      var on = !!saved[btn.dataset.save];
      btn.classList.toggle("is-on", on);
      btn.textContent = (on ? "★ " + (HP.i18n.saved || "Saved") : "☆ " + (HP.i18n.save || "Save"));
    });
    var list = document.querySelector("[data-saved-list]");
    if (list && n) {
      list.innerHTML = "";
      Object.keys(saved).sort(function (a, b) { return saved[b].t - saved[a].t; }).forEach(function (id) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = "/item/" + encodeURIComponent(id);
        a.textContent = saved[id].title;
        var rm = document.createElement("button");
        rm.className = "link-btn"; rm.type = "button"; rm.textContent = " ✕";
        rm.addEventListener("click", function () { var s = store(true); delete s[id]; store(false, s); location.reload(); });
        li.appendChild(a); li.appendChild(rm); list.appendChild(li);
      });
    }
  }
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-save]");
    if (!btn) return;
    var saved = store(true) || {};
    var id = btn.dataset.save;
    if (saved[id]) delete saved[id];
    else saved[id] = { title: btn.dataset.saveTitle || ("#" + id), t: Date.now() };
    store(false, saved);
    refreshSaved();
  });
  refreshSaved();

  // ---- on-demand translation ---------------------------------------------
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-translate]");
    if (!btn) return;
    if (!HP.aiReady) { btn.textContent = HP.i18n.aiOff; btn.disabled = true; return; }
    var id = btn.dataset.translate;
    var scope = btn.closest("[data-item]") || document;
    btn.disabled = true;
    btn.textContent = HP.i18n.translating || "…";
    fetch("/api/v1/items/" + id + "/translate?lang=" + encodeURIComponent(HP.lang), { method: "POST" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        var t = scope.querySelector("[data-tr-title]"), s = scope.querySelector("[data-tr-summary]"),
            w = scope.querySelector("[data-tr-why]");
        if (t && data.title) t.textContent = data.title;
        if (s && data.summary) s.textContent = data.summary;
        if (w && data.why_it_matters) w.textContent = data.why_it_matters;
        btn.remove();
      })
      .catch(function () { btn.textContent = HP.i18n.aiOff; });
  });

  // ---- share ----------------------------------------------------------------
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-share]");
    if (!btn) return;
    var data = { title: btn.dataset.shareTitle, url: btn.dataset.share };
    if (navigator.share) { navigator.share(data).catch(function () {}); return; }
    copy(data.title + " " + data.url, btn);
  });

  // ---- copy (admin social drafts) ------------------------------------------
  function copy(text, btn) {
    var done = function () { var old = btn.textContent; btn.textContent = HP.i18n.copied || "Copied"; setTimeout(function () { btn.textContent = old; }, 1400); };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () {});
  }
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    var ta = btn.closest(".draft").querySelector("textarea");
    copy(ta.value, btn);
  });
  document.addEventListener("input", function (ev) {
    if (ev.target.matches(".draft textarea")) {
      var c = ev.target.closest(".draft").querySelector("[data-charcount]");
      if (c) {
        // X counts every link as 23 characters
        var v = ev.target.value, n = c.dataset.limit === "280" ? v.replace(/https?:\/\/\S+/g, "x".repeat(23)).length : v.length;
        c.textContent = n + " / " + c.dataset.limit;
        c.style.color = n > Number(c.dataset.limit) ? "var(--bad)" : "";
      }
    }
  });
})();
