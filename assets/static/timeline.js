/* Reel2Reel — hand-rolled, no-build, vanilla-JS multi-track timeline + the
 * Gradio<->browser state bridge. Delivered via WAN2GPPlugin.add_custom_js(),
 * which Wan2GP splices into the single gr.Blocks(js=...) init function that runs
 * once on app load — so we wrap in a guarded IIFE, publish window.R2RTimeline,
 * and (re)mount against our own elem_ids once Gradio renders them.
 *
 * STATE BRIDGE (two hidden gr.Textbox pipes):
 *   outbound (browser -> Python): write edit-state JSON into #r2r_tl_to_py via the
 *     native value setter + a bubbling 'input' event, committed on pointerup.
 *   inbound  (Python -> browser): Python returns {seq, op, edit}; #r2r_tl_from_py's
 *     .change(fn=None, js=...) hook calls applyOp(). A monotonic seq dedupes.
 *
 * EDITOR: drag-move (lane-locked), edge-trim, ruler scrub, zoom, snap toggle,
 * zoom-to-fit, transport (play/pause), keyboard shortcuts, and visual cues for
 * fades, opacity, mute, transitions and audio waveforms. Property edits (gain,
 * fades, opacity, mute, transitions, track ops, undo/redo) are Gradio-side and
 * arrive back as a load envelope.
 */
