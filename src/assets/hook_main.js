// WebcastMate Capture - in-process hook for 直播伴侣 (Electron main process)
// Injected as a single `require()` line prepended to resources/app/index.js.
//
// What it does: asks the host app to open an "output" projector window
// (the fully composited stream canvas), immediately moves it off-screen,
// and exposes a tiny file-based command channel so the helper UI can drive
// it (detect canvas size / open / reposition / snapshot).
//
// The window is then captured with Windows.Graphics.Capture by the host tool.
// Nothing is drawn on the user's screen.
"use strict";

var fs = require("fs");
var path = require("path");
var DIR = __dirname + path.sep;
var LOG = DIR + "hook.log";
var CMD = DIR + "cmd.txt";
var RES = DIR + "res.txt";
var CFG = DIR + "config.json";

var CUR_N = null;          // sequence id of the command being handled
var PS = null;             // protosdk
var OPENING = false;
var OPENED = false;
var RETRY = 0;

function loadCfg() {
  var d = {
    auto_open: true, width: 1920, height: 1080, title: "wmc_capture",
    offscreen: -32000, border_w: 18, border_h: 47, retry_max: 12
  };
  try {
    var o = JSON.parse(fs.readFileSync(CFG, "utf8"));
    Object.keys(o).forEach(function (k) { d[k] = o[k]; });
  } catch (e) {}
  return d;
}

function log(s) {
  try { fs.appendFileSync(LOG, "[" + new Date().toISOString() + "] " + s + "\n"); } catch (e) {}
}

function out(obj) {
  try {
    if (CUR_N !== null && obj && typeof obj === "object" && obj._n === undefined) obj._n = CUR_N;
    fs.appendFileSync(RES, JSON.stringify(obj) + "\n");
  } catch (e) {
    try { fs.appendFileSync(RES, JSON.stringify({ error: "out failed" }) + "\n"); } catch (e2) {}
  }
}

function js(v, n) {
  try { return JSON.stringify(v).slice(0, n || 1500); } catch (e) { return "<unserializable>"; }
}

try { fs.writeFileSync(LOG, ""); } catch (e) {}
log("hook loaded. pid=" + process.pid + " electron=" + (process.versions.electron || "?")
    + " node=" + process.versions.node + " dir=" + DIR);

// ------------------------------------------------------------------ sdk access
function findProto() {
  try {
    var s = global.__SDK__;
    if (!s) return null;
    if (s.mediasdk && s.mediasdk.protosdk) return s.mediasdk.protosdk;
    if (s.protosdk) return s.protosdk;
  } catch (e) {}
  return null;
}

function resolvePath(root, p) {
  var parts = p.split("."), cur = root, parent = null;
  for (var i = 0; i < parts.length; i++) {
    if (cur == null) break;
    parent = cur; cur = cur[parts[i]];
  }
  return { fn: cur, self: parent };
}

function api(p, params, tag) {
  return new Promise(function (resolve) {
    var r = resolvePath(PS, p);
    if (typeof r.fn !== "function") {
      out({ tag: tag, path: p, error: "not a function: " + typeof r.fn });
      return resolve(null);
    }
    var t0 = Date.now();
    var pr;
    try { pr = r.fn.call(r.self, params || {}); }
    catch (e) { out({ tag: tag, path: p, exc: String(e && e.message || e) }); return resolve(null); }
    if (pr && typeof pr.then === "function") {
      pr.then(function (v) {
        out({ tag: tag, path: p, ok: v, ms: Date.now() - t0 });
        resolve(v);
      }, function (e) {
        out({ tag: tag, path: p, fail: String(e && (e.message || e.errorCode) || e) });
        resolve(null);
      });
    } else {
      out({ tag: tag, path: p, sync: pr });
      resolve(pr);
    }
  });
}

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

var electron = null;
try { electron = require("electron"); } catch (e) {}

