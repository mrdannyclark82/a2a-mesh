#!/usr/bin/env python3
"""Mesh Thread UI — Messenger-style viewer for the A2A bus.

Serves a two-pane, dark-mode chat page at / (thread rail + message pane)
and proxies the hub's tape at /api/notifications and roster at
/api/roster: a left rail of threads with previews and unread markers,
messages on the right.

Threads: selecting a thread scopes sending to it. The room thread
broadcasts (hub `to` is null). An agent thread addresses just that
agent. A group thread (created in the UI, stored in the browser) fans
out one addressed copy per member — the hub delivers each copy only
to its addressee's seat. The operator's own posts are badged verified
by the hub when the human id matches A2A_HUMAN_SENDERS there.

Configuration is by environment: A2A_HUB_HTTP (hub HTTP base),
A2A_UI_BIND / A2A_UI_PORT (where this page is served), A2A_UI_HUMAN_ID
(the operator's seat id; default "you" — set it to your handle, and
list it in the hub's A2A_HUMAN_SENDERS too).
"""
from __future__ import annotations
import json, os, re, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HUB = os.getenv("A2A_HUB_HTTP", "http://127.0.0.1:8766")
BIND = os.getenv("A2A_UI_BIND", "127.0.0.1")
PORT = int(os.getenv("A2A_UI_PORT", "8767"))
HUMAN_ID = os.getenv("A2A_UI_HUMAN_ID", "you")
ROOT = Path(__file__).resolve().parent
# Same convention as hub.py: the registry the hub serves is a file
# next to the code unless A2A_REGISTRY says otherwise. Adding an
# agent writes that file; the hub reads it fresh on every request,
# so a new agent appears in the rail with no restart anywhere.
REGISTRY_PATH = Path(os.getenv("A2A_REGISTRY", str(ROOT / "registry.json")))

try:
    import agent_templates as _templates
except Exception:  # UI still serves; the add-agent endpoints report why
    _templates = None

# Optional @aliases: nicknames the operator can type after @, mapped to
# seat ids — e.g. {"scout": "ollama-scout"}. The socket id stays put.
TO_ALIASES = {}


