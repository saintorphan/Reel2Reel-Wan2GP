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
    interacting: false, pendingLoad: null, razor: false, ctxSeq: 0,
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
    // Don't clobber an in-flight drag/scrub; queue and apply on pointerup.
    if (S.interacting && msg.op === "load") { S.pendingLoad = msg; return; }
    _apply(msg);
  }
  function _apply(msg) {
    if (typeof msg.seq === "number") S.lastSeqIn = msg.seq;
    if (msg.op === "load" && msg.edit) {
      S.edit = msg.edit;
      var ui = msg.edit.ui || {};
      if (ui.px_per_sec) S.pxPerSec = ui.px_per_sec;
      if (typeof ui.snap === "boolean") S.snap = ui.snap;
      renderAll(); syncSnapBox();
    }
  }
  function endInteract() {
    S.interacting = false;
    if (S.pendingLoad) { var m = S.pendingLoad; S.pendingLoad = null; _apply(m); }
  }

  // ---- geometry -------------------------------------------------------------
  function sec2px(s) { return s * S.pxPerSec; }
  function px2sec(p) { return p / S.pxPerSec; }
  function ph() { return (S.edit.ui && S.edit.ui.playhead) || 0; }
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
      var collapsed = (t.height === 1);
      var h = collapsed ? 16 : (t.height > 1 ? t.height : 52);
      var row = document.createElement("div");
      row.className = "r2r-track r2r-" + (t.kind || "Video").toLowerCase();
      row.style.height = h + "px";
      var head = document.createElement("div");
      head.className = "r2r-head"; head.style.height = h + "px";
      var flags = (t.muted ? " 🔇" : "") + (t.solo ? " ◎" : "") + (t.locked ? " 🔒" : "");
      var btn = document.createElement("button");
      btn.className = "r2r-collapse"; btn.textContent = collapsed ? "▸" : "▾";
      btn.title = "collapse / expand track";
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation(); t.height = collapsed ? 0 : 1; renderAll(); commit();
      });
      var nm = document.createElement("span"); nm.textContent = (t.name || t.id);
      var sm = document.createElement("small"); sm.textContent = t.kind + flags;
      head.appendChild(btn); head.appendChild(nm); head.appendChild(sm);
      var lane = document.createElement("div");
      lane.className = "r2r-lane"; lane.dataset.track = t.id; lane.style.height = h + "px";
      if (!collapsed) {
        clips().filter(function (c) { return c.track === t.id; })
          .forEach(function (c) { lane.appendChild(renderClip(c, t)); });
        renderTransitions(lane, t);
      }
      row.appendChild(head); row.appendChild(lane); S.lanes.appendChild(row);
    });
    var w = Math.max(600, sec2px(totalDur()) + 200);
    S.ruler.style.width = w + "px";
    drawRuler(); renderMarkers(); placePlayhead(); driveVideo(); updateReadout(); syncSeq();
  }
  function renderMarkers() {
    if (!S.ruler) return;
    (S.edit.markers || []).forEach(function (m) {
      var el = document.createElement("div");
      el.className = "r2r-marker";
      el.style.left = sec2px(m.t) + "px";
      el.style.borderTopColor = m.color || "#e0a106";
      el.title = m.label || ("marker @ " + (m.t || 0).toFixed(2) + "s");
      S.ruler.appendChild(el);
    });
  }
  function laneAt(clientY) {
    var lanes = S.lanes ? S.lanes.querySelectorAll(".r2r-lane") : [];
    for (var i = 0; i < lanes.length; i++) {
      var r = lanes[i].getBoundingClientRect();
      if (clientY >= r.top && clientY <= r.bottom) return lanes[i].dataset.track;
    }
    return null;
  }

  function renderClip(c, t) {
    var el = document.createElement("div");
    // r2r-timeline-clip + data-media-src are the shared SaintorphanMenu convention:
    // other saintorphan plugins (Replicant/ImageSuite) register their items against
    // this surface and read the clip's frame/image from data-media-src.
    el.className = "r2r-clip r2r-timeline-clip"
      + (selection().indexOf(c.id) >= 0 ? " sel" : "")
      + (c.mute ? " muted" : "")
      + (c.type === "text" ? " r2r-text" : "");
    el.dataset.id = c.id;
    el.setAttribute("data-media-src", c.thumb_url || c.url || "");
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
    var ct = (hit.in || 0) + ((ph - hit.start) * (hit.speed || 1));
    if (S.video.readyState >= 1) { try { S.video.currentTime = ct; } catch (e) {} }
    else {
      S.video.addEventListener("loadedmetadata", function onm() {
        try { S.video.currentTime = ct; } catch (e) {}
        S.video.removeEventListener("loadedmetadata", onm);
      }, { once: true });
    }
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
  function trackById(id) {
    return (S.edit.tracks || []).filter(function (t) { return t.id === id; })[0] || null;
  }
  function wireClip(el, c, hl, hr) {
    var mode = null, x0 = 0, start0 = 0, in0 = 0, out0 = 0, moved = false, dropTrack = null;
    var group = null;   // {id: start0} when moving a multi-selection
    function down(e, m) {
      e.preventDefault(); e.stopPropagation();
      if (S.razor && m === "move") {            // razor tool: click a clip to cut it there
        var lr = el.parentNode.getBoundingClientRect();
        var t = Math.max(0, px2sec(e.clientX - lr.left));
        setPlayhead(t);
        relayCtx("razor|" + c.id + "|" + t.toFixed(3));
        return;
      }
      mode = m; x0 = e.clientX; start0 = c.start; in0 = c.in; out0 = c.out; moved = false;
      S.interacting = true;
      // modifier click, or clicking a clip not already in the selection -> (re)select;
      // a plain click on an already-selected clip keeps the group for dragging together.
      if ((e.ctrlKey || e.metaKey || e.shiftKey) || selection().indexOf(c.id) < 0) select(c.id, e);
      group = null;
      var sel = selection();
      if (m === "move" && sel.length > 1 && sel.indexOf(c.id) >= 0) {
        group = {};
        sel.forEach(function (id) { var cc = clipById(id); if (cc) group[id] = cc.start; });
      }
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up, { once: true });
    }
    function move(e) {
      var ds = px2sec(e.clientX - x0); if (Math.abs(e.clientX - x0) > 2) moved = true;
      if (mode === "move") {
        var ns = snapVal(Math.max(0, start0 + ds));
        var delta = ns - start0;
        if (group) {
          Object.keys(group).forEach(function (id) {
            var cc = clipById(id); if (cc) cc.start = Math.max(0, group[id] + delta);
          });
          renderAll();
        } else {
          c.start = ns;
          el.style.transform = "translateX(" + sec2px(c.start) + "px)";
          var lane = laneAt(e.clientY); if (lane) dropTrack = lane;
        }
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
    function up() {
      window.removeEventListener("pointermove", move);
      if (mode === "move" && dropTrack && dropTrack !== c.track) {
        var tt = trackById(dropTrack);     // only move between same-kind lanes
        if (tt && tt.kind === c.kind) c.track = dropTrack;
      }
      mode = null; dropTrack = null; renderAll(); commit(); endInteract();
    }
    el.addEventListener("pointerdown", function (e) { down(e, "move"); });
    hl.addEventListener("pointerdown", function (e) { down(e, "l"); });
    hr.addEventListener("pointerdown", function (e) { down(e, "r"); });
  }
  function clipById(id) {
    return clips().filter(function (c) { return c.id === id; })[0] || null;
  }
  function selection() {
    var ui = S.edit.ui || {};
    return ui.selection && ui.selection.length ? ui.selection
      : (ui.selected ? [ui.selected] : []);
  }
  function relayCtx(v) {
    var b = document.querySelector("#reel2reel-ctx-relay textarea, #reel2reel-ctx-relay input");
    if (!b) return;
    S.ctxSeq = (S.ctxSeq || 0) + 1;        // monotonic so repeats still fire .change
    setNativeValue(b, v + "|" + S.ctxSeq);
  }
  function rangeSelect(aId, bId) {
    var ordered = clips().slice().sort(function (x, y) { return x.start - y.start; });
    var ia = ordered.findIndex(function (c) { return c.id === aId; });
    var ib = ordered.findIndex(function (c) { return c.id === bId; });
    if (ia < 0 || ib < 0) return [bId];
    var lo = Math.min(ia, ib), hi = Math.max(ia, ib);
    return ordered.slice(lo, hi + 1).map(function (c) { return c.id; });
  }
  function highlight() {
    var sel = selection();
    if (S.lanes) S.lanes.querySelectorAll(".r2r-clip").forEach(function (n) {
      n.classList.toggle("sel", sel.indexOf(n.dataset.id) >= 0);
    });
  }
  function select(id, e) {
    S.edit.ui = S.edit.ui || {};
    var sel = (S.edit.ui.selection || []).slice();
    if (e && (e.ctrlKey || e.metaKey)) {
      var i = sel.indexOf(id);
      if (i >= 0) sel.splice(i, 1); else sel.push(id);
    } else if (e && e.shiftKey && S.edit.ui.selected) {
      sel = rangeSelect(S.edit.ui.selected, id);
    } else {
      sel = [id];
    }
    S.edit.ui.selected = id;
    S.edit.ui.selection = sel;
    highlight();
  }
  function wireRuler() {
    function scrub(e) {
      var rect = S.ruler.getBoundingClientRect();
      setPlayhead(Math.max(0, px2sec(e.clientX - rect.left))); commit();
    }
    S.ruler.addEventListener("pointerdown", function (e) {
      stop(); S.interacting = true; scrub(e);
      window.addEventListener("pointermove", scrub);
      window.addEventListener("pointerup", function () {
        window.removeEventListener("pointermove", scrub); endInteract();
      }, { once: true });
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
  function zoomToSelection() {
    var sel = selection();
    if (!sel.length) return fit();
    var lo = 1e9, hi = 0;
    sel.forEach(function (id) {
      var c = clipById(id);
      if (c) { lo = Math.min(lo, c.start); hi = Math.max(hi, c.start + c.dur); }
    });
    if (hi <= lo) return;
    var sc = S.root.querySelector(".r2r-scroll");
    var avail = (sc ? sc.clientWidth : 900) - 150;
    S.pxPerSec = Math.max(10, Math.min(400, avail / (hi - lo)));
    var z = S.root.querySelector(".r2r-zoom"); if (z) z.value = Math.round(S.pxPerSec);
    renderAll(); commit();
    if (sc) sc.scrollLeft = Math.max(0, sec2px(lo) - 20);
  }
  function toggleRazor() {
    S.razor = !S.razor;
    if (S.root) S.root.classList.toggle("r2r-razor-on", S.razor);
    var b = S.root && S.root.querySelector(".r2r-razor");
    if (b) b.classList.toggle("active", S.razor);
  }
  function toggleHelp() {
    var ex = document.getElementById("r2r-help-modal");
    if (ex) { ex.remove(); return; }
    var m = document.createElement("div");
    m.id = "r2r-help-modal"; m.className = "r2r-help-modal";
    m.innerHTML = "<div class='r2r-help-card'><h3>Reel2Reel — shortcuts</h3><ul>"
      + "<li><b>Space</b> play/pause · <b>L / K / J</b> play / stop / jump back · <b>← →</b> step a frame</li>"
      + "<li><b>S</b> split at playhead · <b>Del</b> delete (ripple) the selection</li>"
      + "<li><b>Click</b> select · <b>Ctrl/⌘-click</b> add/remove · <b>Shift-click</b> range</li>"
      + "<li><b>Ctrl/⌘ C / V / X</b> copy / paste (at playhead) / cut · drag = move (whole selection if multi)</li>"
      + "<li>Drag a clip <b>edge</b> to trim · drag to another lane to change track</li>"
      + "<li><b>F</b> fit · <b>Shift-Z</b> zoom to selection · <b>Ctrl/⌘-Z</b> undo · <b>Ctrl/⌘-Shift-Z</b> redo</li>"
      + "<li>Click the time readout to jump · <b>right-click a clip</b> for actions + the saintorphan menu</li>"
      + "</ul><button class='r2r-help-close'>Close</button></div>";
    m.addEventListener("click", function (e) {
      if (e.target === m || (e.target.className || "").indexOf("r2r-help-close") >= 0) m.remove();
    });
    document.body.appendChild(m);
  }
  function clickGr(id) {
    var b = document.querySelector("#" + id + " button") || document.querySelector("#" + id);
    if (b) b.click();
  }
  function syncSnapBox() { var s = S.root && S.root.querySelector(".r2r-snap"); if (s) s.checked = S.snap; }
  function syncSeq() {
    if (!S.root) return;
    var f = S.root.querySelector(".r2r-fps");
    if (f && document.activeElement !== f) f.value = Math.round(S.edit.fps || 30);
    var r = S.root.querySelector(".r2r-res");
    if (r && document.activeElement !== r) r.value = (S.edit.width || 1280) + "x" + (S.edit.height || 720);
  }
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
    else if (k === "delete" || k === "backspace") {
      e.preventDefault();
      var ds = selection();
      if (ds.length > 1) relayCtx("delsel|" + ds.join(",")); else clickGr("r2r-ripple");
    }
    else if ((e.ctrlKey || e.metaKey) && k === "c") {
      e.preventDefault(); var s = selection(); if (s.length) relayCtx("copy|" + s.join(","));
    }
    else if ((e.ctrlKey || e.metaKey) && k === "v") { e.preventDefault(); relayCtx("paste"); }
    else if ((e.ctrlKey || e.metaKey) && k === "x") {
      e.preventDefault(); var sx = selection(); if (sx.length) relayCtx("cut|" + sx.join(","));
    }
    else if (k === "z" && e.shiftKey && !(e.ctrlKey || e.metaKey)) { e.preventDefault(); zoomToSelection(); }
    else if (k === "f") { e.preventDefault(); fit(); }
    else if (k === "r" && !(e.ctrlKey || e.metaKey)) { e.preventDefault(); toggleRazor(); }
    else if (k === "l") { e.preventDefault(); play(); }
    else if (k === "k") { e.preventDefault(); stop(); commit(); }
    else if (k === "j") { e.preventDefault(); stop(); setPlayhead(ph() - 1); commit(); }
    else if (k === "arrowleft") { e.preventDefault(); setPlayhead(ph() - 1 / (S.edit.fps || 30)); commit(); }
    else if (k === "arrowright") { e.preventDefault(); setPlayhead(ph() + 1 / (S.edit.fps || 30)); commit(); }
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
      '      <button class="r2r-help" title="Keyboard shortcuts">?</button>' +
      '    </div>' +
      '    <label>Zoom <input type="range" class="r2r-zoom" min="10" max="400" value="80"></label>' +
      '    <div class="r2r-tools-row">' +
      '      <button class="r2r-fit" title="Zoom to fit (F)">Fit</button>' +
      '      <button class="r2r-razor" title="Razor (R): click a clip to cut it">✂ Razor</button>' +
      '      <label class="r2r-snaplbl"><input type="checkbox" class="r2r-snap" checked> Snap</label>' +
      '    </div>' +
      '    <div class="r2r-tools-row r2r-seq">' +
      '      <label>FPS <input type="number" class="r2r-fps" min="1" max="120" step="1"></label>' +
      '      <label>Size <input class="r2r-res" size="9" placeholder="1280x720"></label>' +
      '      <button class="r2r-matchfps" title="Set the timeline fps to the highest source-clip fps">Match fps</button>' +
      '    </div>' +
      '    <small class="r2r-hint">Drag = move · edge = trim · ruler = scrub · S split · Del ripple · ⌘Z undo. Clips conform to the timeline FPS/size on export.</small>' +
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
    wrap.querySelector(".r2r-razor").addEventListener("click", toggleRazor);
    wrap.querySelector(".r2r-snap").addEventListener("change", function (e) { S.snap = e.target.checked; commit(); });
    wrap.querySelector(".r2r-fps").addEventListener("change", function (e) {
      S.edit.fps = Math.max(1, parseInt(e.target.value, 10) || 30); renderAll(); commit();
    });
    wrap.querySelector(".r2r-res").addEventListener("change", function (e) {
      var m = /(\d+)\s*[x×]\s*(\d+)/.exec(e.target.value || "");
      if (m) { S.edit.width = +m[1]; S.edit.height = +m[2]; commit(); }
      syncSeq();
    });
    wrap.querySelector(".r2r-matchfps").addEventListener("click", function () {
      var mx = 0;
      (S.edit.clips || []).forEach(function (c) { if (c.src_fps) mx = Math.max(mx, Math.round(c.src_fps)); });
      if (mx > 0) { S.edit.fps = mx; syncSeq(); renderAll(); commit(); }
    });
    if (S.readout) {
      S.readout.style.cursor = "pointer";
      S.readout.title = "click to jump to a time";
      S.readout.addEventListener("click", function () {
        var v = window.prompt("Jump to (seconds or M:SS):", ph().toFixed(2));
        if (v == null) return;
        v = v.trim();
        var t = v.indexOf(":") >= 0
          ? (parseFloat(v.split(":")[0]) || 0) * 60 + (parseFloat(v.split(":")[1]) || 0)
          : parseFloat(v);
        if (!isNaN(t)) { stop(); setPlayhead(Math.max(0, t)); commit(); }
      });
    }
    var help = wrap.querySelector(".r2r-help");
    if (help) help.addEventListener("click", toggleHelp);
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