// ------------------------------------------------------------------ sizing
// The projector window can never be taller/wider than the desktop work area,
// otherwise the host app silently clamps it. So: 1:1 when it fits, else scale.
function screenLimit() {
  try {
    var d = electron.screen.getPrimaryDisplay();
    var sf = d.scaleFactor || 1;
    return { w: Math.floor(d.workAreaSize.width * sf) - 80,
             h: Math.floor(d.workAreaSize.height * sf) - 80 };
  } catch (e) {
    try {
      var all = electron.screen.getAllDisplays() || [];
      var mw = 0, mh = 0;
      for (var i = 0; i < all.length; i++) {
        var s = all[i].size, f = all[i].scaleFactor || 1;
        mw = Math.max(mw, Math.floor(s.width * f));
        mh = Math.max(mh, Math.floor(s.height * f));
      }
      if (mw > 0) return { w: mw - 80, h: mh - 80 };
    } catch (e2) {}
    return { w: 1840, h: 1320 };
  }
}

function even(n) { n = Math.round(n); return n - (n % 2); }

function fitSize(w, h, cfg) {
  var lim = screenLimit();
  if (cfg && cfg.max_w > 0 && cfg.max_h > 0) lim = { w: cfg.max_w, h: cfg.max_h };
  var scale = Math.min(1, lim.w / w, lim.h / h);
  return { w: Math.max(2, even(w * scale)), h: Math.max(2, even(h * scale)), scale: scale };
}

// Real output canvas: 1920x1080 landscape, 1080x1920 portrait, 4:3, 21:9 ...
function detectCanvas() {
  return api("call.msapi.GetVideoParameter", { ioIndex: 0 }, "detect").then(function (r) {
    try {
      var v = r && r.video;
      var s = v && v.outputSize;
      if (s && s.x > 0 && s.y > 0) {
        return { w: s.x, h: s.y, fps: v.fps || 0 };
      }
    } catch (e) {}
    return null;
  });
}

// ------------------------------------------------------------------ open flow
function autoOpen(force) {
  var cfg = loadCfg();
  if (!force && cfg.auto_open === false) { log("[auto] disabled by config"); return Promise.resolve(null); }
  if (OPENING) { log("[auto] already opening, skip"); return Promise.resolve(null); }
  OPENING = true;

  return detectCanvas().then(function (d) {
    var cw = (d && d.w) || cfg.width || 1920;
    var ch = (d && d.h) || cfg.height || 1080;
    var fit = fitSize(cw, ch, cfg);
    log("[auto] canvas=" + cw + "x" + ch + (d ? " fps=" + d.fps : " (no canvas yet, default)")
        + " -> window=" + fit.w + "x" + fit.h + " scale=" + fit.scale.toFixed(3));

    return api("call.msapi.Projector.CloseAll", { index: 0 }, "autoClose").then(function () {
      return sleep(300);
    }).then(function () {
      return api("call.msapi.Projector.Open", {
        index: 0, title: cfg.title, cx: fit.w, cy: fit.h, bOutput: true, bNolimitSize: true
      }, "autoOpen");
    }).then(function (r) {
      if (!r || !r.childHwnd) {
        OPENING = false;
        log("[auto] open FAILED " + js(r));
        return null;
      }
      log("[auto] child_hwnd=" + r.childHwnd);
      return sleep(900).then(function () {
        var ow = fit.w + (cfg.border_w || 18);
        var oh = fit.h + (cfg.border_h || 47);
        return api("call.msapi.SetPositionForProjector", {
          index: 0, viewRect: { x: cfg.offscreen, y: cfg.offscreen, z: ow, w: oh }
        }, "autoPos").then(function () {
          OPENING = false; OPENED = true; RETRY = 0;
          log("[auto] READY hwnd=" + r.childHwnd + " canvas=" + cw + "x" + ch
              + " target=" + fit.w + "x" + fit.h + " fps=" + ((d && d.fps) || 0)
              + " outer=" + ow + "x" + oh + " scale=" + fit.scale.toFixed(3));
          return { hwnd: r.childHwnd, canvasW: cw, canvasH: ch, winW: fit.w, winH: fit.h, scale: fit.scale };
        });
      });
    });
  }).catch(function (e) {
    OPENING = false;
    log("[auto] err " + String(e && e.message || e));
    return null;
  });
}

// Retry loop: the canvas may not exist yet when the app just booted
// (user still on the lobby screen / no scene added). Keep trying quietly.
function scheduleAuto() {
  var cfg = loadCfg();
  var max = cfg.retry_max > 0 ? cfg.retry_max : 12;
  autoOpen(false).then(function (r) {
    if (r || OPENED) return;
    RETRY++;
    if (RETRY <= max) {
      log("[auto] retry " + RETRY + "/" + max + " in 5s");
      setTimeout(scheduleAuto, 5000);
    } else {
      log("[auto] giving up after " + max + " retries (add a video source then call op=auto)");
    }
  });
}

