"""画像审批台 Web —— 她这一侧的门。

- GET  /portrait                    审批台页面(需 Dashboard 登录)
- GET  /api/portrait/state          画像 + 事实(JSON)
- POST /api/portrait/update         更新画像层(JSON: target/tier/content;网页侧三张画像所有层都可写)
- POST /api/portrait/fact?id=&action=approve|reject|deprecate  审批事实
       (approve 时请求体可携带修改后的 object_text,原样存入)

存储与 MCP 端共用: <buckets_dir>/portrait/{portrait.json, facts.json}
规则:K 提交的 profile_fact 停在 pending,这里批准才 active —— 确认机制的正身。
"""

import os
import json
import datetime as _dt

from starlette.requests import Request
from starlette.responses import Response, JSONResponse, HTMLResponse, RedirectResponse

from . import _shared as sh
from . import _ui

_TARGETS = ("persona", "user", "relationship")
_TIERS = ("stable", "midterm")


def _root() -> str:
    root = os.path.join(sh.config.get("buckets_dir", "buckets"), "portrait")
    os.makedirs(root, exist_ok=True)
    return root


def _load(name: str, default):
    path = os.path.join(_root(), name)
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(name: str, data) -> None:
    path = os.path.join(_root(), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _default_portrait() -> dict:
    return {t: {"stable": {"text": "", "updated": ""},
                "midterm": {"text": "", "updated": ""}} for t in _TARGETS}


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


_PAGE = _ui.page_head("画像审批台 · Ombre Brain") + """<body>
<h1>画像审批台</h1>
<p class="meta">三张画像的 stable 层只有这里能写。K 提交的事实停在待审区,批准才生效。
<a href="/dashboard">← 返回 Dashboard</a> · <a href="/files">文件区</a></p>
<div id="pending" class="card"><h2>待审事实 <span id="pcount" class="pill"></span></h2><div id="plist"></div></div>
<div id="portraits"></div>
<div class="card"><h2>已生效事实 <span id="acount" class="pill"></span></h2><div id="alist"></div></div>
<script>
const NAMES = {persona:'Persona(他)', user:'User(她)', relationship:'Relationship(你们)'};
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs||{})) {
    if (k==='text') e.textContent = v; else if (k.startsWith('on')) e[k]=v; else e.setAttribute(k,v);
  }
  kids.forEach(k=>e.appendChild(k)); return e;
}
async function load() {
  const r = await fetch('/api/portrait/state');
  if (r.status === 401) { location.href='/dashboard'; return; }
  const d = await r.json();
  // 画像
  const box = document.getElementById('portraits'); box.innerHTML='';
  for (const t of ['persona','user','relationship']) {
    const card = el('div',{class:'card'}); card.appendChild(el('h2',{text:NAMES[t]}));
    for (const tier of ['stable','midterm']) {
      const cur = ((d.portrait[t]||{})[tier])||{};
      card.appendChild(el('div',{class:'tierlabel',
        text: tier + (cur.updated ? ' · 更新于 '+cur.updated : ' · 空')}));
      const ta = el('textarea',{rows:'3'}); ta.value = cur.text||''; card.appendChild(ta);
      card.appendChild(el('button',{text:'保存 '+tier, onclick: async ()=>{
        await fetch('/api/portrait/update',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({target:t, tier:tier, content:ta.value})});
        load();
      }}));
    }
    box.appendChild(card);
  }
  // 待审
  const pl = document.getElementById('plist'); pl.innerHTML='';
  const pend = d.facts.filter(f=>f.status==='pending');
  document.getElementById('pcount').textContent = pend.length;
  if (!pend.length) pl.appendChild(el('div',{class:'meta',text:'队列是空的。'}));
  for (const f of pend) {
    const row = el('div',{class:'fact'});
    row.appendChild(el('div',{class:'meta',
      text:`[${f.id}] ${f.subject} · ${f.predicate} · 置信 ${f.confidence} · 证据: ${f.evidence} · ${f.created}`}));
    const ta = el('textarea',{rows:'2'}); ta.value = f.object_text; row.appendChild(ta);
    row.appendChild(el('button',{class:'ok',text:'批准(按上框文本)', onclick: async ()=>{
      await fetch('/api/portrait/fact?id='+encodeURIComponent(f.id)+'&action=approve',
                  {method:'POST', body: ta.value}); load();
    }}));
    row.appendChild(el('button',{text:'驳回', class:'no', onclick: async ()=>{
      await fetch('/api/portrait/fact?id='+encodeURIComponent(f.id)+'&action=reject',{method:'POST'}); load();
    }}));
    pl.appendChild(row);
  }
  // 已生效
  const al = document.getElementById('alist'); al.innerHTML='';
  const act = d.facts.filter(f=>f.status==='active');
  document.getElementById('acount').textContent = act.length;
  if (!act.length) al.appendChild(el('div',{class:'meta',text:'还没有。'}));
  for (const f of act) {
    const row = el('div',{class:'fact'});
    row.appendChild(el('div',{text:`${f.subject} · ${f.predicate} · ${f.object_text}`}));
    row.appendChild(el('div',{class:'meta',text:`[${f.id}] 置信 ${f.confidence} · 证据: ${f.evidence} · 批准于 ${f.updated}`}));
    row.appendChild(el('button',{text:'废弃', class:'no', onclick: async ()=>{
      if (!confirm('废弃这条事实?')) return;
      await fetch('/api/portrait/fact?id='+encodeURIComponent(f.id)+'&action=deprecate',{method:'POST'}); load();
    }}));
    al.appendChild(row);
  }
}
load();
</script></body></html>"""


def register(mcp) -> None:

    @mcp.custom_route("/portrait", methods=["GET"])
    async def portrait_page(request: Request) -> Response:
        if sh._require_auth(request) is not None:
            return RedirectResponse("/dashboard")
        return HTMLResponse(_PAGE)

    @mcp.custom_route("/api/portrait/state", methods=["GET"])
    async def portrait_get(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({
            "portrait": _load("portrait.json", _default_portrait()),
            "facts": _load("facts.json", []),
        })

    @mcp.custom_route("/api/portrait/update", methods=["POST"])
    async def portrait_update_web(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = json.loads((await request.body()).decode("utf-8"))
        except Exception:
            return JSONResponse({"error": "无效 JSON"}, status_code=400)
        target = (body.get("target") or "").strip().lower()
        tier = (body.get("tier") or "").strip().lower()
        content = (body.get("content") or "").strip()[:2000]
        if target not in _TARGETS or tier not in _TIERS:
            return JSONResponse({"error": "target/tier 非法"}, status_code=400)
        p = _load("portrait.json", _default_portrait())
        p.setdefault(target, {})[tier] = {"text": content, "updated": _now()}
        _save("portrait.json", p)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/portrait/fact", methods=["POST"])
    async def portrait_fact_decide(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        fid = request.query_params.get("id", "")
        action = request.query_params.get("action", "")
        if action not in ("approve", "reject", "deprecate"):
            return JSONResponse({"error": "action 非法"}, status_code=400)
        facts = _load("facts.json", [])
        hit = next((x for x in facts if x.get("id") == fid), None)
        if hit is None:
            return JSONResponse({"error": "没有这条事实"}, status_code=404)
        if action == "approve":
            edited = (await request.body()).decode("utf-8", errors="replace").strip()
            if edited:
                hit["object_text"] = edited[:500]
            hit["status"] = "active"
        elif action == "reject":
            hit["status"] = "deprecated"
        else:
            hit["status"] = "deprecated"
        hit["updated"] = _now()
        _save("facts.json", facts)
        return JSONResponse({"ok": True, "id": fid, "status": hit["status"]})
