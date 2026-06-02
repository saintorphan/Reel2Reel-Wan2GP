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
#reel2reel-banner img {{ height: 34px; width: auto; display: block; }}
#reel2reel-banner h2 {{ margin: 0; color: {_ACCENT}; font-style: italic; font-size: 20px; }}
#reel2reel-root .tab-nav {{ padding-left: 140px; }}
"""