// ------------------------------------------------------------------ commands
var HANDLERS = {
  ping: function () { out({ tag: "ping", pid: process.pid, t: Date.now(), opened: OPENED }); },

  detect: function () {
    return detectCanvas().then(function (v) {
      var fit = v ? fitSize(v.w, v.h, loadCfg()) : null;
      var res = { size: v, fit: fit, opened: OPENED };
      out(res);
      return res;
    });
  },

  auto: function () { return autoOpen(true); },

  openProj: function (c) {
    return api("call.msapi.Projector.Open", {
      index: c.index == null ? 0 : c.index,
      title: c.title || "wmc_capture",
      cx: c.cx || 1920, cy: c.cy || 1080,
      x: c.x == null ? 0 : c.x, y: c.y == null ? 0 : c.y,
      bOutput: c.bOutput !== false,
      bRatio: !!c.bRatio,
      bNolimitSize: c.bNolimitSize !== false,
      visualId: c.visualId || ""
    }, "openProj");
  },

  setPosProj: function (c) {
    return api("call.msapi.SetPositionForProjector", {
      index: c.index == null ? 0 : c.index,
      viewRect: { x: c.x || 0, y: c.y || 0, z: c.cx || 1920, w: c.cy || 1080 }
    }, "setPosProj");
  },

  closeProj: function (c) {
    OPENED = false;
    return api("call.msapi.Projector.Close", { index: c.index == null ? 0 : c.index }, "closeProj");
  },

  closeAllProj: function (c) {
    OPENED = false;
    return api("call.msapi.Projector.CloseAll", { index: c.index == null ? 0 : c.index }, "closeAllProj");
  },

  // last-resort snapshot straight from the host app (no WGC involved)
  snap: function (c) {
    return api("call.msapi.OutputThumbnailSaveAs", {
      viewIndex: c.index || 0, filePath: c.path, format: c.format || 2
    }, "snap");
  },

  keys: function (c) {
    var r = resolvePath(PS, c.path || "call");
    var names = [];
    try { names = Object.keys(r.fn); } catch (e) {}
    out({ tag: "keys", path: c.path || "call", names: names });
  },

  raw: function (c) { return api(c.path, c.params || {}, c.tag || "raw"); },

  hello: function () { out({ tag: "hello", pid: process.pid }); }
};

var lastCmd = "";
function pollCmd() {
  var txt = "";
  try { txt = fs.readFileSync(CMD, "utf8").trim(); } catch (e) { return; }
  if (!txt || txt === lastCmd) return;
  lastCmd = txt;
  var c;
  try { c = JSON.parse(txt); } catch (e) {
    out({ error: "bad json", raw: txt.slice(0, 120) });
    return;
  }
  CUR_N = c._n === undefined ? null : c._n;
  log("CMD " + txt.slice(0, 180));
  var h = HANDLERS[c.op];
  if (!h) { out({ error: "unknown op", op: c.op }); return; }
  try {
    var r = h(c);
    if (r && typeof r.then === "function") {
      r.catch(function (e) { out({ error: String(e) }); });
    }
  } catch (e) { out({ error: String(e && e.message || e) }); }
}

// ------------------------------------------------------------------ boot
var waited = 0;
var bootTimer = setInterval(function () {
  waited += 2;
  var p = findProto();
  if (!p) {
    if (waited % 20 === 0) log("poll t=" + waited + "s waiting __SDK__");
    if (waited >= 240) { clearInterval(bootTimer); log("!! timeout waiting __SDK__"); }
    return;
  }
  PS = p;
  clearInterval(bootTimer);
  log(">>> protosdk ready (t=" + waited + "s)");
  try { log("    msapi = " + Object.keys(PS.call.msapi).join(",")); } catch (e) {}
  out({ tag: "ready", pid: process.pid, t: waited });

  setInterval(pollCmd, 700);
  try { lastCmd = fs.readFileSync(CMD, "utf8").trim(); } catch (e) { lastCmd = ""; }
  log("cmd channel armed: " + CMD);

  setTimeout(function () { scheduleAuto(); }, 6000);
}, 2000);