def resolve_to(token: str) -> str:
    key = token.strip().lower()
    return TO_ALIASES.get(key, token)

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mesh</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0e1a; --panel:#101728; --panel2:#0d1424; --line:#1e2a45;
  --txt:#e8eefc; --dim:#8fa3c8; --accent:#8ecfff; --accent-dim:#5b83ad;
  --bubble:#1a2745; --mine:#2b5ea7; --green:#3fb950; --amber:#e0a83c;
}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;height:100vh;display:flex;overflow:hidden}
/* ---------- rail ---------- */
#rail{width:324px;min-width:280px;background:var(--panel2);border-right:1px solid var(--line);display:flex;flex-direction:column}
#rail header{padding:16px 16px 10px;display:flex;align-items:center;gap:10px}
#rail header h1{font-size:22px;font-weight:700}
#rail header .dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green)}
#rail header .sub{font-size:11px;color:var(--dim);margin-left:auto;text-align:right}
#search{margin:0 12px 8px;background:var(--panel);border:1px solid var(--line);border-radius:18px;color:var(--txt);padding:8px 14px;font-size:13px;outline:none;width:calc(100% - 24px)}
#search:focus{border-color:var(--accent-dim)}
#newgroup{margin:0 12px 10px;background:none;border:1px dashed var(--accent-dim);color:var(--accent);border-radius:14px;padding:7px;font-size:13px;cursor:pointer}
#newgroup:hover{background:var(--panel)}
#gpanel{margin:0 12px 10px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;display:none}
#gpanel h3{font-size:13px;margin-bottom:8px}
#gpanel label{display:flex;gap:8px;align-items:center;font-size:13px;padding:4px 0;cursor:pointer}
#gname{width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:10px;color:var(--txt);padding:8px 10px;font-size:13px;outline:none;margin:8px 0}
#gpanel .row2{display:flex;gap:8px}
#gpanel button{flex:1;border:none;border-radius:10px;padding:8px;font-size:13px;font-weight:600;cursor:pointer}
#gcreate{background:var(--mine);color:#fff}
#gcancel{background:var(--bubble);color:var(--txt)}
#newagent{margin:0 12px 10px;background:none;border:1px dashed var(--accent-dim);color:var(--accent);border-radius:14px;padding:7px;font-size:13px;cursor:pointer}
#newagent:hover{background:var(--panel)}
#apanel{margin:0 12px 10px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;display:none}
#apanel h3{font-size:13px;margin-bottom:8px}
#apanel .adesc{font-size:12px;color:var(--dim);margin:6px 0 2px;line-height:1.4}
#apanel select,#apanel input{width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:10px;color:var(--txt);padding:8px 10px;font-size:13px;outline:none;margin:6px 0}
#aerror{display:none;color:#ff9d9d;font-size:12px;line-height:1.45;margin:6px 0;white-space:pre-wrap}
#apanel .row2{display:flex;gap:8px;margin-top:6px}
#apanel button{flex:1;border:none;border-radius:10px;padding:8px;font-size:13px;font-weight:600;cursor:pointer}
#acreate{background:var(--mine);color:#fff}
#acancel{background:var(--bubble);color:var(--txt)}
#convs{flex:1;overflow-y:auto;padding:2px 8px 12px}
.conv{display:flex;gap:11px;padding:10px 8px;border-radius:12px;cursor:pointer;align-items:center}
.conv:hover{background:var(--panel)}
.conv.sel{background:var(--panel);box-shadow:inset 2px 0 0 var(--accent)}
.ava{width:48px;height:48px;min-width:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;color:#fff;position:relative;text-align:center;line-height:1.1}
.ava .live{position:absolute;right:0;bottom:0;width:12px;height:12px;border-radius:50%;background:var(--green);border:2px solid var(--panel2)}
.cmeta{flex:1;min-width:0}
.cname{font-size:14px;font-weight:600;display:flex;gap:6px;align-items:baseline}
.cname .ctime{margin-left:auto;font-size:11px;color:var(--dim);font-weight:400;white-space:nowrap}
.cprev{font-size:12.5px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.conv.unread .cprev{color:var(--txt);font-weight:600}
.conv.unread .cname .ctime{color:var(--accent)}
.odot{min-width:10px;height:10px;border-radius:50%;background:var(--accent);box-shadow:0 0 6px var(--accent)}
.uroom{font-size:10px;color:var(--accent-dim);letter-spacing:.4px}
/* ---------- thread pane ---------- */
main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg)}
#thead{padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel2);display:flex;align-items:center;gap:12px}
#thead .ava{width:38px;height:38px;min-width:38px;font-size:12px}
#thead .tname{font-size:15px;font-weight:700}
#thead .tsub{font-size:12px;color:var(--dim)}
#delgroup{margin-left:auto;background:none;border:1px solid var(--line);color:var(--amber);border-radius:14px;padding:5px 12px;font-size:12px;cursor:pointer;display:none}
#chat{flex:1;overflow-y:auto;padding:18px 16px;display:flex;flex-direction:column;gap:3px}
.day{text-align:center;font-size:11px;color:var(--dim);margin:14px 0 10px}
.day span{background:var(--panel);border:1px solid var(--line);padding:3px 12px;border-radius:10px}
.row{display:flex;gap:8px;align-items:flex-end;margin-top:8px}
.row .ava{width:28px;height:28px;min-width:28px;font-size:10px}
.row.mine{flex-direction:row-reverse}
.msg{max-width:72%;background:var(--bubble);border-radius:16px;padding:8px 13px}
.row.mine .msg{background:var(--mine)}
.msg .who{font-size:11.5px;font-weight:700;margin-bottom:2px}
.msg .body{font-size:14px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.msg .meta{font-size:10.5px;color:var(--dim);margin-top:3px}
.row.mine .msg .meta{color:#c9dcff}
.vbadge{font-size:9.5px;font-weight:600;margin-left:6px;padding:1px 6px;border-radius:8px;vertical-align:1px}
.vbadge.ok{color:#7fd97f;border:1px solid #2a5a2a}
.vbadge.relay{color:var(--amber);border:1px solid #5a4a1a}
.empty{margin:auto;color:var(--dim);font-size:14px;text-align:center;padding:40px}
#composer{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line);background:var(--panel2)}
#composer input{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:20px;color:var(--txt);padding:11px 16px;font-size:14px;outline:none}
#composer input:focus{border-color:var(--accent-dim)}
#composer button{background:var(--mine);border:none;border-radius:20px;color:#fff;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer}
#composer button:active{background:#3a73c4}
footer{padding:6px 16px;border-top:1px solid var(--line);font-size:11px;color:var(--dim);display:flex;justify-content:space-between;background:var(--panel2)}
#backbtn{display:none;background:none;border:none;color:var(--accent);font-size:20px;cursor:pointer;padding:0 4px}
@media (max-width:760px){
  #rail{width:100%;min-width:0;border-right:none}
  main{display:none}
  body.showthread #rail{display:none}
  body.showthread main{display:flex}
  #backbtn{display:block}
}
</style></head><body>
<aside id="rail">
  <header><span class="dot"></span><h1>Mesh</h1><span class="sub" id="railsub">seating…</span></header>
  <input id="search" type="text" placeholder="Search threads" autocomplete="off">
  <button id="newgroup">+ New group thread</button>
  <div id="gpanel">
    <h3>New group — pick 2 or more agents</h3>
    <div id="glist"></div>
    <input id="gname" type="text" placeholder="Group name" autocomplete="off">
    <div class="row2"><button id="gcreate">Create</button><button id="gcancel">Cancel</button></div>
  </div>
  <button id="newagent">+ Add agent from template</button>
  <div id="apanel">
    <h3>Add agent — pick a template</h3>
    <select id="atemplate"></select>
    <div class="adesc" id="adesc"></div>
    <input id="aname" type="text" placeholder="Agent name (blank = template name)" autocomplete="off">
    <input id="amodel" type="text" placeholder="Model override (blank = template default)" autocomplete="off">
    <div id="aerror"></div>
    <div class="row2"><button id="acreate">Add agent</button><button id="acancel">Cancel</button></div>
  </div>
  <div id="convs"><div class="empty">Listening to the mesh…</div></div>
</aside>
<main>
  <div id="thead">
    <button id="backbtn" title="Back to threads">&#8592;</button>
    <div class="ava" id="tava" style="background:#2b5ea7">M</div>
    <div><div class="tname" id="tname">The Mesh</div><div class="tsub" id="tsub">full council room</div></div>
    <button id="delgroup">Delete group</button>
  </div>
  <div id="chat"><div class="empty">Listening to the mesh…</div></div>
  <div id="composer"><input id="box" type="text" placeholder="Message the mesh…" autocomplete="off"><button id="sendbtn" onclick="sendMsg()">Send</button></div>
  <footer><span id="count"></span><span>auto-refresh 5s</span></footer>
</main>
<script>
const NOISE = new Set(["ping","pong","hello","identify","ack"]);
const HUMAN="__HUMAN_ID__";
const CHAT_KINDS = new Set(["bus_node","human","cli_agent","persona","local_agent","ollama_model"]);
let units = [], roster = [], liveIds = new Set();
let sel = {kind:"room"};               // {kind:room} | {kind:agent,id} | {kind:group,gid}
let groups = [];
try{ groups = JSON.parse(localStorage.getItem("meshui.groups")||"[]"); }catch(e){ groups = []; }
let renderKey = "";
function hue(s){let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))%360;return h;}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function initials(id){const p=String(id).replace(/[^a-z0-9 ]/gi," ").split(" ").filter(Boolean);return ((p[0]||"?")[0]+(p[1]?p[1][0]:"")).toUpperCase();}
function nameOf(id){const a=roster.find(x=>x.id===id);return a?a.name:id;}
function groupOf(gid){return groups.find(g=>g.gid===gid);}
function saveGroups(){localStorage.setItem("meshui.groups",JSON.stringify(groups));}
function threadKey(t){return t.kind==="room"?"room":(t.kind==="agent"?"agent:"+t.id:"group:"+t.gid);}
function getSeen(k){return localStorage.getItem("meshui.seen."+k)||"";}
function setSeen(k,ts){localStorage.setItem("meshui.seen."+k,ts);}
function fmtRailTime(ts){
  const d=new Date(ts), now=new Date();
  if(d.toDateString()===now.toDateString()) return d.toLocaleTimeString([], {hour:"numeric",minute:"2-digit"});
  if((now-d)/864e5<7) return d.toLocaleDateString(undefined,{weekday:"short"});
  return d.toLocaleDateString(undefined,{month:"short",day:"numeric"});
}
/* Build display units: merge the operator's fan-out copies (same text, seconds
   apart, different addressees) into one unit carrying all recipients. */
function buildUnits(raw){
  const out=[];
  for(const e of raw){
    const type=String(e.type||"").toLowerCase();
    if(NOISE.has(type)) continue;
    const content=(e.content||e.summary||"").trim(); if(!content) continue;
    const from=e.from||"hub";
    const last=out[out.length-1];
    if(from===HUMAN&&last&&last.from===HUMAN&&last.content===content
       &&Math.abs(new Date(e.ts)-new Date(last.ts))<=15000){
      if(e.to&&!last.recipients.includes(e.to)) last.recipients.push(e.to);
      continue;
    }
    out.push({from,type,content,to:e.to||"",recipients:e.to?[e.to]:[],ts:e.ts||"",badge:e.badge,verified:e.verified});
  }
  // collapse consecutive identical non-operator repeats
  const done=[]; let prev=null, rep=0;
  for(const u of out){
    if(prev&&u.from!==HUMAN&&u.from===prev.from&&u.type===prev.type&&u.content===prev.content){rep++;continue;}
    if(rep>0){done.push({sys:"↑ repeated ×"+(rep+1)});rep=0;}
    done.push(u); prev=u;
  }
  if(rep>0) done.push({sys:"↑ repeated ×"+(rep+1)});
  return done;
}
function sameSet(a,b){ if(a.length!==b.length) return false; const s=new Set(b); return a.every(x=>s.has(x)); }
function unitInThread(u,t){
  if(u.sys) return t.kind==="room";
  if(t.kind==="room") return true;
  if(t.kind==="agent"){
    if(u.from===t.id) return true;
    return u.from===HUMAN&&u.recipients.includes(t.id);
  }
  if(t.kind==="group"){
    const g=groupOf(t.gid); if(!g) return false;
    if(g.members.includes(u.from)) return true;
    return u.from===HUMAN&&u.recipients.length>1&&sameSet(u.recipients,g.members);
  }
  return false;
}
function threadUnits(t){ return units.filter(u=>unitInThread(u,t)); }
function threadTarget(){
  if(sel.kind==="agent") return sel.id;
  if(sel.kind==="group"){ const g=groupOf(sel.gid); return g?g.members.slice():null; }
  return null;
}
async function sendMsg(){
  const box=document.getElementById("box"); const text=box.value.trim(); if(!text) return;
  box.value="";
  try{
    await fetch("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({content:text,to:threadTarget()})});
    const tu=threadUnits(sel); const last=tu[tu.length-1];
    setSeen(threadKey(sel), last?last.ts:new Date().toISOString());
    setTimeout(tick,700);
  }catch(e){}
}
document.getElementById("box").addEventListener("keydown",e=>{if(e.key==="Enter") sendMsg();});
document.getElementById("search").addEventListener("input",()=>renderRail());
document.getElementById("backbtn").addEventListener("click",()=>document.body.classList.remove("showthread"));
document.getElementById("newgroup").addEventListener("click",()=>{
  const p=document.getElementById("gpanel");
  const opening=p.style.display!=="block"; p.style.display=opening?"block":"none";
  if(opening){
    document.getElementById("glist").innerHTML=roster.filter(a=>CHAT_KINDS.has(a.kind)&&a.id!==HUMAN).map(a=>
      '<label><input type="checkbox" class="gmem" value="'+esc(a.id)+'"> '+esc(a.name)+(a.live?" · live":"")+'</label>').join("");
  }
});
document.getElementById("gcancel").addEventListener("click",()=>{document.getElementById("gpanel").style.display="none";});
document.getElementById("gcreate").addEventListener("click",()=>{
  const members=[...document.querySelectorAll(".gmem:checked")].map(x=>x.value);
  if(members.length<2){ alert("Pick at least 2 agents for a group."); return; }
  const name=document.getElementById("gname").value.trim()||members.map(nameOf).join(" + ");
  const g={gid:"g"+Date.now(),name,members};
  groups.push(g); saveGroups();
  document.getElementById("gpanel").style.display="none";
  document.getElementById("gname").value="";
  selectThread({kind:"group",gid:g.gid});
});
document.getElementById("delgroup").addEventListener("click",()=>{
  if(sel.kind!=="group") return;
  const g=groupOf(sel.gid); if(!g) return;
  if(!confirm("Delete group thread '"+g.name+"'? (The mesh tape is untouched.)")) return;
  groups=groups.filter(x=>x.gid!==sel.gid); saveGroups();
  selectThread({kind:"room"});
});
let templates=[];
function showTemplateDesc(){
  const t=templates.find(x=>x.template===document.getElementById("atemplate").value);
  document.getElementById("adesc").textContent=t?(t.description+(t.model?" · model: "+t.model:"")):"";
}
async function loadTemplates(){
  try{
    const r=await fetch("/api/templates"); const d=await r.json();
    templates=d.templates||[];
    const tsel=document.getElementById("atemplate");
    tsel.innerHTML=templates.map(t=>'<option value="'+esc(t.template)+'">'+esc(t.title)+"</option>").join("");
    showTemplateDesc();
  }catch(e){}
}
document.getElementById("atemplate").addEventListener("change",showTemplateDesc);
document.getElementById("newagent").addEventListener("click",()=>{
  const p=document.getElementById("apanel");
  const opening=p.style.display!=="block"; p.style.display=opening?"block":"none";
  if(opening){ document.getElementById("aerror").style.display="none"; loadTemplates(); }
});
document.getElementById("acancel").addEventListener("click",()=>{document.getElementById("apanel").style.display="none";});
document.getElementById("acreate").addEventListener("click",async()=>{
  const err=document.getElementById("aerror"); err.style.display="none";
  const body={template:document.getElementById("atemplate").value,
              name:document.getElementById("aname").value.trim(),
              model:document.getElementById("amodel").value.trim()};
  try{
    const r=await fetch("/api/agents",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(!d.ok){ err.textContent=(d.errors||["add failed"]).join("\n"); err.style.display="block"; return; }
    document.getElementById("apanel").style.display="none";
    document.getElementById("aname").value=""; document.getElementById("amodel").value="";
    await tick();
    selectThread({kind:"agent",id:d.agent.id});
  }catch(e){ err.textContent="Add failed — the UI server could not be reached."; err.style.display="block"; }
});
function selectThread(t){
  sel=t;
  const tu=threadUnits(sel); const last=tu[tu.length-1];
  if(last) setSeen(threadKey(sel),last.ts);
  document.body.classList.add("showthread");
  renderAll(true);
}
function agentThreads(){
  const map=new Map();
  for(const a of roster){ if(CHAT_KINDS.has(a.kind)&&a.id!==HUMAN) map.set(a.id,a); }
  for(const u of units){ if(!u.sys&&!map.has(u.from)&&u.from!==HUMAN&&u.from!=="a2a-hub") map.set(u.from,{id:u.from,name:u.from,kind:""}); }
  return [...map.values()];
}
function railRows(){
  const rows=[];
  const room={kind:"room"};
  const ru=threadUnits(room); const rl=ru[ru.length-1];
  rows.push({t:room,name:"The Mesh",tag:"ROOM",prev:rl?(nameOf(rl.from)+": "+rl.content):"Quiet right now",ts:rl?rl.ts:"",unread:ru.filter(u=>!u.sys&&u.ts>getSeen("room")&&u.from!==HUMAN).length,ava:'<div class="ava" style="background:#2b5ea7">M<div class="live"></div></div>'});
  for(const g of groups){
    const t={kind:"group",gid:g.gid}; const tu=threadUnits(t); const last=tu[tu.length-1];
    rows.push({t,name:g.name,tag:"GROUP · "+g.members.length,prev:last?(nameOf(last.from)+": "+last.content):"No messages yet",ts:last?last.ts:"",
      unread:tu.filter(u=>!u.sys&&u.ts>getSeen(threadKey(t))&&u.from!==HUMAN).length,
      ava:'<div class="ava" style="background:hsl('+hue(g.gid)+',45%,32%)">'+esc(g.members.slice(0,2).map(initials).join("+"))+'</div>'});
  }
  const agents=agentThreads().map(a=>{
    const t={kind:"agent",id:a.id}; const tu=threadUnits(t); const last=tu[tu.length-1];
    return {t,name:a.name,tag:"",prev:last?(last.from===HUMAN?"You: "+last.content:last.content):"No messages yet",ts:last?last.ts:"",
      unread:tu.filter(u=>!u.sys&&u.from===a.id&&u.ts>getSeen(threadKey(t))).length,
      ava:'<div class="ava" style="background:hsl('+hue(a.id)+',45%,32%)">'+esc(initials(a.id))+(liveIds.has(a.id)?'<div class="live"></div>':"")+'</div>'};
  }).sort((x,y)=>(y.ts||"").localeCompare(x.ts||""));
  return rows.concat(agents);
}
function renderRail(){
  const q=(document.getElementById("search").value||"").toLowerCase();
  const rows=railRows().filter(r=>!q||r.name.toLowerCase().includes(q));
  const convs=document.getElementById("convs");
  convs.innerHTML=rows.map((r,i)=>{
    const isSel=threadKey(r.t)===threadKey(sel);
    return '<div class="conv'+(isSel?" sel":"")+(r.unread>0?" unread":"")+'" data-i="'+i+'">'+r.ava+
      '<div class="cmeta"><div class="cname">'+esc(r.name)+(r.tag?' <span class="uroom">'+esc(r.tag)+'</span>':"")+'<span class="ctime">'+(r.ts?fmtRailTime(r.ts):"")+'</span></div>'+
      '<div class="cprev">'+esc((r.prev||"").slice(0,80))+'</div></div>'+
      (r.unread>0?'<div class="odot"></div>':"")+'</div>';
  }).join("")||'<div class="empty">No threads match.</div>';
  convs.querySelectorAll(".conv").forEach(el=>el.addEventListener("click",()=>selectThread(rows[Number(el.dataset.i)].t)));
  const live=roster.filter(a=>a.live).length;
  document.getElementById("railsub").innerHTML=live+" live · "+roster.length+" citizens<br>A2A bus";
}
function renderThread(){
  const chat=document.getElementById("chat");
  const atBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight<80;
  let title="The Mesh", sub="full council room · "+liveIds.size+" live", avaTxt="M", avaBg="#2b5ea7";
  if(sel.kind==="agent"){ title=nameOf(sel.id); sub="direct thread — only "+title+" is addressed"; avaTxt=initials(sel.id); avaBg="hsl("+hue(sel.id)+",45%,32%)"; }
  if(sel.kind==="group"){ const g=groupOf(sel.gid); if(g){ title=g.name; sub="group · "+g.members.map(nameOf).join(", "); avaTxt=g.members.slice(0,2).map(initials).join("+"); avaBg="hsl("+hue(g.gid)+",45%,32%)"; } }
  document.getElementById("tname").textContent=title;
  document.getElementById("tsub").textContent=sub;
  const ta=document.getElementById("tava"); ta.textContent=avaTxt; ta.style.background=avaBg;
  document.getElementById("delgroup").style.display=sel.kind==="group"?"block":"none";
  document.getElementById("box").placeholder=sel.kind==="room"?"Message the mesh… (@Name still works)":("Message "+title+"…");
  const view=threadUnits(sel);
  let html="", lastDay="", shown=0;
  for(const m of view){
    if(m.sys){ html+='<div class="day"><span>'+esc(m.sys)+'</span></div>'; continue; }
    const d=new Date(m.ts), day=d.toLocaleDateString();
    if(day!==lastDay){ lastDay=day; html+='<div class="day"><span>'+d.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"})+'</span></div>'; }
    let badge="";
    if(m.badge==="verified"||m.verified===true) badge=' <span class="vbadge ok" title="verified sender">✓</span>';
    else if(m.badge==="relayed"||m.verified===false) badge=' <span class="vbadge relay" title="sender unverified">relayed</span>';
    const mine=m.from===HUMAN;
    const tos=m.recipients&&m.recipients.length?" → "+m.recipients.map(nameOf).join(", "):(m.to?" → "+nameOf(m.to):"");
    html+='<div class="row'+(mine?" mine":"")+'">'+
      (mine?"":'<div class="ava" style="background:hsl('+hue(m.from)+',45%,32%)">'+esc(initials(m.from))+'</div>')+
      '<div class="msg"><div class="who" style="color:hsl('+hue(m.from)+',80%,72%)">'+esc(nameOf(m.from))+badge+'</div>'+
      '<div class="body">'+esc(m.content.slice(0,4000))+'</div>'+
      '<div class="meta">'+d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})+" · "+esc(m.type)+esc(tos)+'</div></div></div>';
    shown++;
  }
  chat.innerHTML=html||'<div class="empty">Nothing in this thread yet.</div>';
  document.getElementById("count").textContent=shown+" messages";
  if(atBottom) chat.scrollTop=chat.scrollHeight;
}
function renderAll(force){
  const last=units.length?units[units.length-1]:null;
  const key=units.length+"|"+(last?last.ts:"")+"|"+threadKey(sel)+"|"+groups.length+"|"+roster.filter(a=>a.live).length+"|"+(document.getElementById("search").value||"");
  if(!force&&key===renderKey) return;
  renderKey=key; renderRail(); renderThread();
}
async function tick(){
  try{
    const [r,rr]=await Promise.all([fetch("/api/notifications"),fetch("/api/roster")]);
    const data=await r.json();
    units=buildUnits(Array.isArray(data)?data:(data.notifications||[]));
    try{ const rd=await rr.json(); roster=rd.roster||[]; liveIds=new Set(roster.filter(a=>a.live).map(a=>a.id)); }catch(e){}
    renderAll(false);
  }catch(err){ /* keep last render */ }
}
tick(); setInterval(tick,5000);
</script></body></html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        if self.path == "/" or self.path == "/thread":
            body = PAGE.replace("__HUMAN_ID__", HUMAN_ID).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/roster":
            try:
                with urllib.request.urlopen(HUB + "/clients", timeout=10) as r:
                    clients = json.loads(r.read().decode())
                with urllib.request.urlopen(HUB + "/registry", timeout=10) as r:
                    reg = json.loads(r.read().decode())
                live_ids = set()
                cl = clients.get("clients", clients) if isinstance(clients, dict) else clients
                for c in (cl or []):
                    aid = c.get("agent_id")
                    if aid:
                        live_ids.add(aid)
                agents = reg.get("agents", reg) if isinstance(reg, dict) else reg
                roster = []
                for a in (agents or []):
                    aid = a.get("id", "?")
                    roster.append({
                        "id": aid,
                        "name": a.get("name", aid),
                        "kind": a.get("kind", ""),
                        "lane": a.get("lane", ""),
                        "live": aid in live_ids,
                    })
                data = json.dumps({"roster": roster, "live_count": len(live_ids)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(502)
                self.end_headers()
        elif self.path == "/api/notifications":
            try:
                with urllib.request.urlopen(HUB + "/notifications", timeout=10) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_response(502)
                self.end_headers()
        elif self.path == "/api/templates":
            self._templates_json()
        else:
            self.send_response(404)
            self.end_headers()

    def _json_out(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _templates_json(self):
        if _templates is None:
            self._json_out(501, {"ok": False, "errors": ["agent_templates module unavailable"]})
            return
        try:
            rows = _templates.list_templates()
        except _templates.TemplateError as e:
            self._json_out(500, {"ok": False, "errors": e.errors, "warnings": e.warnings})
            return
        self._json_out(200, {"ok": True, "templates": rows})

    def do_POST(self):
        if self.path == "/api/send":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                content = str(payload.get("content", "")).strip()
                # Recipients: payload "to" wins (null = room broadcast,
                # a string = one agent, a list = fan out one copy each).
                # A leading @Name in the text still overrides, as before.
                if "to" in payload:
                    raw_to = payload.get("to")
                    if raw_to is None:
                        recipients = [None]
                    elif isinstance(raw_to, list):
                        recipients = [str(x) for x in raw_to if str(x).strip()] or [None]
                    else:
                        recipients = [str(raw_to)]
                else:
                    recipients = [None]
                    m = re.match(r"^@(\S+)\s+(.+)$", content)
                    if m:
                        recipients, content = [resolve_to(m.group(1))], m.group(2)
                if not content:
                    self.send_response(400); self.end_headers(); return
                delivered = 0
                last_data = b"{}"
                for to in recipients:
                    req = urllib.request.Request(
                        HUB + "/send",
                        data=json.dumps({"from": HUMAN_ID, "type": "thought", "to": to, "content": content, "seat": "thread-ui"}).encode(),
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        last_data = r.read()
                        try:
                            delivered += int(json.loads(last_data.decode() or "{}").get("delivered", 0))
                        except Exception:
                            pass
                data = json.dumps({"ok": True, "copies": len(recipients), "delivered": delivered}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(502)
                self.end_headers()
        elif self.path == "/api/agents":
            self._add_agent()
        else:
            self.send_response(404)
            self.end_headers()

    def _add_agent(self):
        """Add an agent from a template. Validation is loud: a bad
        spec comes back as a 400 with every problem listed, and
        nothing is written — no silent dead seats."""
        if _templates is None:
            self._json_out(501, {"ok": False, "errors": ["agent_templates module unavailable"]})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            self._json_out(400, {"ok": False, "errors": ["request body must be JSON"]})
            return
        template = str(payload.get("template") or "").strip()
        name = str(payload.get("name") or "").strip() or None
        model = str(payload.get("model") or "").strip() or None
        if not template:
            self._json_out(400, {"ok": False, "errors": ["'template' is required"]})
            return
        try:
            entry, _env, warnings = _templates.add_agent(
                template, agent_name=name, model_override=model, registry=REGISTRY_PATH
            )
        except _templates.TemplateError as e:
            self._json_out(400, {"ok": False, "errors": e.errors, "warnings": e.warnings})
            return
        except Exception as e:
            self._json_out(500, {"ok": False, "errors": [f"add failed: {e}"]})
            return
        public = {k: entry.get(k) for k in ("id", "name", "kind", "lane", "template", "inbox", "model")}
        self._json_out(200, {"ok": True, "agent": public, "warnings": warnings})

if __name__ == "__main__":
    HTTPServer((BIND, PORT), Handler).serve_forever()
