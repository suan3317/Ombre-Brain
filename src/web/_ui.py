"""web/_ui.py — 子页面共享 UI(与 frontend/dashboard.html 的复古掌机设计同源)。

dashboard.html 的大体量 CSS 是页面专属的,这里抽取它的设计 token(调色板/字体/
拟物光影)做成一份轻量样式,给 /files、/portrait 这类子页面用,保证全站观感统一。
改配色时请与 dashboard.html 的 :root 保持同步。

对外暴露:page_head(title) —— 返回 <!doctype html> 到 </head> 的完整头部。
非路由模块,不需要在 web/__init__.py 的 _WEB_MODULES 里注册,普通 import 即可。
"""

_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500'
    '&family=Cormorant+Garamond:ital,wght@0,400;0,500;1,400'
    '&family=Share+Tech+Mono&display=swap" rel="stylesheet">'
)

_CSS = """
:root {
  --bg:#E7E2D8; --surface:#F4F0E8; --surface-solid:#EDE8DE;
  --border:#DCD6CA; --border-strong:#C7C0B2;
  --text:#2C2A26; --text-dim:#6E695E; --text-light:#9E988C;
  --accent:#C2982F; --accent-light:#D7B14A; --accent-glow:rgba(194,152,47,.22);
  --positive:#87A987; --negative:#BE5A41;
  --shadow-light:rgba(255,253,247,.9); --shadow-dark:rgba(150,138,116,.40);
  --shadow-dark-subtle:rgba(150,138,116,.22);
}
* { box-sizing:border-box; }
body {
  font-family:'Share Tech Mono','Noto Sans SC',system-ui,sans-serif;
  background:
    radial-gradient(circle at 1px 1px, rgba(120,108,86,.05) 1px, transparent 1.4px) 0 0 / 5px 5px,
    var(--bg);
  color:var(--text); max-width:880px; margin:32px auto; padding:0 20px;
  line-height:1.75; letter-spacing:.02em; -webkit-font-smoothing:antialiased;
}
h1 { font-family:'Cormorant Garamond','Noto Sans SC',serif; font-size:26px; font-weight:600;
     color:var(--accent); letter-spacing:.5px; margin:8px 0 4px; }
h2 { font-family:'Noto Sans SC',sans-serif; font-size:15px; font-weight:500;
     margin:4px 0 12px; color:var(--text); }
a { color:var(--accent); text-decoration:none; }
a:hover { color:var(--accent-light); }
.meta { font-size:13px; color:var(--text-dim); }
.card {
  background:var(--surface); border-radius:16px; padding:18px 22px; margin:16px 0;
  box-shadow:6px 6px 12px var(--shadow-dark-subtle), -6px -6px 12px var(--shadow-light);
}
button {
  background:var(--surface-solid); border:none; border-radius:12px; padding:7px 16px;
  cursor:pointer; font-family:inherit; font-size:13px; color:var(--text);
  box-shadow:4px 4px 8px var(--shadow-dark-subtle), -4px -4px 8px var(--shadow-light);
  transition:all .2s ease; margin-right:6px;
}
button:hover { box-shadow:5px 5px 10px var(--shadow-dark), -5px -5px 10px var(--shadow-light);
               transform:translateY(-1px); }
button:active { box-shadow:inset 3px 3px 6px var(--shadow-dark-subtle), inset -3px -3px 6px var(--shadow-light);
                transform:none; }
button.ok { background:var(--positive); color:var(--surface); }
button.del, .del, .no { color:var(--negative); }
input[type=text], input[type=file], textarea {
  background:var(--surface); border:1px solid var(--border); color:var(--text);
  padding:8px 12px; border-radius:10px; font-family:inherit; font-size:13px;
  box-shadow:inset 2px 2px 4px var(--shadow-dark-subtle), inset -2px -2px 4px var(--shadow-light);
  transition:all .3s ease;
}
input[type=text]:focus, textarea:focus {
  outline:none; border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-glow), inset 2px 2px 4px var(--shadow-dark-subtle);
}
table { width:100%; border-collapse:collapse; font-size:14px; }
td,th { padding:8px 6px; border-bottom:1px solid var(--border); text-align:left; }
th { color:var(--text-dim); font-weight:400; font-size:12px; letter-spacing:.4px; }
pre.board {
  white-space:pre-wrap; background:var(--surface-solid); border-radius:10px;
  padding:12px 14px; max-height:300px; overflow:auto; font-size:13px;
  box-shadow:inset 2px 2px 5px var(--shadow-dark-subtle), inset -2px -2px 5px var(--shadow-light);
}
.pill { display:inline-block; background:var(--surface-solid); border-radius:999px;
        padding:1px 10px; font-size:12px; margin-left:6px; color:var(--text-dim);
        box-shadow:inset 1px 1px 3px var(--shadow-dark-subtle); }
.fact { border-top:1px solid var(--border); padding:10px 0; font-size:14px; }
.tierlabel { font-size:12px; color:var(--text-dim); margin-top:8px; }
#msg,#bmsg { min-height:20px; font-size:13px; color:var(--text-dim); }
"""


def page_head(title: str) -> str:
    """返回统一风格的 <!doctype html>...</head> 头部,title 为页面标题。"""
    return (
        '<!doctype html>\n<html lang="zh"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n{_FONTS}\n<style>{_CSS}</style></head>"
    )
