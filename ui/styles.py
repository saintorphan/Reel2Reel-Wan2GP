"""Reel2Reel chrome CSS — light touch: amber tab accent, the logo banner, the
hidden-class trick. The timeline's own styling lives in assets/static/timeline.css
(injected separately by ui/timeline_widget.py)."""

_ACCENT = "#e0a106"      # reel-to-reel amber/gold
_ACCENT_DK = "#b07d00"

CSS = f"""
#reel2reel-root {{ position: relative; }}
#reel2reel-root .reel2reel-acc {{ border-radius: 10px; }}
.reel2reel-hidden {{ display: none !important; }}

/* Green-coded main-webui tab button (tagged from JS in plugin.py), matching the
   other saintorphan plugins' tab color-coding (ImageSuite pink, Replicant violet). */
button.reel2reel-tabbtn {{ border: 2px solid #2ea043 !important; border-radius: 6px; }}

#reel2reel-root .reel2reel-prim {{ background: {_ACCENT}; border-color: {_ACCENT_DK}; }}
#reel2reel-root .reel2reel-gallery {{ min-height: 300px; }}
#reel2reel-inspector {{ border-left: 2px solid {_ACCENT_DK}; padding-left: 10px; }}

/* Small logo, tucked top-LEFT. Absolutely positioned; the sub-tab nav gets left
   padding so its buttons never slide under the logo. */
#reel2reel-banner {{
    position: absolute; top: 2px; left: 6px; z-index: 6; pointer-events: none;
}}
#reel2reel-banner img {{ height: 60px; width: auto; display: block; }}
#reel2reel-banner h2 {{ margin: 0; color: {_ACCENT}; font-style: italic; font-size: 28px; }}
#reel2reel-root .tab-nav {{ padding-left: 230px; }}

/* ---- persistent project / version bar (above the sub-tabs, on every page) ---- */
#reel2reel-projbar {{ padding-left: 230px; display: flex; align-items: center; gap: 8px;
    margin-bottom: 4px; padding-bottom: 6px; border-bottom: 1px solid {_ACCENT_DK}; }}
#reel2reel-projbar > * {{ margin: 0 !important; }}
#reel2reel-projbar .prose p {{ margin: 0; font-size: 13px; }}
#reel2reel-bar-status .prose p {{ margin: 0; font-size: 12px; color: {_ACCENT}; }}
#reel2reel-manage {{ margin-bottom: 6px; }}

/* ---- timeline: hide the demoted host-action Row (kept in DOM for the JS bridge) ---- */
#r2r-host-tools {{ display: none !important; }}

/* ---- collapsible right-docked inspector ----
   display:none on the inspector lets the scale=3 canvas column flex-grow to full width. */
#r2r-stage {{ position: relative; }}
#reel2reel-inspector {{ border-left: 2px solid {_ACCENT_DK}; padding-left: 10px;
    flex: 0 0 360px; max-width: 420px; min-width: 300px; }}
#r2r-stage.r2r-ins-collapsed #reel2reel-inspector {{ display: none !important; }}
#reel2reel-inspector video {{ max-height: 40vh; }}
#r2r-ins-close {{ position: absolute; top: 6px; right: 10px; z-index: 7; width: 26px;
    height: 26px; border: 1px solid {_ACCENT_DK}; border-radius: 6px; background: #1f2128;
    color: {_ACCENT}; cursor: pointer; font-weight: 700; display: flex; align-items: center;
    justify-content: center; }}
#r2r-stage.r2r-ins-collapsed #r2r-ins-close {{ display: none; }}
#r2r-reveal {{ position: absolute; top: 0; right: 0; width: 16px; height: 100%; z-index: 7;
    display: none; align-items: center; justify-content: center; cursor: pointer;
    background: linear-gradient({_ACCENT_DK}, {_ACCENT}); color: #15161a;
    writing-mode: vertical-rl; font-size: 11px; font-weight: 700; border-radius: 6px 0 0 6px;
    user-select: none; }}
#r2r-stage.r2r-ins-collapsed #r2r-reveal {{ display: flex; }}

/* ---- collapsible left-docked Library rail (mirror of the inspector) ---- */
#reel2reel-librail {{ border-right: 2px solid {_ACCENT_DK}; padding-right: 10px;
    flex: 0 0 320px; max-width: 380px; min-width: 250px; }}
#r2r-stage.r2r-lib-collapsed #reel2reel-librail {{ display: none !important; }}
#r2r-lib-close {{ position: absolute; top: 6px; left: 10px; z-index: 7; width: 26px;
    height: 26px; border: 1px solid {_ACCENT_DK}; border-radius: 6px; background: #1f2128;
    color: {_ACCENT}; cursor: pointer; font-weight: 700; display: flex; align-items: center;
    justify-content: center; }}
#r2r-stage.r2r-lib-collapsed #r2r-lib-close {{ display: none; }}
#r2r-lib-reveal {{ position: absolute; top: 0; left: 0; width: 16px; height: 100%; z-index: 7;
    display: none; align-items: center; justify-content: center; cursor: pointer;
    background: linear-gradient({_ACCENT_DK}, {_ACCENT}); color: #15161a;
    writing-mode: vertical-rl; font-size: 11px; font-weight: 700; border-radius: 0 6px 6px 0;
    user-select: none; }}
#r2r-stage.r2r-lib-collapsed #r2r-lib-reveal {{ display: flex; }}

/* ---- library: uniform galleries, one action bar, thin danger row ---- */
#reel2reel-root .reel2reel-gallery .thumbnail-item img {{ object-fit: cover; }}
#reel2reel-lib-actions {{ align-items: center; gap: 8px; padding: 8px 10px; margin-top: 6px;
    border: 1px solid var(--border-color-primary); border-radius: 10px; }}
#reel2reel-lib-actions .prose p {{ margin: 0; opacity: .85; font-size: 13px; }}
.reel2reel-lib-danger {{ justify-content: flex-end; opacity: .85; }}
.reel2reel-lib-danger button {{ color: #c0392b; }}
#reel2reel-lib-tabs .tab-nav {{ padding-left: 34px !important; }}  /* clear #r2r-lib-close */

/* ---- render: compact controls + big sticky preview ---- */
#reel2reel-root #r2r-render-controls {{ max-width: 380px; }}
#reel2reel-root #r2r-render-preview {{ position: sticky; top: 8px; }}
#reel2reel-root #r2r-render-preview .r2r-render-video,
#reel2reel-root #r2r-render-preview .r2r-render-video video {{ width: 100%; height: 56vh;
    min-height: 360px; object-fit: contain; background: #000; border-radius: 8px; }}
#reel2reel-root .r2r-range-off {{ display: none !important; }}
"""