(function () {
  if (window.R2RTimeline) return;

  var ROOT_ID = "r2r_timeline_root", TO_PY = "r2r_tl_to_py", FROM_PY = "r2r_tl_from_py";

  var S = {
    edit: { name: "Cut 1", fps: 24, tracks: [], clips: [], transitions: [],
            ui: { px_per_sec: 80, playhead: 0, selected: null, snap: true } },
    pxPerSec: 80, snap: true, lastSeqIn: -1, mounted: false, playing: false,
    root: null, lanes: null, ruler: null, playhead: null, video: null, readout: null,
    pushTimer: null, rafId: null, lastT: 0,
  };

  // ---- bridge ---------------------------------------------------------------
  function setNativeValue(el, value) {
    var proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function pushNow() {
    var ta = document.querySelector("#" + TO_PY + " textarea, #" + TO_PY + " input");
    if (!ta) return;
    S.edit.ui = S.edit.ui || {};
    S.edit.ui.px_per_sec = S.pxPerSec; S.edit.ui.snap = S.snap;
    try { setNativeValue(ta, JSON.stringify(S.edit)); } catch (e) { console.error("[R2R] push", e); }
  }
  function commit() { if (S.pushTimer) clearTimeout(S.pushTimer); S.pushTimer = setTimeout(pushNow, 120); }

  function applyOp(payload) {
    if (!payload) return;
    var msg; try { msg = typeof payload === "string" ? JSON.parse(payload) : payload; }
    catch (e) { console.error("[R2R] bad inbound", e); return; }
    if (typeof msg.seq === "number" && msg.seq <= S.lastSeqIn) return;
    if (typeof msg.seq === "number") S.lastSeqIn = msg.seq;
    if (msg.op === "load" && msg.edit) {
      S.edit = msg.edit;
      var ui = msg.edit.ui || {};
      if (ui.px_per_sec) S.pxPerSec = ui.px_per_sec;
      if (typeof ui.snap === "boolean") S.snap = ui.snap;
      renderAll(); syncSnapBox();
    }
  }

  // ---- geometry -------------------------------------------------------------
  function sec2px(s) { return s * S.pxPerSec; }
  function px2sec(p) { return p / S.pxPerSec; }
  function clips() { return S.edit.clips || []; }
  function totalDur() {
    return clips().reduce(function (m, c) { return Math.max(m, (c.start || 0) + (c.dur || 0)); }, 0);
  }
  function snapVal(s) {
    if (!S.snap) return Math.max(0, s);
    var grid = 0.25, best = Math.round(s / grid) * grid, bestD = Math.abs(best - s);
    clips().forEach(function (c) {
      [c.start, c.start + c.dur].forEach(function (e) {
        if (Math.abs(e - s) < bestD && Math.abs(e - s) < 0.2) { best = e; bestD = Math.abs(e - s); }
      });
    });
    return Math.max(0, best);
  }

  // ---- rendering ------------------------------------------------------------
  function renderAll() {
    if (!S.mounted) return;
    S.lanes.innerHTML = "";
    (S.edit.tracks || []).forEach(function (t) {
      var row = document.createElement("div");
      row.className = "r2r-track r2r-" + (t.kind || "Video").toLowerCase();
      var head = document.createElement("div");
      head.className = "r2r-head";
      var flags = (t.muted ? " 🔇" : "") + (t.solo ? " ◎" : "") + (t.locked ? " 🔒" : "");
      head.innerHTML = "<span>" + (t.name || t.id) + "</span><small>" + (t.kind) + flags + "</small>";
      var lane = document.createElement("div");
      lane.className = "r2r-lane"; lane.dataset.track = t.id;
      clips().filter(function (c) { return c.track === t.id; })
        .forEach(function (c) { lane.appendChild(renderClip(c, t)); });
      renderTransitions(lane, t);
      row.appendChild(head); row.appendChild(lane); S.lanes.appendChild(row);
    });
    var w = Math.max(600, sec2px(totalDur()) + 200);
    S.ruler.style.width = w + "px";
    drawRuler(); placePlayhead(); driveVideo(); updateReadout();
  }

  function renderClip(c, t) {
    var el = document.createElement("div");
    el.className = "r2r-clip" + (S.edit.ui && S.edit.ui.selected === c.id ? " sel" : "")
      + (c.mute ? " muted" : "");
    el.dataset.id = c.id;
    el.style.transform = "translateX(" + sec2px(c.start) + "px)";
    el.style.width = Math.max(8, sec2px(c.dur)) + "px";
    if (c.thumb_url) el.style.backgroundImage = "url('" + c.thumb_url + "')";
    if (c.opacity != null && c.opacity < 0.999) el.style.outlineOffset = "-2px";
    var lbl = document.createElement("span"); lbl.className = "r2r-label";
    lbl.textContent = (c.label || c.id) + (c.mute ? " 🔇" : "")
      + (c.opacity != null && c.opacity < 0.999 ? "  " + Math.round(c.opacity * 100) + "%" : "");
    el.appendChild(lbl);
    if (c.fade_in > 0.001) el.appendChild(fadeTri("l", sec2px(c.fade_in)));
    if (c.fade_out > 0.001) el.appendChild(fadeTri("r", sec2px(c.fade_out)));
    var hl = document.createElement("div"); hl.className = "r2r-handle l";
    var hr = document.createElement("div"); hr.className = "r2r-handle r";
    el.appendChild(hl); el.appendChild(hr);
    wireClip(el, c, hl, hr);
    return el;
  }
  function fadeTri(side, w) {
    var f = document.createElement("div");
    f.className = "r2r-fade " + side; f.style.width = Math.min(w, 200) + "px";
    return f;
  }
  function renderTransitions(lane, t) {
    (S.edit.transitions || []).filter(function (x) { return x.track === t.id; })
      .forEach(function (x) {
        var m = document.createElement("div");
        m.className = "r2r-trans";
        m.style.transform = "translateX(" + sec2px(x.position) + "px)";
        m.style.width = Math.max(6, sec2px(x.duration)) + "px";
        m.title = "dissolve " + x.duration + "s";
        lane.appendChild(m);
      });
  }
  function drawRuler() {
    S.ruler.innerHTML = "";
    var dur = Math.ceil(totalDur()) + 4;
    var step = S.pxPerSec < 40 ? 5 : (S.pxPerSec < 90 ? 2 : 1);
    for (var s = 0; s <= dur; s += step) {
      var tick = document.createElement("div");
      tick.className = "r2r-tick"; tick.style.left = sec2px(s) + "px"; tick.textContent = s + "s";
      S.ruler.appendChild(tick);
    }
  }
  function placePlayhead() {
    S.playhead.style.transform = "translateX(" + sec2px((S.edit.ui && S.edit.ui.playhead) || 0) + "px)";
  }
  function fmt(s) { s = Math.max(0, s); var m = Math.floor(s / 60); var r = (s % 60); return m + ":" + (r < 10 ? "0" : "") + r.toFixed(2); }
  function updateReadout() {
    if (S.readout) S.readout.textContent = fmt((S.edit.ui && S.edit.ui.playhead) || 0) + " / " + fmt(totalDur());
  }

  // ---- preview --------------------------------------------------------------
  function driveVideo() {
    if (!S.video) return;
    var ph = (S.edit.ui && S.edit.ui.playhead) || 0, hit = null;
    clips().forEach(function (c) {
      if (c.kind === "Video" && c.url && ph >= c.start && ph < c.start + c.dur) hit = c;
    });
    if (!hit) return;
    if (S.video.getAttribute("data-src") !== hit.url) {
      S.video.setAttribute("data-src", hit.url); S.video.src = hit.url;
    }
    try { S.video.currentTime = (hit.in || 0) + (ph - hit.start); } catch (e) {}
  }

  // ---- transport ------------------------------------------------------------
  function setPlayhead(s) {
    S.edit.ui = S.edit.ui || {}; S.edit.ui.playhead = Math.max(0, s);
    placePlayhead(); driveVideo(); updateReadout();
  }
  function play() {
    if (S.playing) return; S.playing = true; S.lastT = 0;
    var step = function (ts) {
      if (!S.playing) return;
      if (!S.lastT) S.lastT = ts;
      var dt = (ts - S.lastT) / 1000; S.lastT = ts;
      var ph = ((S.edit.ui && S.edit.ui.playhead) || 0) + dt;
      if (ph >= totalDur()) { setPlayhead(totalDur()); stop(); commit(); return; }
      setPlayhead(ph); S.rafId = requestAnimationFrame(step);
    };
    S.rafId = requestAnimationFrame(step);
  }
  function stop() { S.playing = false; if (S.rafId) cancelAnimationFrame(S.rafId); S.rafId = null; }
  function togglePlay() { S.playing ? (stop(), commit()) : play(); }

  // ---- interaction ----------------------------------------------------------
  function wireClip(el, c, hl, hr) {
    var mode = null, x0 = 0, start0 = 0, in0 = 0, out0 = 0, moved = false;
    function down(e, m) {
      e.preventDefault(); e.stopPropagation();
      mode = m; x0 = e.clientX; start0 = c.start; in0 = c.in; out0 = c.out; moved = false;
      select(c.id);
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up, { once: true });
    }
    function move(e) {
      var ds = px2sec(e.clientX - x0); if (Math.abs(e.clientX - x0) > 2) moved = true;
      if (mode === "move") {
        c.start = snapVal(Math.max(0, start0 + ds));
        el.style.transform = "translateX(" + sec2px(c.start) + "px)";
      } else if (mode === "l") {
        var ni = Math.min(out0 - 1 / (S.edit.fps || 24), Math.max(0, in0 + ds));
        c.in = ni; c.start = Math.max(0, start0 + (ni - in0)); c.dur = c.out - c.in;
        el.style.transform = "translateX(" + sec2px(c.start) + "px)";
        el.style.width = Math.max(8, sec2px(c.dur)) + "px";
      } else if (mode === "r") {
        c.out = Math.max(c.in + 1 / (S.edit.fps || 24), out0 + ds); c.dur = c.out - c.in;
        el.style.width = Math.max(8, sec2px(c.dur)) + "px";
      }
    }
    function up() { window.removeEventListener("pointermove", move); mode = null; renderAll(); commit(); }
    el.addEventListener("pointerdown", function (e) { down(e, "move"); });
    hl.addEventListener("pointerdown", function (e) { down(e, "l"); });
    hr.addEventListener("pointerdown", function (e) { down(e, "r"); });
  }
  function select(id) {
    S.edit.ui = S.edit.ui || {}; S.edit.ui.selected = id;
    if (S.lanes) S.lanes.querySelectorAll(".r2r-clip").forEach(function (n) {
      n.classList.toggle("sel", n.dataset.id === id);
    });
  }
  function wireRuler() {
    function scrub(e) {
      var rect = S.ruler.getBoundingClientRect();
      setPlayhead(Math.max(0, px2sec(e.clientX - rect.left))); commit();
    }
    S.ruler.addEventListener("pointerdown", function (e) {
      stop(); scrub(e);
      window.addEventListener("pointermove", scrub);
      window.addEventListener("pointerup", function () { window.removeEventListener("pointermove", scrub); }, { once: true });
    });
  }

  // ---- toolbar + keyboard ---------------------------------------------------
  function fit() {
    var sc = S.root.querySelector(".r2r-scroll");
    var avail = (sc ? sc.clientWidth : 900) - 130;
    var dur = totalDur() || 10;
    S.pxPerSec = Math.max(10, Math.min(400, avail / dur));
    var z = S.root.querySelector(".r2r-zoom"); if (z) z.value = Math.round(S.pxPerSec);
    renderAll(); commit();
  }
  function clickGr(id) {
    var b = document.querySelector("#" + id + " button") || document.querySelector("#" + id);
    if (b) b.click();
  }
  function syncSnapBox() { var s = S.root && S.root.querySelector(".r2r-snap"); if (s) s.checked = S.snap; }
  function typingInField() {
    var a = document.activeElement;
    return a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable);
  }
  function timelineVisible() {
    var r = document.getElementById(ROOT_ID);
    return r && r.offsetParent !== null;
  }
  function onKey(e) {
    if (!timelineVisible() || typingInField()) return;
    var k = e.key.toLowerCase();
    if (k === " " || e.code === "Space") { e.preventDefault(); togglePlay(); }
    else if (k === "s") { e.preventDefault(); clickGr("r2r-split"); }
    else if (k === "delete" || k === "backspace") { e.preventDefault(); clickGr("r2r-ripple"); }
    else if (k === "f") { e.preventDefault(); fit(); }
    else if ((e.ctrlKey || e.metaKey) && k === "z" && !e.shiftKey) { e.preventDefault(); clickGr("r2r-undo"); }
    else if ((e.ctrlKey || e.metaKey) && (k === "y" || (k === "z" && e.shiftKey))) { e.preventDefault(); clickGr("r2r-redo"); }
  }

  // ---- mount ----------------------------------------------------------------
  function buildSkeleton(root) {
    root.innerHTML = "";
    var wrap = document.createElement("div"); wrap.className = "r2r-tl";
    wrap.innerHTML =
      '<div class="r2r-toolbar">' +
      '  <video class="r2r-preview" controls playsinline></video>' +
      '  <div class="r2r-tools">' +
      '    <div class="r2r-transport">' +
      '      <button class="r2r-play" title="Play / pause (Space)">► / ❚❚</button>' +
      '      <span class="r2r-readout">0:00.00 / 0:00.00</span>' +
      '    </div>' +
      '    <label>Zoom <input type="range" class="r2r-zoom" min="10" max="400" value="80"></label>' +
      '    <div class="r2r-tools-row">' +
      '      <button class="r2r-fit" title="Zoom to fit (F)">Fit</button>' +
      '      <label class="r2r-snaplbl"><input type="checkbox" class="r2r-snap" checked> Snap</label>' +
      '    </div>' +
      '    <small class="r2r-hint">Drag = move · edge = trim · ruler = scrub · S split · Del ripple · ⌘Z undo</small>' +
      '  </div>' +
      '</div>' +
      '<div class="r2r-scroll"><div class="r2r-ruler"></div><div class="r2r-lanes"></div>' +
      '<div class="r2r-playhead"></div></div>';
    root.appendChild(wrap);
    S.root = wrap;
    S.lanes = wrap.querySelector(".r2r-lanes");
    S.ruler = wrap.querySelector(".r2r-ruler");
    S.playhead = wrap.querySelector(".r2r-playhead");
    S.video = wrap.querySelector(".r2r-preview");
    S.readout = wrap.querySelector(".r2r-readout");
    wireRuler();
    wrap.querySelector(".r2r-zoom").addEventListener("input", function (e) {
      S.pxPerSec = parseInt(e.target.value, 10) || 80; renderAll(); commit();
    });
    wrap.querySelector(".r2r-play").addEventListener("click", togglePlay);
    wrap.querySelector(".r2r-fit").addEventListener("click", fit);
    wrap.querySelector(".r2r-snap").addEventListener("change", function (e) { S.snap = e.target.checked; commit(); });
    S.mounted = true; renderAll(); syncSnapBox();
  }
  function tryMount() {
    var root = document.getElementById(ROOT_ID);
    if (root && (!root.querySelector(".r2r-tl") || !S.mounted)) buildSkeleton(root);
  }
  function boot() {
    tryMount();
    var tries = 0;
    (function poll() { if (S.mounted || tries++ > 80) return;
      requestAnimationFrame(function () { tryMount(); setTimeout(poll, 100); }); })();
    try {
      new MutationObserver(function () {
        var root = document.getElementById(ROOT_ID);
        if (root && !root.querySelector(".r2r-tl")) { S.mounted = false; tryMount(); }
      }).observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
    window.addEventListener("keydown", onKey, true);
  }

  window.R2RTimeline = { applyOp: applyOp, remount: tryMount, _state: S };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
