"""Self-contained answer form served at GET /inbox (no external resources)."""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inbox</title>
<style>
:root{--bg:#fafafa;--fg:#1a1a1a;--card:#fff;--bd:#c8c8c8;--mut:#666;--acc:#0b5fd0;--bad:#b00020;--ok:#0a7a32;--tag:#fde7c8}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--card:#1e1e1e;--bd:#3a3a3a;--mut:#9a9a9a;--acc:#6aa8ff;--bad:#ff7a8a;--ok:#5fd68a;--tag:#5a3d10}}
*{box-sizing:border-box}
body{margin:0 auto;max-width:42rem;padding:1rem;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}
h1{font-size:1.3rem;margin:0 0 .75rem}
.row{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.75rem}
input,textarea,button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:.5rem}
input{flex:1;min-width:10rem}
button{cursor:pointer;background:var(--acc);color:var(--bg);border-color:var(--acc)}
button:disabled{opacity:.5;cursor:wait}
textarea{width:100%;min-height:5rem;margin:.5rem 0}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:.75rem;margin-bottom:.75rem}
.key{font-family:monospace;font-weight:bold;word-break:break-all}
.txt{white-space:pre-wrap;margin:.4rem 0}
.mut{color:var(--mut);font-size:.85rem}
.tag{display:inline-block;background:var(--tag);border-radius:10px;padding:0 .5rem;margin-right:.25rem;font-size:.8rem}
.need{color:var(--bad);font-weight:bold}
.err{color:var(--bad)}.ok{color:var(--ok)}
</style></head><body>
<h1>Inbox</h1>
<div class="row">
<input id="tok" type="password" placeholder="bearer token" autocomplete="off">
<button id="load">Load</button><button id="ref">Refresh</button>
</div>
<div id="msg" class="mut"></div>
<div id="list"></div>
<script>
(function(){
var token="";
var $=function(i){return document.getElementById(i)};
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=String(text);return e}
function msg(t,cls){var m=$("msg");m.className=cls||"mut";m.textContent=t}
function hdr(json){var h={Authorization:"Bearer "+token};if(json)h["Content-Type"]="application/json";return h}
function errText(r,b){
  var m=b&&(b.msg||b.error);
  if(r.status===403)m="this token may not answer; use the router token";
  return "error: "+(m||("HTTP "+r.status));
}
function card(q){
  var c=el("div","card"),tags=Array.isArray(q.risk_tags)?q.risk_tags:[];
  c.appendChild(el("div","key",q.key));
  c.appendChild(el("div","txt",q.text));
  c.appendChild(el("div","mut","created "+(q.created_at||"?")));
  if(tags.length){var t=el("div");tags.forEach(function(x){t.appendChild(el("span","tag",x))});c.appendChild(t)}
  if(q.default_answer!=null)
    c.appendChild(el("div","mut","Default if unanswered: "+q.default_answer+" (applies after "+(q.deadline||q.default_deadline||"?")+"; decided by the workflow timer, not by this page)"));
  if(tags.length||q.default_answer==null)
    c.appendChild(el("div","need","No default - needs your answer"));
  var ta=el("textarea"),btn=el("button",null,"Answer"),st=el("div","mut");
  ta.placeholder="your answer";
  btn.addEventListener("click",function(){
    var text=ta.value.trim();
    if(!text){st.className="err";st.textContent="error: answer is empty";return}
    btn.disabled=true;st.className="mut";st.textContent="sending...";
    fetch("/v1/answer_question",{method:"POST",headers:hdr(true),body:JSON.stringify({question_id:q.id,text:text})})
    .then(function(r){return r.json().catch(function(){return {}}).then(function(b){
      if(r.ok){var s=b.signal&&b.signal.status;c.remove();msg("answered "+q.key+" - signal: "+(s||"unknown"),"ok")}
      else{st.className="err";st.textContent=errText(r,b);btn.disabled=false}
    })})
    .catch(function(e){st.className="err";st.textContent="error: "+e.message;btn.disabled=false});
  });
  c.appendChild(ta);c.appendChild(btn);c.appendChild(st);
  return c;
}
function load(){
  token=$("tok").value;
  if(!token){msg("enter a token first","err");return}
  msg("loading...");
  fetch("/v1/questions?state=open",{headers:hdr(false)})
  .then(function(r){return r.json().catch(function(){return {}}).then(function(b){
    if(!r.ok){msg(errText(r,b),"err");return}
    var qs=Array.isArray(b)?b:(b.questions||[]),l=$("list");
    l.textContent="";
    qs.forEach(function(q){l.appendChild(card(q))});
    msg(qs.length+" open question(s)");
  })})
  .catch(function(e){msg("error: "+e.message,"err")});
}
$("load").addEventListener("click",load);
$("ref").addEventListener("click",load);
})();
</script></body></html>
"""
