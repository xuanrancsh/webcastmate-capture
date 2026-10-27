/* WebcastMate Capture — UI logic */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const el = {
    pill: $("#pill-state"),
    pillRes: $("#pill-res"),
    preview: $("#preview"),
    ph: $("#placeholder"),
    stage: $("#stage"),
    progWrap: $("#progress-wrap"),
    progBar: $("#progress-bar"),
    canvas: $("#s-canvas"),
    aspect: $("#s-aspect"),
    fps: $("#s-fps"),
    scale: $("#s-scale"),
    msg: $("#s-msg"),
    start: $("#btn-start"),
    stop: $("#btn-stop"),
    snap: $("#btn-snap"),
    logbox: $("#logbox"),
    inPath: $("#in-path"),
    adminDot: $("#admin-dot"),
  };

  let S = {};
  let streaming = false;
  let busy = false;
  let autoStopped = false;

  /* ─────────────────────────── helpers ─────────────────────────── */
  function toast(text, kind = "") {
    const wrap = $("#toasts");
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.textContent = text;
    wrap.appendChild(t);
    setTimeout(() => {
      t.classList.add("out");
      setTimeout(() => t.remove(), 240);
    }, kind === "err" ? 5200 : 2800);
  }

  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
    const r = await fetch(path, opt);
    const ct = r.headers.get("Content-Type") || "";
    if (ct.includes("application/json")) return r.json();
    return { ok: r.ok };
  }

  const STATE_TEXT = {
    idle: "未启动", locating: "正在定位", injecting: "注入钩子",
    launching: "启动中", waiting: "等待画面", ready: "抓取中", error: "出错",
  };

  /* ─────────────────────────── render ─────────────────────────── */
  function render(st) {
    S = st;
    const state = st.state;

    // pill
    el.pill.className = "pill";
    if (state === "ready") el.pill.classList.add("run");
    else if (state === "error") el.pill.classList.add("err");
    else if (state !== "idle") el.pill.classList.add("busy");
    el.pill.querySelector("b").textContent = STATE_TEXT[state] || state;

    const hasCanvas = st.canvas_w > 0 && st.canvas_h > 0;
    el.pillRes.hidden = !hasCanvas;
    if (hasCanvas) {
      el.pillRes.textContent =
        `${st.canvas_w}×${st.canvas_h}${st.aspect !== "-" ? "  " + st.aspect : ""}`;
    }

    // progress
    const showProg = state !== "idle" && state !== "ready" && state !== "error";
    el.progWrap.hidden = !showProg;
    el.progBar.style.width = (st.progress || 0) + "%";

    // stats
    el.canvas.textContent = hasCanvas ? `${st.canvas_w} × ${st.canvas_h}` : "—";
    el.aspect.textContent = st.aspect !== "-" ? st.aspect : "—";
    el.fps.textContent = st.fps ? st.fps + " fps" : "—";
    el.scale.textContent = (st.capturing && st.scale)
      ? "×" + Number(st.scale).toFixed(3).replace(/0+$/, "").replace(/\.$/, "")
      : "—";
    el.msg.textContent = st.message || "—";

    // buttons
    el.start.disabled = busy || state === "locating" || state === "injecting" ||
      state === "launching" || state === "waiting" || state === "ready";
    el.stop.disabled = busy || (state === "idle" && !st.injected);
    el.snap.disabled = !st.capturing;

    // stage
    if (st.capturing && !streaming) attachStream();
    if (!st.capturing && streaming) detachStream();
    el.stage.classList.toggle("live", !!st.capturing);
    el.ph.style.display = st.capturing ? "none" : "flex";

    // misc
    el.inPath.value = st.index_js || "";
    el.adminDot.classList.toggle("ok", !!st.admin);
    el.adminDot.title = st.admin ? "管理员权限：已获得" : "普通权限：可能无法写入安装目录";
    const $av = $("#about-ver");
    if ($av) {
      $av.textContent = "v" + (st.version || "—");
      $("#about-hook").textContent = st.hook_dir || "—";
      $("#about-data").textContent = st.data_dir || "—";
    }

    // auto-stop detection: capture window vanished
    if (autoStopped === false && state === "error" && streaming) detachStream();
  }

  function attachStream() {
    streaming = true;
    el.preview.classList.add("on");
    el.preview.src = "/api/stream?t=" + Date.now();
  }

  function detachStream() {
    streaming = false;
    el.preview.classList.remove("on");
    el.preview.removeAttribute("src");
    el.ph.style.display = "flex";
  }

  // recover a dead stream
  setInterval(() => {
    if (streaming && S.capturing && el.preview.naturalWidth === 0) {
      el.preview.src = "/api/stream?t=" + Date.now();
    }
  }, 6000);

  /* ─────────────────────────── polling ─────────────────────────── */
  async function poll() {
    try {
      const st = await api("/api/state");
      if (st && st.state) render(st);
    } catch (e) { /* server gone */ }
  }

  let logTimer = 0;
  async function pollLogs() {
    if (!$("#view-logs").classList.contains("on")) return;
    try {
      const r = await api("/api/logs?tail=300");
      const box = el.logbox;
      const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
      box.textContent = (r.logs || []).join("\n");
      if (atEnd) box.scrollTop = box.scrollHeight;
    } catch (e) { /* ignore */ }
  }

  setInterval(poll, 900);
  setInterval(pollLogs, 1200);
  poll();

  /* ─────────────────────────── actions ─────────────────────────── */
  el.start.addEventListener("click", async () => {
    busy = true; el.start.disabled = true;
    detachStream();
    try {
      const r = await api("/api/start", {});
      if (!r.ok) toast(r.message || "启动失败", "err");
    } catch (e) { toast("无法连接后台服务", "err"); }
    busy = false;
    poll();
  });

  el.stop.addEventListener("click", async () => {
    busy = true; el.stop.disabled = true;
    try {
      const r = await api("/api/stop", { restore: true });
      if (r.ok) toast("已停止，钩子已还原", "ok");
      else toast(r.message || "停止失败", "err");
    } catch (e) { toast("无法连接后台服务", "err"); }
    detachStream();
    busy = false;
    poll();
  });

  el.snap.addEventListener("click", async () => {
    try {
      const r = await api("/api/snapshot", {});
      if (r.ok) toast("已保存到 " + r.path, "ok");
      else toast(r.message || "截图失败", "err");
    } catch (e) { toast("截图失败", "err"); }
  });

  function switchView(v) {
    const btn = $$(".nav").find((x) => x.dataset.view === v);
    if (!btn) return;
    $$(".nav").forEach((x) => x.classList.toggle("on", x === btn));
    $$(".view").forEach((s) => s.classList.toggle("on", s.id === "view-" + v));
    if (location.hash !== "#" + v) history.replaceState(null, "", "#" + v);
    if (v === "logs") pollLogs();
  }

  $$(".nav").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
  switchView((location.hash || "#capture").slice(1));

  /* keep-alive: tells the launcher the window is still open */
  setInterval(() => {
    fetch("/api/ping", { cache: "no-store" }).catch(() => {});
  }, 5000);

  /* ─────────────────────────── settings ─────────────────────────── */
  $("#btn-locate").addEventListener("click", async () => {
    toast("正在扫描…");
    const r = await api("/api/locate", { deep: true });
    if (r.ok) toast("已找到安装位置", "ok"); else toast(r.message || "未找到", "err");
    poll();
  });

  $("#btn-browse").addEventListener("click", async () => {
    const r = await api("/api/win", { action: "browse" });
    if (r && r.ok) { toast("已设置安装路径", "ok"); poll(); }
    else if (r && r.message) toast(r.message, "err");
  });

  $("#btn-inject").addEventListener("click", async () => {
    const r = await api("/api/inject", {});
    toast(r.message || (r.ok ? "已注入" : "注入失败"), r.ok ? "ok" : "err");
    poll();
  });

  $("#btn-restore").addEventListener("click", async () => {
    const r = await api("/api/restore", {});
    toast(r.message || (r.ok ? "已还原" : "还原失败"), r.ok ? "ok" : "err");
    poll();
  });

  $("#btn-open-data").addEventListener("click", () => api("/api/open-folder", { what: "data" }));

  const sliders = [
    ["#r-maxw", "#o-maxw", "max_w"],
    ["#r-q", "#o-q", "quality"],
    ["#r-fps", "#o-fps", "fps"],
  ];

  async function loadSettings() {
    const r = await api("/api/settings");
    const p = r.preview || {};
    for (const [sel, out, key] of sliders) {
      const inp = $(sel);
      inp.value = p[key];
      $(out).textContent = p[key];
      paintRange(inp);
    }
  }

  function paintRange(inp) {
    const pct = (inp.value - inp.min) / (inp.max - inp.min) * 100;
    inp.style.setProperty("--p", pct + "%");
  }

  let saveTimer = 0;
  for (const [sel, out, key] of sliders) {
    const inp = $(sel);
    paintRange(inp);
    inp.addEventListener("input", () => {
      $(out).textContent = inp.value;
      paintRange(inp);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        const body = {};
        for (const [s2, , k2] of sliders) body[k2] = Number($(s2).value);
        api("/api/settings", { preview: body });
      }, 260);
    });
  }
  loadSettings();

  // about
  $("#btn-github").addEventListener("click", () =>
    api("/api/open-url", { url: "https://github.com/xuanrancsh/webcastmate-capture" }));
  $("#btn-open-hook").addEventListener("click", () =>
    api("/api/open-folder", { what: "hook" }));

  /* ─────────────────────────── logs ─────────────────────────── */
  $("#btn-log-clear").addEventListener("click", () => { el.logbox.textContent = ""; });
  $("#btn-log-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(el.logbox.textContent);
      toast("已复制", "ok");
    } catch (e) { toast("复制失败", "err"); }
  });

  /* ─────────────────────────── help ─────────────────────────── */
  $("#btn-help").addEventListener("click", () => {
    toast("步骤：① 打开直播伴侣并进入直播画面 ② 点「开始抓取」③ 完成后点「停止并还原」");
  });

  /* Esc = 关闭当前提示, Ctrl+Q = 退出 */
  window.addEventListener("keydown", (e) => {
    if (e.key === "q" && (e.ctrlKey || e.metaKey)) api("/api/win", { action: "close" });
  });
})();
