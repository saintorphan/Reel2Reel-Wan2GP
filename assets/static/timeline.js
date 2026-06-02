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
      var isAudio = (t.kind || "Video") === "Audio";
      var btn = document.createElement("button");
      btn.className = "r2r-collapse"; btn.textContent = collapsed ? "▸" : "▾";
      btn.title = "Collapse / expand track";
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation(); t.height = collapsed ? 0 : 1; renderAll(); commit();
      });
      var nm = document.createElement("span");
      nm.className = "r2r-trk-name"; nm.dataset.trk = t.id;
      nm.textContent = (t.name || t.id); nm.title = "Double-click to rename · right-click for more";
      nm.addEventListener("dblclick", function (ev) { ev.stopPropagation(); renameTrack(t); });
      var ctrl = document.createElement("div"); ctrl.className = "r2r-trk-ctrl";
      var sm = document.createElement("small"); sm.textContent = t.kind; ctrl.appendChild(sm);
      [["muted", "M", "Mute"], ["solo", "S", "Solo"], ["locked", "L", "Lock"]].forEach(function (f) {
        var b = document.createElement("button");
        b.className = "r2r-trk-flag" + (t[f[0]] ? " active" : "");
        b.textContent = f[1]; b.title = f[2];
        b.addEventListener("click", function (ev) {
          ev.stopPropagation(); t[f[0]] = !t[f[0]]; renderAll(); commit();
        });
        ctrl.appendChild(b);
      });
      head.appendChild(btn); head.appendChild(nm); head.appendChild(ctrl);
      if (!collapsed && isAudio) {                        // audio: inline volume on the head
        var vol = document.createElement("input");
        vol.type = "range"; vol.className = "r2r-trk-vol";
        vol.min = -40; vol.max = 12; vol.step = 0.5;
        vol.value = (t.volume_db != null ? t.volume_db : 0);
        vol.title = "Track volume " + (t.volume_db || 0) + " dB";
        vol.addEventListener("pointerdown", function (ev) { ev.stopPropagation(); });
        vol.addEventListener("input", function () {
          t.volume_db = parseFloat(vol.value); vol.title = "Track volume " + vol.value + " dB";
        });
        vol.addEventListener("change", function () { t.volume_db = parseFloat(vol.value); commit(); });
        head.appendChild(vol);
      }
      if (!collapsed) {
        var rsz = document.createElement("div");
        rsz.className = "r2r-trk-resize"; rsz.title = "Drag to resize track";
        wireTrackResize(rsz, t);
        head.appendChild(rsz);
      }
      head.addEventListener("contextmenu", function (ev) { openTrackMenu(ev, t); });
      var lane = document.createElement("div");
      lane.className = "r2r-lane"; lane.dataset.track = t.id; lane.style.height = h + "px";
      if (!collapsed) {
        clips().filter(function (c) { return c.track === t.id; })
          .forEach(function (c) { lane.appendChild(renderClip(c, t)); });
        renderTransitions(lane, t);
      }
      row.appendChild(head); row.appendChild(lane); S.lanes.appendChild(row);
    });
    if (!clips().length) {
      var hint = document.createElement("div");
      hint.className = "r2r-empty";
      hint.innerHTML = "Empty timeline — add clips from the <b>Library</b> panel on the "
        + "left (press <b>B</b> to toggle it), or right-click any output / clip → "
        + "<b>Reel2Reel Library</b>. Double-click a clip to inspect it · <b>?</b> for shortcuts.";
      S.lanes.appendChild(hint);
    }
    var w = Math.max(600, sec2px(totalDur()) + 200);
    S.ruler.style.width = w + "px";
    drawRuler(); renderMarkers(); renderRange(); placePlayhead(); driveVideo();
    updateReadout(); updateZoomVal(); syncSeq();
  }
  function renderMarkers() {
    if (!S.ruler) return;
    (S.edit.markers || []).forEach(function (m) {
      var el = document.createElement("div");
      el.className = "r2r-marker"; el.dataset.mid = m.id;
      el.style.left = sec2px(m.t) + "px";
      el.style.borderTopColor = m.color || "#e0a106";
      el.title = (m.label ? m.label + " · " : "") + "right-click to rename / delete";
      S.ruler.appendChild(el);
    });
  }
  function wireTrackResize(handle, track) {
    handle.addEventListener("pointerdown", function (e) {
      e.preventDefault(); e.stopPropagation();
      var h0 = (track.height > 1 ? track.height : 52), y0 = e.clientY;
      function mv(e2) { track.height = Math.max(28, Math.round(h0 + (e2.clientY - y0))); renderAll(); }
      window.addEventListener("pointermove", mv);
      window.addEventListener("pointerup", function () {
        window.removeEventListener("pointermove", mv); commit();
      }, { once: true });
    });
  }
  // ---- track ops (client-side; persist through the same commit() payload) ----
  function reindexTracks() { (S.edit.tracks || []).forEach(function (t, i) { t.index = i; }); }
  function deleteTrack(id) {                       // mirrors timeline.remove_track
    var ts = S.edit.tracks || [];
    if (ts.length <= 1) return;                     // keep at least one track
    var gone = {};
    S.edit.clips = (S.edit.clips || []).filter(function (c) {
      if (c.track === id) { gone[c.id] = 1; return false; } return true;
    });
    S.edit.tracks = ts.filter(function (t) { return t.id !== id; });
    S.edit.transitions = (S.edit.transitions || []).filter(function (x) {
      return x.track !== id && !(x.between && (gone[x.between[0]] || gone[x.between[1]]));
    });
    reindexTracks(); renderAll(); commit();
  }
  function moveTrack(id, delta) {                   // mirrors timeline.move_track
    var ts = S.edit.tracks || [], i = -1;
    for (var k = 0; k < ts.length; k++) if (ts[k].id === id) { i = k; break; }
    if (i < 0) return;
    var n = Math.max(0, Math.min(ts.length - 1, i + delta));
    if (n === i) return;
    ts.splice(n, 0, ts.splice(i, 1)[0]); reindexTracks(); renderAll(); commit();
  }
  function renameTrack(t) {                          // inline edit, in place on the head
    var span = S.lanes && S.lanes.querySelector('.r2r-trk-name[data-trk="' + t.id + '"]');
    if (!span) return;
    var inp = document.createElement("input");
    inp.className = "r2r-trk-rename"; inp.value = t.name || "";
    span.replaceWith(inp); inp.focus(); inp.select();
    var closed = false;
    function done(save) {
      if (closed) return; closed = true;
      if (save) { var v = inp.value.trim(); if (v) { t.name = v; commit(); } }
      renderAll();
    }
    inp.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); done(true); }
      else if (e.key === "Escape") { e.preventDefault(); done(false); }
    });
    inp.addEventListener("blur", function () { done(true); });
  }
  function closeTrackMenu() {
    var m = document.getElementById("r2r-trk-menu");
    if (!m) return;
    if (m._onDown) document.removeEventListener("pointerdown", m._onDown, true);
    m.remove();
  }
  function openTrackMenu(ev, t) {
    ev.preventDefault(); ev.stopPropagation(); closeTrackMenu();
    var ts = S.edit.tracks || [], i = ts.indexOf(t);
    var menu = document.createElement("div");
    menu.className = "r2r-trk-menu"; menu.id = "r2r-trk-menu";
    var items = [
      ["Rename", function () { renameTrack(t); }],
      ["Move up", function () { moveTrack(t.id, -1); }, i <= 0],
      ["Move down", function () { moveTrack(t.id, 1); }, i >= ts.length - 1],
      ["Delete track", function () { deleteTrack(t.id); }, ts.length <= 1],
      ["sep"],
      ["＋ Video track", function () { clickGr("r2r-addv"); }],
      ["＋ Audio track", function () { clickGr("r2r-adda"); }]
    ];
    items.forEach(function (it) {
      if (it[0] === "sep") {
        var s = document.createElement("div"); s.className = "r2r-trk-menu-sep";
        menu.appendChild(s); return;
      }
      var b = document.createElement("button"); b.textContent = it[0];
      if (it[2]) { b.disabled = true; }
      else { b.addEventListener("click", function () { closeTrackMenu(); it[1](); }); }
      menu.appendChild(b);
    });
    document.body.appendChild(menu);
    var mw = menu.offsetWidth, mh = menu.offsetHeight;
    menu.style.left = Math.min(ev.clientX, window.innerWidth - mw - 6) + "px";
    menu.style.top = Math.min(ev.clientY, window.innerHeight - mh - 6) + "px";
    // Close on any press OUTSIDE the menu — containment check so a press on a menu
    // item doesn't tear the menu down before its click lands.
    menu._onDown = function (e) { if (!menu.contains(e.target)) closeTrackMenu(); };
    setTimeout(function () {
      document.addEventListener("pointerdown", menu._onDown, true);
      window.addEventListener("blur", closeTrackMenu, { once: true });
    }, 0);
  }
  function laneAt(clientY) {
    var lanes = S.lanes ? S.lanes.querySelectorAll(".r2r-lane") : [];
    for (var i = 0; i < lanes.length; i++) {
      var r = lanes[i].getBoundingClientRect();
      if (clientY >= r.top && clientY <= r.bottom) return lanes[i].dataset.track;
    }
    return null;
  }

  function isGraded(c) {
    var k = c && c.color; if (!k) return false;
    return Math.abs(k.brightness || 0) > 1e-3
      || Math.abs((k.contrast == null ? 1 : k.contrast) - 1) > 1e-3
      || Math.abs((k.saturation == null ? 1 : k.saturation) - 1) > 1e-3
      || Math.abs((k.gamma == null ? 1 : k.gamma) - 1) > 1e-3
      || Math.abs(k.temp || 0) > 1e-3 || Math.abs(k.tint || 0) > 1e-3;
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
    el.setAttribute("role", "button");
    el.setAttribute("tabindex", "0");
    el.setAttribute("aria-label", (c.label || c.id) + " clip on " + (t.name || t.id));
    el.style.transform = "translateX(" + sec2px(c.start) + "px)";
    el.style.width = Math.max(8, sec2px(c.dur)) + "px";
    if (c.thumb_url) {
      el.style.backgroundImage = "url('" + c.thumb_url + "')";
      if ((t.kind || "Video") === "Video" && c.type !== "text") {   // filmstrip tiles
        el.style.backgroundSize = "auto 100%";
        el.style.backgroundRepeat = "repeat-x";
      }
    }
    if (c.opacity != null && c.opacity < 0.999) el.style.outlineOffset = "-2px";
    var lbl = document.createElement("span"); lbl.className = "r2r-label";
    lbl.textContent = (c.label || c.id) + (c.mute ? " 🔇" : "")
      + (c.opacity != null && c.opacity < 0.999 ? "  " + Math.round(c.opacity * 100) + "%" : "");
    el.appendChild(lbl);
    if (isGraded(c)) {                                  // visible "this clip is graded" badge
      var g = document.createElement("span");
      g.className = "r2r-graded"; g.textContent = "◐"; g.title = "Colour-graded clip";
      el.appendChild(g);
    }
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
  function isImg(u) { return /\.(png|jpe?g|gif|webp|bmp|avif)(\?|$)/i.test(u || ""); }
  function currentPreviewClip() {
    var ph = (S.edit.ui && S.edit.ui.playhead) || 0, hit = null;
    clips().forEach(function (c) {
      if (c.kind === "Video" && c.url && ph >= c.start && ph < c.start + c.dur) hit = c;
    });
    return hit;
  }
  function driveVideo() {
    if (!S.video) return;
    var ph = (S.edit.ui && S.edit.ui.playhead) || 0, hit = currentPreviewClip(), img = S.previewImg;
    if (!hit) { S.video.style.display = "block"; if (img) img.style.display = "none"; applyPreviewFilter(null); return; }
    if (isImg(hit.url)) {                          // still image: <video> can't show it
      if (img) {
        if (img.getAttribute("src") !== hit.url) img.src = hit.url;
        img.style.display = "block";
      }
      S.video.style.display = "none";
      try { S.video.pause(); } catch (e) {}
    } else {                                       // video: scrub the <video>
      if (img) img.style.display = "none";
      S.video.style.display = "block";
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
    applyPreviewFilter(hit);
  }
  // Live, approximate preview of the selected clip's grade/opacity via CSS filters
  // (the real composite uses ffmpeg eq). Reads the inspector sliders while they move.
  function readSliderVal(id) {
    var el = document.getElementById(id);
    var inp = el && el.querySelector('input[type="range"], input[type="number"]');
    var v = inp ? parseFloat(inp.value) : NaN;
    return isNaN(v) ? null : v;
  }
  function stageInsOpen() { var s = stageEl(); return s && !s.classList.contains("r2r-ins-collapsed"); }
  function applyPreviewFilter(clip) {
    var b = 0, c = 1, s = 1, op = 1, col = (clip && clip.color) || {};
    b = col.brightness || 0;
    c = (col.contrast == null ? 1 : col.contrast);
    s = (col.saturation == null ? 1 : col.saturation);
    if (clip && clip.opacity != null) op = clip.opacity;
    var sel = selection();                         // live override while editing THIS clip
    if (clip && sel.length === 1 && sel[0] === clip.id && stageInsOpen()) {
      var lb = readSliderVal("r2r-ins-bright"); if (lb != null) b = lb;
      var lc = readSliderVal("r2r-ins-contrast"); if (lc != null) c = lc;
      var ls = readSliderVal("r2r-ins-sat"); if (ls != null) s = ls;
      var lo = readSliderVal("r2r-ins-opacity"); if (lo != null) op = lo;
    }
    var f = "brightness(" + (1 + b).toFixed(3) + ") contrast(" + c.toFixed(3)
      + ") saturate(" + s.toFixed(3) + ")";
    [S.video, S.previewImg].forEach(function (el) { if (el) { el.style.filter = f; el.style.opacity = op; } });
  }

  // ---- transport ------------------------------------------------------------
  function setPlayhead(s) {
    S.edit.ui = S.edit.ui || {}; S.edit.ui.playhead = Math.max(0, s);
    placePlayhead(); driveVideo(); updateReadout();
  }
  function play() {
    if (S.playing) return; S.playing = true; S.lastT = 0; setPlayBtn("❚❚");
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
  function stop() { S.playing = false; if (S.rafId) cancelAnimationFrame(S.rafId); S.rafId = null; setPlayBtn("►"); }
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
      var trackChanged = false;
      if (mode === "move" && dropTrack && dropTrack !== c.track) {
        var tt = trackById(dropTrack);     // only move between same-kind lanes
        if (tt && tt.kind === c.kind) { c.track = dropTrack; trackChanged = true; }
      }
      var didEdit = moved || trackChanged;
      mode = null; dropTrack = null;
      // A pure click (no drag) must NOT renderAll — that would destroy this element
      // between the two clicks of a double-click and the dblclick would never fire.
      if (didEdit) renderAll(); else highlight();
      commit(); endInteract();
    }
    el.addEventListener("pointerdown", function (e) { down(e, "move"); });
    hl.addEventListener("pointerdown", function (e) { down(e, "l"); });
    hr.addEventListener("pointerdown", function (e) { down(e, "r"); });
    el.addEventListener("dblclick", function (e) {     // open in the inspector
      e.preventDefault(); e.stopPropagation();
      S.edit.ui = S.edit.ui || {};
      S.edit.ui.selected = c.id; S.edit.ui.selection = [c.id];
      highlight(); commit(); openInspector();
    });
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
      + "<li><b>B</b> library panel · <b>I</b> clip inspector · <b>right-click a track head</b> to rename / delete / reorder</li>"
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
  function setZoom(px) {
    S.pxPerSec = Math.max(10, Math.min(400, px));
    var z = S.root && S.root.querySelector(".r2r-zoom"); if (z) z.value = Math.round(S.pxPerSec);
    updateZoomVal(); renderAll(); commit();
  }
  function updateZoomVal() {
    var v = S.root && S.root.querySelector(".r2r-zoomval");
    if (v) v.textContent = Math.round(S.pxPerSec) + " px/s";
  }
  function setPlayBtn(t) { var b = S.root && S.root.querySelector(".r2r-play"); if (b) b.textContent = t; }
  function renderRange() {                    // in/out marks → amber band on the ruler
    if (!S.ruler) return;
    var ui = S.edit.ui || {};
    if (ui["in"] == null || ui["out"] == null || ui["out"] <= ui["in"]) return;
    var band = document.createElement("div");
    band.className = "r2r-range";
    band.style.left = sec2px(ui["in"]) + "px";
    band.style.width = sec2px(ui["out"] - ui["in"]) + "px";
    S.ruler.appendChild(band);
  }
  // ---- collapsible right-docked inspector (pure client-side, hosted on #r2r-stage) ----
  function stageEl() { return document.getElementById("r2r-stage"); }
  function openInspector() { var s = stageEl(); if (s) s.classList.remove("r2r-ins-collapsed"); }
  function closeInspector() { var s = stageEl(); if (s) s.classList.add("r2r-ins-collapsed"); }
  function ensureInsChrome() {
    var s = stageEl(); if (!s) return;
    if (!s.querySelector("#r2r-ins-close")) {
      var b = document.createElement("button");
      b.id = "r2r-ins-close"; b.type = "button"; b.title = "Hide inspector"; b.textContent = "»";
      b.addEventListener("click", closeInspector);
      s.appendChild(b);
    }
    if (!s.querySelector("#r2r-reveal")) {
      var h = document.createElement("div");
      h.id = "r2r-reveal"; h.title = "Show clip inspector"; h.textContent = "◀ Clip";
      h.addEventListener("click", openInspector);
      s.appendChild(h);
    }
  }
  // left-docked Library rail — mirror of the inspector chrome above
  function openLibrary() { var s = stageEl(); if (s) s.classList.remove("r2r-lib-collapsed"); }
  function closeLibrary() { var s = stageEl(); if (s) s.classList.add("r2r-lib-collapsed"); }
  function ensureLibChrome() {
    var s = stageEl(); if (!s) return;
    if (!s.querySelector("#r2r-lib-close")) {
      var b = document.createElement("button");
      b.id = "r2r-lib-close"; b.type = "button"; b.title = "Hide library"; b.textContent = "«";
      b.addEventListener("click", closeLibrary);
      s.appendChild(b);
    }
    if (!s.querySelector("#r2r-lib-reveal")) {
      var h = document.createElement("div");
      h.id = "r2r-lib-reveal"; h.title = "Show library"; h.textContent = "Library ▶";
      h.addEventListener("click", openLibrary);
      s.appendChild(h);
    }
  }
  function ensureChrome() { ensureInsChrome(); ensureLibChrome(); }
  // ---- library bins: make thumbnails a menu surface + drag source, and let the
  //      canvas accept drops (onto a track, or blank space = a new track of its kind) ----
  var LIB_BINS = { "r2r-bin-outputs": "outputs", "r2r-bin-pbin": "pbin", "r2r-bin-gbin": "gbin" };
  function decorateLib() {
    Object.keys(LIB_BINS).forEach(function (gid) {
      var g = document.getElementById(gid); if (!g) return;
      var bin = LIB_BINS[gid], items = g.querySelectorAll(".thumbnail-item");
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        it.classList.add("r2r-lib-thumb");          // our menu + drag surface
        it.setAttribute("data-bin", bin);
        it.setAttribute("data-idx", i);             // display index → server path
        if (!it._r2rDrag) {
          it._r2rDrag = true;
          it.setAttribute("draggable", "true");
          it.addEventListener("dragstart", function (e) {
            var t = e.currentTarget;
            S.libDrag = { bin: t.getAttribute("data-bin"), idx: t.getAttribute("data-idx") };
            try { e.dataTransfer.setData("text/plain", "r2rlib"); e.dataTransfer.effectAllowed = "copy"; } catch (x) {}
          });
          it.addEventListener("dragend", function () { S.libDrag = null; });
        }
      }
    });
  }
  function wireLibDrop() {
    var sc = S.root && S.root.querySelector(".r2r-scroll");
    if (!sc || sc._r2rDrop) return;
    sc._r2rDrop = true;
    sc.addEventListener("dragover", function (e) {
      if (!S.libDrag) return;
      e.preventDefault(); e.dataTransfer.dropEffect = "copy"; sc.classList.add("r2r-drop-on");
    });
    sc.addEventListener("dragleave", function (e) { if (e.target === sc) sc.classList.remove("r2r-drop-on"); });
    sc.addEventListener("drop", function (e) {
      sc.classList.remove("r2r-drop-on");
      if (!S.libDrag) return;
      e.preventDefault();
      var bin = S.libDrag.bin, idx = S.libDrag.idx; S.libDrag = null;
      var track = laneAt(e.clientY) || "NEW";       // lane under cursor, else a new track
      var ref = S.lanes && S.lanes.querySelector(".r2r-lane");   // any lane = time-0 origin
      var t = ref ? Math.max(0, px2sec(e.clientX - ref.getBoundingClientRect().left)) : 0;
      relayCtx("libdrop|" + bin + "|" + idx + "|" + track + "|" + t.toFixed(3));
    });
  }
  function decorateProjbar() {     // icon buttons → hover tooltips (Gradio has no title prop)
    var tips = { "r2r-pb-open": "Open the selected project", "r2r-pb-save": "Save project",
                 "r2r-pb-snap": "Snapshot a named version" };
    Object.keys(tips).forEach(function (id) {
      var el = document.getElementById(id), b = el && (el.querySelector("button") || el);
      if (b && b.title !== tips[id]) b.title = tips[id];
    });
  }
  function libTick() { decorateLib(); wireLibDrop(); decorateProjbar(); }
  var _libT = null;
  function scheduleLibTick() { if (_libT) clearTimeout(_libT); _libT = setTimeout(libTick, 80); }
  // Reel2Reel-only thumbnail menu (no cross-plugin items). Reuses the track-menu
  // popup chrome (#r2r-trk-menu / closeTrackMenu); relays the same libadd/… verbs.
  function openLibMenu(ev, th) {
    ev.preventDefault(); closeTrackMenu();
    var bin = th.getAttribute("data-bin"), idx = th.getAttribute("data-idx");
    if (idx == null) return;
    var items = [["➕ Add to timeline", "libadd"], ["📦 Copy to project bin", "libpbin"],
                 ["🌐 Copy to global bin", "libgbin"]];
    if (bin === "pbin" || bin === "gbin") items.push(["sep"], ["✖ Remove from this bin", "librm"]);
    var menu = document.createElement("div");
    menu.className = "r2r-trk-menu"; menu.id = "r2r-trk-menu";
    items.forEach(function (it) {
      if (it[0] === "sep") {
        var s = document.createElement("div"); s.className = "r2r-trk-menu-sep";
        menu.appendChild(s); return;
      }
      var b = document.createElement("button"); b.textContent = it[0];
      b.addEventListener("click", function () { closeTrackMenu(); relayCtx(it[1] + "|" + bin + "|" + idx); });
      menu.appendChild(b);
    });
    placeMenu(menu, ev);
  }
  // shared popup placement + outside-close (used by track / lib / clip menus)
  function placeMenu(menu, ev) {
    document.body.appendChild(menu);
    var mw = menu.offsetWidth, mh = menu.offsetHeight;
    menu.style.left = Math.min(ev.clientX, window.innerWidth - mw - 6) + "px";
    menu.style.top = Math.min(ev.clientY, window.innerHeight - mh - 6) + "px";
    menu._onDown = function (e) { if (!menu.contains(e.target)) closeTrackMenu(); };
    setTimeout(function () {
      document.addEventListener("pointerdown", menu._onDown, true);
      window.addEventListener("blur", closeTrackMenu, { once: true });
    }, 0);
  }
  // OrphanSuite / cross-plugin menu items (Replicant, ImageSuite, …) that match an
  // element — read live from the shared registry so they can sit at the BOTTOM of
  // our own clip menu. Our Reel2Reel clip verbs are no longer registered there.
  function crossPluginItemsFor(el) {
    var M = window.SaintorphanMenu, out = [];
    if (!M || !M.items) return out;
    M.items.forEach(function (it) {
      var m = it.match, hitEl = null;
      if (m === "image") hitEl = el.closest("img");
      else if (m === "video") hitEl = el.closest("video");
      else { try { hitEl = el.closest(m); } catch (e) {} }
      if (hitEl) out.push({ label: it.label, handler: it.handler, el: hitEl });
    });
    return out;
  }
  // Timeline-clip menu: standard timeline + Reel2Reel actions on top, then host
  // sends, then cross-plugin items at the very bottom. No "saintorphan" header.
  function openClipMenu(ev, cl) {
    ev.preventDefault(); closeTrackMenu();
    var id = cl.getAttribute("data-id"); if (!id) return;
    var menu = document.createElement("div");
    menu.className = "r2r-trk-menu"; menu.id = "r2r-trk-menu";
    function add(label, fn) {
      var b = document.createElement("button"); b.textContent = label;
      b.addEventListener("click", function () { closeTrackMenu(); fn(); });
      menu.appendChild(b);
    }
    function sep() { var s = document.createElement("div"); s.className = "r2r-trk-menu-sep"; menu.appendChild(s); }
    add("✂ Split at playhead", function () { relayCtx("csplit|" + id); });
    add("⧉ Duplicate", function () { relayCtx("cdup|" + id); });
    add("🎙 Detach audio", function () { relayCtx("cdetach|" + id); });
    add("📦 Copy to project bin", function () { relayCtx("clip2pbin|" + id); });
    add("🌐 Copy to global bin", function () { relayCtx("clip2gbin|" + id); });
    add("🗑 Delete clip", function () { relayCtx("cdel|" + id); });
    sep();
    add("→ Send to Vid2Vid", function () { relayCtx("vid2vid|" + id); });
    add("→ I2V first frame", function () { relayCtx("start|" + id); });
    add("→ I2V last frame", function () { relayCtx("end|" + id); });
    add("→ Sliding-window anchor", function () { relayCtx("anchor|" + id); });
    var cross = crossPluginItemsFor(cl);
    if (cross.length) {
      sep();
      cross.forEach(function (it) { add(it.label, function () { try { it.handler(it.el); } catch (e) {} }); });
    }
    placeMenu(menu, ev);
  }
  function openMarkerMenu(ev, el) {
    ev.preventDefault(); closeTrackMenu();
    var mid = el.dataset.mid;
    var m = (S.edit.markers || []).filter(function (x) { return x.id === mid; })[0];
    if (!m) return;
    var menu = document.createElement("div");
    menu.className = "r2r-trk-menu"; menu.id = "r2r-trk-menu";
    function add(label, fn) {
      var b = document.createElement("button"); b.textContent = label;
      b.addEventListener("click", function () { closeTrackMenu(); fn(); });
      menu.appendChild(b);
    }
    add("⏎ Go to marker", function () { stop(); setPlayhead(m.t || 0); commit(); });
    add("✎ Rename…", function () {
      var v = window.prompt("Marker label:", m.label || "");
      if (v != null) { m.label = v; renderAll(); commit(); }
    });
    add("🗑 Delete marker", function () {
      S.edit.markers = (S.edit.markers || []).filter(function (x) { return x.id !== mid; });
      renderAll(); commit();
    });
    placeMenu(menu, ev);
  }
  function syncSnapBox() {
    var b = S.root && S.root.querySelector('[data-act="snap"]');
    if (b) b.classList.toggle("active", S.snap);
  }
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
    else if (k === "i" && !(e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      var s = stageEl();
      if (s) (s.classList.contains("r2r-ins-collapsed") ? openInspector : closeInspector)();
    }
    else if (k === "b" && !(e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      var sb = stageEl();
      if (sb) (sb.classList.contains("r2r-lib-collapsed") ? openLibrary : closeLibrary)();
    }
  }

  // ---- mount ----------------------------------------------------------------
  function buildSkeleton(root) {
    root.innerHTML = "";
    var wrap = document.createElement("div"); wrap.className = "r2r-tl";
    wrap.innerHTML =
      '<video class="r2r-preview" playsinline></video>' +   // playhead-driven, no native controls
      '<img class="r2r-preview-img" alt="" style="display:none">' +   // shown for still-image clips
      '<div class="r2r-toolbar">' +
      '  <div class="r2r-grp">' +                            // transport
      '    <button class="r2r-btn" data-act="home" title="Go to start">⏮</button>' +
      '    <button class="r2r-btn" data-act="fback" title="Back 1s (J)">◀</button>' +
      '    <button class="r2r-btn r2r-play" data-act="play" title="Play / pause (Space)">►</button>' +
      '    <button class="r2r-btn" data-act="ffwd" title="Forward 1s (L)">▶</button>' +
      '    <button class="r2r-btn" data-act="end" title="Go to end">⏭</button>' +
      '    <span class="r2r-readout">0:00.00 / 0:00.00</span>' +
      '    <button class="r2r-btn r2r-help" data-act="help" title="Keyboard shortcuts (?)">?</button>' +
      '  </div>' +
      '  <div class="r2r-sep"></div>' +
      '  <div class="r2r-grp">' +                            // edit
      '    <button class="r2r-btn" data-gr="r2r-undo" title="Undo (⌘Z)">↶</button>' +
      '    <button class="r2r-btn" data-gr="r2r-redo" title="Redo (⌘⇧Z)">↷</button>' +
      '    <button class="r2r-btn" data-gr="r2r-split" title="Split at playhead (S)">✂</button>' +
      '    <button class="r2r-btn r2r-razor" data-act="razor" title="Razor — click a clip to cut (R)">Razor</button>' +
      '    <button class="r2r-btn" data-gr="r2r-ripple" title="Ripple delete (Del)">⇤</button>' +
      '    <button class="r2r-btn" data-gr="r2r-lift" title="Delete clip (leave gap)">🗑</button>' +
      '    <button class="r2r-btn" data-gr="r2r-dup" title="Duplicate clip">⧉</button>' +
      '  </div>' +
      '  <div class="r2r-sep"></div>' +
      '  <div class="r2r-grp">' +                            // insert
      '    <button class="r2r-btn" data-gr="r2r-addv" title="Add video track">+Video</button>' +
      '    <button class="r2r-btn" data-gr="r2r-adda" title="Add audio track">+Audio</button>' +
      '    <button class="r2r-btn" data-gr="r2r-title" title="Add title clip">🆃</button>' +
      '    <button class="r2r-btn" data-gr="r2r-marker" title="Add marker at playhead">🚩</button>' +
      '  </div>' +
      '  <div class="r2r-sep"></div>' +
      '  <div class="r2r-grp">' +                            // view
      '    <button class="r2r-btn" data-act="zout" title="Zoom out">−</button>' +
      '    <input type="range" class="r2r-zoom" min="10" max="400" value="80" title="Zoom">' +
      '    <span class="r2r-zoomval">80 px/s</span>' +
      '    <button class="r2r-btn" data-act="zin" title="Zoom in">+</button>' +
      '    <button class="r2r-btn" data-act="fit" title="Zoom to fit (F)">Fit</button>' +
      '    <button class="r2r-btn" data-act="zoomsel" title="Zoom to selection (⇧Z)">Sel</button>' +
      '    <button class="r2r-btn" data-act="snap" title="Toggle snapping">Snap</button>' +
      '    <button class="r2r-btn" data-act="in" title="Set in mark ([)">[</button>' +
      '    <button class="r2r-btn" data-act="out" title="Set out mark (])">]</button>' +
      '  </div>' +
      '  <div class="r2r-sep"></div>' +
      '  <div class="r2r-grp r2r-settings">' +               // sequence settings (gear popover)
      '    <button class="r2r-btn" data-act="gear" title="Timeline FPS / size">⚙</button>' +
      '    <div class="r2r-seq-pop">' +
      '      <label>FPS <input type="number" class="r2r-fps" min="1" max="120" step="1"></label>' +
      '      <label>Size <input class="r2r-res" size="9" placeholder="1280x720"></label>' +
      '      <button class="r2r-btn r2r-matchfps" title="Set fps to the highest source-clip fps">Match fps</button>' +
      '    </div>' +
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
    S.previewImg = wrap.querySelector(".r2r-preview-img");
    S.readout = wrap.querySelector(".r2r-readout");
    wireRuler();
    // One delegated handler: data-gr fires a hidden Gradio button (Python action via
    // the clickGr bridge); data-act runs a pure client-side view command.
    wrap.querySelector(".r2r-toolbar").addEventListener("click", function (e) {
      var el = e.target.closest("[data-gr],[data-act]");
      if (!el) return;
      e.preventDefault();
      if (el.dataset.gr) { clickGr(el.dataset.gr); return; }
      switch (el.dataset.act) {
        case "play": togglePlay(); break;
        case "home": stop(); setPlayhead(0); commit(); break;
        case "end": stop(); setPlayhead(totalDur()); commit(); break;
        case "fback": stop(); setPlayhead(ph() - 1); commit(); break;
        case "ffwd": stop(); setPlayhead(ph() + 1); commit(); break;
        case "fit": fit(); break;
        case "zoomsel": zoomToSelection(); break;
        case "zin": setZoom(S.pxPerSec * 1.3); break;
        case "zout": setZoom(S.pxPerSec / 1.3); break;
        case "snap": S.snap = !S.snap; el.classList.toggle("active", S.snap); commit(); break;
        case "razor": toggleRazor(); break;
        case "in": S.edit.ui = S.edit.ui || {}; S.edit.ui["in"] = ph(); renderAll(); commit(); break;
        case "out": S.edit.ui = S.edit.ui || {}; S.edit.ui["out"] = ph(); renderAll(); commit(); break;
        case "gear": var pop = wrap.querySelector(".r2r-seq-pop"); if (pop) pop.classList.toggle("open"); break;
        case "help": toggleHelp(); break;
      }
    });
    wrap.querySelector(".r2r-zoom").addEventListener("input", function (e) {
      setZoom(parseInt(e.target.value, 10) || 80);
    });
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
    ensureChrome();
    S.mounted = true; renderAll(); syncSnapBox();
  }
  function tryMount() {
    var root = document.getElementById(ROOT_ID);
    if (root && (!root.querySelector(".r2r-tl") || !S.mounted)) buildSkeleton(root);
    ensureChrome();   // idempotent: re-attach reveal/close chrome on #r2r-stage after re-mounts
  }
  function boot() {
    tryMount();
    libTick();
    var tries = 0;
    (function poll() { if (S.mounted || tries++ > 80) return;
      requestAnimationFrame(function () { tryMount(); setTimeout(poll, 100); }); })();
    try {
      new MutationObserver(function () {
        var root = document.getElementById(ROOT_ID);
        if (root && !root.querySelector(".r2r-tl")) { S.mounted = false; tryMount(); }
        scheduleLibTick();    // re-tag bin thumbnails + (re)wire drop after any DOM change
      }).observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
    window.addEventListener("keydown", onKey, true);
    // Live grade/opacity preview while dragging the inspector sliders.
    document.addEventListener("input", function (e) {
      if (e.target && e.target.closest && e.target.closest("#reel2reel-inspector"))
        applyPreviewFilter(currentPreviewClip());
    }, true);
    // window-capture beats the shared SaintorphanMenu's document-capture listener, so
    // our surfaces get OUR Reel2Reel-first menu: bins (no cross-plugin items) and
    // clips (standard actions on top, cross-plugin items appended at the bottom).
    window.addEventListener("contextmenu", function (e) {
      var th = e.target.closest && e.target.closest(".r2r-lib-thumb");
      if (th) { e.stopImmediatePropagation(); e.preventDefault(); openLibMenu(e, th); return; }
      var cl = e.target.closest && e.target.closest(".r2r-timeline-clip");
      if (cl) { e.stopImmediatePropagation(); e.preventDefault(); openClipMenu(e, cl); return; }
      var mk = e.target.closest && e.target.closest(".r2r-marker");
      if (mk) { e.stopImmediatePropagation(); e.preventDefault(); openMarkerMenu(e, mk); return; }
    }, true);
  }

  window.R2RTimeline = { applyOp: applyOp, remount: tryMount, _state: S };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
