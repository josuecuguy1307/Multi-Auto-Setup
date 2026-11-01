"""Per-host wrappers around the cards. A card is the same everywhere; only the door differs.

* `panel`      the owner's screen (loopback, no bridge)
* `openwebui`  a Rich UI embed: the iframe cannot read its own height, so it posts `iframe:height` to its parent (docs.openwebui.com, Rich UI)
* `mcp`        an MCP App view: a static shell that speaks the MCP Apps `postMessage` JSON-RPC (`ui/initialize`, `ui/notifications/tool-result`, `tools/call`, `size-changed`) and draws the
               cards the tool result carries (`structuredContent.cards[].html`)
* `plain`      no bridge (tests, screenshots)

Every document says, in its own Content-Security-Policy, that it loads nothing: no network, no frames, no images but inline ones."""

from __future__ import annotations

from envrec_harness.cards import CSS, esc
from envrec_harness.strings import t

HOSTS = ("panel", "openwebui", "mcp", "plain")
CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"

BEHAVIOR_JS = """
(function(){
  var openId=null;
  function details(){return document.querySelectorAll('.mas-detail');}
  function toggle(id){var open=false;details().forEach(function(d){if(d.getAttribute('data-for')===id&&d.hidden){d.hidden=false;open=true;}else{d.hidden=true;}});openId=open?id:null;report();return open;}
  document.addEventListener('click',function(e){
    var n=e.target.closest&&e.target.closest('[data-node]');if(n){toggle(n.getAttribute('data-node'));return;}
    var c=e.target.closest&&e.target.closest('[data-choose]');
    if(c&&window.__mas&&typeof window.__mas.choose==='function'){c.disabled=true;window.__mas.choose(c.getAttribute('data-intent'),c.getAttribute('data-choose'));}
  });
  document.addEventListener('keydown',function(e){
    var n=e.target.closest&&e.target.closest('[data-node]');
    if(n&&(e.key==='Enter'||e.key===' ')){e.preventDefault();toggle(n.getAttribute('data-node'));}
    if(e.key==='Escape'){details().forEach(function(d){d.hidden=true;});openId=null;report();}
  });
  function report(){if(typeof window.__mas_report==='function'){window.__mas_report();}}
  window.__mas_toggle=toggle;
  window.__mas_reopen=function(){if(openId){details().forEach(function(d){d.hidden=d.getAttribute('data-for')!==openId;});report();}};  // a refreshed map keeps the machine card the owner had open
  if(typeof ResizeObserver==='function'){new ResizeObserver(report).observe(document.documentElement);}
  window.addEventListener('load',report);
})();
"""

OPENWEBUI_JS = """
window.__mas_report=function(){var h=Math.ceil(document.documentElement.scrollHeight);parent.postMessage({type:'iframe:height',height:h},'*');};
"""

