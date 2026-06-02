"""Reel2Reel chrome CSS — light touch: amber tab accent, the logo banner, the
hidden-class trick. The timeline's own styling lives in assets/static/timeline.css
(injected separately by ui/timeline_widget.py)."""

_ACCENT = "#e0a106"      # reel-to-reel amber/gold
_ACCENT_DK = "#b07d00"

CSS = f"""
#reel2reel-root {{ position: relative; }}
#reel2reel-root .reel2reel-acc {{ border-radius: 10px; }}
.reel2reel-hidden {{ display: none !important; }}

/* amber accent on our main-webui tab button (tagged from JS in plugin.py) */
button.reel2reel-tabbtn {{ border-bottom: 2px solid {_ACCENT} !important; }}

#reel2reel-root .reel2reel-prim {{ background: {_ACCENT}; border-color: {_ACCENT_DK}; }}
#reel2reel-root .reel2reel-gallery {{ min-height: 300px; }}

/* Logo banner, top-right; absolutely positioned so the left column rises to the
   same top line, with right padding on the sub-tab nav so it never slides under
   the logo. */
#reel2reel-banner {{
    position: absolute; top: 0; right: 8px; z-index: 5; pointer-events: none;
}}
#reel2reel-banner img {{ height: 64px; width: auto; display: block; }}
#reel2reel-banner h2 {{ margin: 0; color: {_ACCENT}; font-style: italic; }}
#reel2reel-root .tab-nav {{ padding-right: 280px; }}
"""
