/* Reel2Reel — hand-rolled, no-build, vanilla-JS multi-track timeline + the
 * Gradio<->browser state bridge.
 *
 * Delivered through WAN2GPPlugin.add_custom_js(), which Wan2GP splices into the
 * single gr.Blocks(js=...) init function that runs once on app load. We therefore
 * wrap everything in a guarded IIFE, publish window.R2RTimeline, and (re)mount
 * against our own elem_ids once Gradio has rendered them (retry on rAF + a
 * MutationObserver — never touch Gradio internal classes).
 *
 * STATE BRIDGE (two hidden gr.Textbox pipes):
 *   outbound (browser -> Python): write the edit-state JSON into
 *     #r2r_tl_to_py textarea via the native value setter + a bubbling 'input'
 *     event (a plain .value= is swallowed by Gradio's controlled Svelte input),
 *     debounced/committed on pointerup.
 *   inbound  (Python -> browser): Python returns a {seq, op, edit} envelope as
 *     #r2r_tl_from_py's value; its .change(fn=None, js=...) hook calls
 *     R2RTimeline.applyOp(payload). A monotonic seq dedupes re-sends.
 */
(function () {
  if (window.R2RTimeline) return;

  var ROOT_ID = "r2r_timeline_root";
  var TO_PY = "r2r_tl_to_py";
  var FROM_PY = "r2r_tl_from_py";

  var S = {
    edit: { name: "Cut 1", fps: 24, tracks: [], clips: [],
            ui: { px_per_sec: 80, playhead: 0, selected: null } },
    pxPerSec: 80,
    lastSeqIn: -1,
    mounted: false,
    root: null, lanes: null, ruler: null, playhead: null, video: null,
    pushTimer: null,
  };

  // ---- Gradio bridge helpers ------------------------------------------------
  function setNativeValue(el, value) {
    var proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function pushNow() {
    var ta = document.querySelector("#" + TO_PY + " textarea, #" + TO_PY + " input");
    if (!ta) return;
    try { setNativeValue(ta, JSON.stringify(S.edit)); } catch (e) { console.error("[R2R] push", e); }
  }

  function commit() {
    if (S.pushTimer) clearTimeout(S.pushTimer);
    S.pushTimer = setTimeout(pushNow, 120);
  }

  // ---- inbound op envelope --------------------------------------------------
  function applyOp(payload) {
    if (!payload) return;
    var msg;
    try { msg = typeof payload === "string" ? JSON.parse(payload) : payload; }
    catch (e) { console.error("[R2R] bad inbound", e); return; }
    if (typeof msg.seq === "number" && msg.seq <= S.lastSeqIn) return;
    if (typeof msg.seq === "number") S.lastSeqIn = msg.seq;
    if (msg.op === "load" && msg.edit) {
      S.edit = msg.edit;
      S.pxPerSec = (msg.edit.ui && msg.edit.ui.px_per_sec) || S.pxPerSec;
      renderAll();
    }
  }

  // ---- geometry -------------------------------------------------------------
  function sec2px(s) { return s * S.pxPerSec; }
  function px2sec(p) { return p / S.pxPerSec; }
  function snap(s) {
    var grid = 0.5;                 // snap to half-second + to clip edges
    var best = Math.round(s / grid) * grid, bestD = Math.abs(best - s);
    (S.edit.clips || []).forEach(function (c) {
      [c.start, c.start + c.dur].forEach(function (e) {
        if (Math.abs(e - s) < bestD && Math.abs(e - s) < 0.25) { best = e; bestD = Math.abs(e - s); }
      });
    });
    return Math.max(0, best);
  }
  function totalDur() {
    return (S.edit.clips || []).reduce(function (m, c) {
      return Math.max(m, (c.start || 0) + (c.dur || 0)); }, 0);
  }

  // ---- rendering ------------------------------------------------------------
  function renderAll() {
    if (!S.mounted) return;
    var tracks = S.edit.tracks || [];
    S.lanes.innerHTML = "";
    tracks.forEach(function (t) {
      var row = document.createElement("div");
      row.className = "r2r-track r2r-" + (t.kind || "Video").toLowerCase();
      var head = document.createElement("div");
      head.className = "r2r-head";
      head.textContent = t.name || t.id;
      var lane = document.createElement("div");
      lane.className = "r2r-lane";
      lane.dataset.track = t.id;
      (S.edit.clips || []).filter(function (c) { return c.track === t.id; })
        .forEach(function (c) { lane.appendChild(renderClip(c, t)); });
      row.appendChild(head);
      row.appendChild(lane);
      S.lanes.appendChild(row);
    });
    var w = Math.max(600, sec2px(totalDur()) + 200);
    S.ruler.style.width = w + "px";
    drawRuler();
    placePlayhead();
    driveVideo();
  }

  function renderClip(c, t) {
    var el = document.createElement("div");
    el.className = "r2r-clip" + (S.edit.ui && S.edit.ui.selected === c.id ? " sel" : "");
    el.dataset.id = c.id;
    el.style.transform = "translateX(" + sec2px(c.start) + "px)";
    el.style.width = Math.max(8, sec2px(c.dur)) + "px";
    if (c.thumb_url && (t.kind || "Video") === "Video")
      el.style.backgroundImage = "url('" + c.thumb_url + "')";
    var lbl = document.createElement("span");
    lbl.className = "r2r-label";
    lbl.textContent = c.label || c.id;
    el.appendChild(lbl);
    var hl = document.createElement("div"); hl.className = "r2r-handle l";
    var hr = document.createElement("div"); hr.className = "r2r-handle r";
    el.appendChild(hl); el.appendChild(hr);
    wireClip(el, c, hl, hr);
    return el;
  }

  function drawRuler() {
    S.ruler.innerHTML = "";
    var dur = Math.ceil(totalDur()) + 4;
    var step = S.pxPerSec < 40 ? 5 : (S.pxPerSec < 90 ? 2 : 1);
    for (var s = 0; s <= dur; s += step) {
      var tick = document.createElement("div");
      tick.className = "r2r-tick";
      tick.style.left = sec2px(s) + "px";
      tick.textContent = s + "s";
      S.ruler.appendChild(tick);
    }
  }

  function placePlayhead() {
    var ph = (S.edit.ui && S.edit.ui.playhead) || 0;
    S.playhead.style.transform = "translateX(" + sec2px(ph) + "px)";
  }

  // ---- preview --------------------------------------------------------------
  function driveVideo() {
    if (!S.video) return;
    var ph = (S.edit.ui && S.edit.ui.playhead) || 0;
    // topmost video clip covering the playhead
    var hit = null;
    (S.edit.clips || []).forEach(function (c) {
      if (c.kind === "Video" && c.url && ph >= c.start && ph < c.start + c.dur) hit = c;
    });
    if (!hit) return;
    var want = hit.url;
    if (S.video.getAttribute("data-src") !== want) {
      S.video.setAttribute("data-src", want);
      S.video.src = want;
    }
    try { S.video.currentTime = (hit.in || 0) + (ph - hit.start); } catch (e) {}
  }

  // ---- interaction ----------------------------------------------------------
  function wireClip(el, c, hl, hr) {
    var mode = null, x0 = 0, start0 = 0, in0 = 0, out0 = 0;
    function down(e, m) {
      e.preventDefault(); e.stopPropagation();
      mode = m; x0 = e.clientX; start0 = c.start; in0 = c.in; out0 = c.out;
      select(c.id);
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up, { once: true });
    }
    function move(e) {
      var ds = px2sec(e.clientX - x0);
      if (mode === "move") {
        c.start = snap(Math.max(0, start0 + ds));
        el.style.transform = "translateX(" + sec2px(c.start) + "px)";
      } else if (mode === "l") {                 // trim in-point (moves start too)
        var ni = Math.min(out0 - 1 / (S.edit.fps || 24), Math.max(0, in0 + ds));
        c.in = ni; c.start = Math.max(0, start0 + (ni - in0)); c.dur = c.out - c.in;
        el.style.transform = "translateX(" + sec2px(c.start) + "px)";
        el.style.width = Math.max(8, sec2px(c.dur)) + "px";
      } else if (mode === "r") {                 // trim out-point
        c.out = Math.max(c.in + 1 / (S.edit.fps || 24), out0 + ds);
        c.dur = c.out - c.in;
        el.style.width = Math.max(8, sec2px(c.dur)) + "px";
      }
    }
    function up() {
      window.removeEventListener("pointermove", move);
      mode = null; renderAll(); commit();
    }
    el.addEventListener("pointerdown", function (e) { down(e, "move"); });
    hl.addEventListener("pointerdown", function (e) { down(e, "l"); });
    hr.addEventListener("pointerdown", function (e) { down(e, "r"); });
  }

  function select(id) {
    S.edit.ui = S.edit.ui || {};
    S.edit.ui.selected = id;
    if (S.lanes) S.lanes.querySelectorAll(".r2r-clip").forEach(function (n) {
      n.classList.toggle("sel", n.dataset.id === id);
    });
  }

  function wireRuler() {
    function scrub(e) {
      var rect = S.ruler.getBoundingClientRect();
      var s = Math.max(0, px2sec(e.clientX - rect.left));
      S.edit.ui = S.edit.ui || {}; S.edit.ui.playhead = s;
      placePlayhead(); driveVideo(); commit();
    }
    S.ruler.addEventListener("pointerdown", function (e) {
      scrub(e);
      window.addEventListener("pointermove", scrub);
      window.addEventListener("pointerup", function () {
        window.removeEventListener("pointermove", scrub);
      }, { once: true });
    });
  }

  function wireZoom(input) {
    input.addEventListener("input", function () {
      S.pxPerSec = parseInt(input.value, 10) || 80;
      S.edit.ui = S.edit.ui || {}; S.edit.ui.px_per_sec = S.pxPerSec;
      renderAll(); commit();
    });
  }

  // ---- mount ----------------------------------------------------------------
  function buildSkeleton(root) {
    root.innerHTML = "";
    var wrap = document.createElement("div"); wrap.className = "r2r-tl";
    wrap.innerHTML =
      '<div class="r2r-toolbar">' +
      '  <video class="r2r-preview" controls playsinline></video>' +
      '  <div class="r2r-tools">' +
      '    <label>Zoom <input type="range" class="r2r-zoom" min="20" max="240" value="80"></label>' +
      '  </div>' +
      '</div>' +
      '<div class="r2r-scroll">' +
      '  <div class="r2r-ruler"></div>' +
      '  <div class="r2r-lanes"></div>' +
      '  <div class="r2r-playhead"></div>' +
      '</div>';
    root.appendChild(wrap);
    S.root = wrap;
    S.lanes = wrap.querySelector(".r2r-lanes");
    S.ruler = wrap.querySelector(".r2r-ruler");
    S.playhead = wrap.querySelector(".r2r-playhead");
    S.video = wrap.querySelector(".r2r-preview");
    wireRuler();
    wireZoom(wrap.querySelector(".r2r-zoom"));
    S.mounted = true;
    renderAll();
  }

  function tryMount() {
    var root = document.getElementById(ROOT_ID);
    if (root && !root.querySelector(".r2r-tl")) buildSkeleton(root);
    else if (root && !S.mounted) buildSkeleton(root);
  }

  function boot() {
    tryMount();
    // Gradio mounts late and may re-render; keep trying + watch the DOM.
    var tries = 0;
    (function poll() {
      if (S.mounted || tries++ > 80) return;
      requestAnimationFrame(function () { tryMount(); setTimeout(poll, 100); });
    })();
    try {
      new MutationObserver(function () {
        var root = document.getElementById(ROOT_ID);
        if (root && !root.querySelector(".r2r-tl")) { S.mounted = false; tryMount(); }
      }).observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
  }

  window.R2RTimeline = { applyOp: applyOp, remount: tryMount, _state: S };
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