MCP_JS = """
(function(){
  var pending={},next=0,root=document.getElementById('root'),initialized=false,slots={},order=[],timer=null,ctx={},armed=null,drawn=false;
  function send(m){parent.postMessage(m,'*');}
  function rpc(method,params){return new Promise(function(res,rej){var id=++next;pending[id]={res:res,rej:rej};send({jsonrpc:'2.0',id:id,method:method,params:params||{}});});}
  function note(method,params){send({jsonrpc:'2.0',method:method,params:params||{}});}
  function say(text){root.innerHTML='<div class="mas card"><p class="sub tight">'+String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</p></div>';report();}  // a view that cannot do its job says so on screen: an empty frame is invisible
  function height(){var h=document.documentElement,old=h.style.height;h.style.height='max-content';var v=Math.ceil(h.getBoundingClientRect().height);h.style.height=old;return v;}  // the content's own height, as the SDK measures it (scrollHeight never shrinks)
  function report(){if(!initialized){return;}note('ui/notifications/size-changed',{width:Math.ceil(window.innerWidth||document.documentElement.scrollWidth||0),height:Math.max(1,height()||0)});}
  window.__mas_report=report;
  function paint(){root.innerHTML=order.map(function(k){return slots[k];}).join('');if(window.__mas_reopen){window.__mas_reopen();}report();}
  function put(cards){cards.forEach(function(c){var k=String(c.slot||c.intent_id);if(!(k in slots)){order.push(k);}slots[k]=String(c.html||'');});paint();}
  function schedule(active,ms){
    if(!active){if(timer){clearInterval(timer);timer=null;}return;}
    if(timer){return;}
    timer=setInterval(function(){if(document.hidden||!initialized){return;}rpc('tools/call',{name:'mas_live',arguments:{}}).then(draw,function(){});},ms);  // the view refreshes itself while a run is active
  }
  function draw(result){
    var m=(result&&result._meta)||{},cs=m['mas/cards'];
    if(Array.isArray(cs)&&cs.length){drawn=true;put(cs);}
    else if(!drawn){say(@@no_cards@@);}
    var lv=m['mas/live'];schedule(!!(lv&&lv.active),((lv&&lv.every_s)||3)*1000);
  }
  window.__mas={host:null,choose:function(intentId,option){
    return rpc('tools/call',{name:'mas_answer',arguments:{intent_id:intentId,option:option}}).then(draw,function(){});
  }};
  function evidence(extra){
    var c=ctx||{};
    rpc('tools/call',{name:'mas_view',arguments:{context:Object.assign({displayMode:String(c.displayMode||''),availableDisplayModes:Array.isArray(c.availableDisplayModes)?c.availableDisplayModes.map(String):[],theme:String(c.theme||''),platform:String(c.platform||''),viewStarted:true},extra||{})}}).then(function(){},function(){});
  }
  window.addEventListener('message',function(e){
    if(e.source!==parent){return;}
    var m=e.data;if(!m||m.jsonrpc!=='2.0'){return;}
    if(m.id!==undefined&&(('result' in m)||m.error)){var p=pending[m.id];if(p){delete pending[m.id];m.error?p.rej(m.error):p.res(m.result);}return;}
    if(m.method==='ui/notifications/tool-result'){clearTimeout(armed);draw(m.params);}
    if(m.method==='ui/notifications/host-context-changed'){ctx=Object.assign(ctx,m.params||{});}
    if(m.method==='ui/resource-teardown'&&m.id!==undefined){send({jsonrpc:'2.0',id:m.id,result:{}});}
  });
  say(@@connecting@@);
  armed=setTimeout(function(){if(!initialized){say(@@no_init@@);}},6000);
  rpc('ui/initialize',{protocolVersion:'2026-01-26',appInfo:{name:'mas-cards',version:'1'},appCapabilities:{}}).then(function(r){
    window.__mas.host=r&&r.hostInfo||null;ctx=(r&&r.hostContext)||{};initialized=true;note('ui/notifications/initialized',{});report();
    armed=setTimeout(function(){if(!drawn){say(@@no_result@@);}},8000);
    try{
      var avail=Array.isArray(ctx.availableDisplayModes)?ctx.availableDisplayModes:[];
      if(ctx.displayMode&&ctx.displayMode!=='inline'&&avail.indexOf('inline')>=0){  // the live view belongs in the conversation: ask for inline, and say what the host answered
        rpc('ui/request-display-mode',{mode:'inline'}).then(function(res){evidence({requestedInline:true,grantedMode:String((res&&res.mode)||'')});},function(){evidence({requestedInline:true,grantedMode:'refused'});});
      }else{evidence({requestedInline:false});}
    }catch(err){evidence({error:String(err&&err.message||err).slice(0,80)});}
  },function(err){clearTimeout(armed);say(@@refused@@+String((err&&err.message)||err).slice(0,120));});
})();
"""


import json as _json

MCP_JS = (MCP_JS.replace("@@connecting@@", _json.dumps(t("en", "shell_connecting"))).replace("@@no_init@@", _json.dumps(t("en", "shell_no_init")))
          .replace("@@no_result@@", _json.dumps(t("en", "shell_no_result"))).replace("@@refused@@", _json.dumps(t("en", "shell_refused"))).replace("@@no_cards@@", _json.dumps(t("en", "shell_no_cards"))))  # the shell's words are the strings module's, written into the script once


def document(fragment: str, host: str = "plain", lang: str = "en", title: str = "Multi Auto Setup") -> str:
    """A complete, self-contained HTML document for `host`. For `mcp` the body is the empty shell (the cards arrive in the tool result); `fragment` is drawn first if given."""
    if host not in HOSTS:
        raise ValueError(f"unknown host {host!r}")
    bridge = OPENWEBUI_JS if host == "openwebui" else ""
    root = f'<div id="root">{fragment}</div>' if host == "mcp" else fragment  # (the MCP shell fills its own root at once: an empty frame has no height and is invisible)
    tail = MCP_JS if host == "mcp" else ""
    return (f'<!doctype html><html lang="{esc(lang)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}"><title>{esc(title)}</title><style>{CSS}</style></head><body>{root}<script>{bridge}{BEHAVIOR_JS}{tail}</script></body></html>')


FRAME_CSP = "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"


def linked_document(fragment: str, lang: str = "en", title: str = "Multi Auto Setup") -> str:
    """The same cards for a frame INSIDE the owner's screen: style and behavior come from two files of the same origin (`/cards.css`, `/cards.js`), because that screen allows nothing inline."""
    return (f'<!doctype html><html lang="{esc(lang)}" class="framed"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<link rel="stylesheet" href="/cards.css"></head><body>{fragment}<script src="/cards.js"></script></body></html>')
