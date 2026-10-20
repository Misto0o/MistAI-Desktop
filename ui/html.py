"""
ui/html.py — the pywebview frontend (single HTML string, kept separate from
application logic so it's easy to restyle without touching core/).
"""

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>MistAI</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#050709;--surface:#0b1016;--surface2:#101923;--border:rgba(0,217,255,0.12);
  --accent:#00d9ff;--accent2:#5eead4;--green:#34d399;--amber:#fbbf24;--red:#f87171;
  --text:#dff6ff;--muted:#4c6472;--muted2:#7ea3b3;
}
body{
  font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);
  height:100vh;display:flex;flex-direction:column;overflow:hidden;
  background-image:
    linear-gradient(rgba(0,217,255,0.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,217,255,0.035) 1px, transparent 1px);
  background-size:28px 28px;
}

header{
  padding:10px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;flex-shrink:0;
  background:linear-gradient(180deg, rgba(0,217,255,0.05), transparent);
}
.brand{display:flex;align-items:center;gap:8px}
.brand-name{font-family:'Orbitron',sans-serif;font-size:1.05em;font-weight:700;letter-spacing:.12em;color:var(--accent);text-shadow:0 0 8px rgba(0,217,255,0.5)}
.pill{display:flex;align-items:center;gap:5px;font-size:.68em;font-family:'DM Mono',monospace;color:var(--muted2);background:var(--surface2);border:1px solid var(--border);padding:3px 10px;border-radius:20px}
.pill .dot{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green)}
.clock{font-family:'DM Mono',monospace;font-size:.75em;color:var(--muted2);display:flex;gap:8px;align-items:center;background:var(--surface2);border:1px solid var(--border);padding:5px 14px;border-radius:20px}
.clock b{color:var(--text);font-weight:500}
.header-right{display:flex;align-items:center;gap:10px}
.gear-btn{width:30px;height:30px;border-radius:8px;background:var(--surface2);border:1px solid var(--border);color:var(--muted2);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:.9em;pointer-events:auto;transition:all .2s}
.gear-btn:hover{color:var(--accent);border-color:var(--accent)}

.layout{flex:1;display:flex;overflow:hidden;min-height:0}
.sidebar{width:220px;flex-shrink:0;padding:14px;overflow-y:auto;border-right:1px solid var(--border);display:flex;flex-direction:column;gap:12px}
.sidebar::-webkit-scrollbar{width:3px}
.sidebar::-webkit-scrollbar-thumb{background:rgba(0,217,255,0.15);border-radius:3px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px}
.card-title{display:flex;align-items:center;justify-content:space-between;font-size:.72em;font-weight:600;color:var(--accent2);text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px;font-family:'DM Mono',monospace}
.stat-row{display:flex;justify-content:space-between;font-size:.72em;color:var(--muted2);margin-bottom:6px;font-family:'DM Mono',monospace}
.stat-row b{color:var(--text)}
.mini-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.mini{text-align:center;background:var(--surface2);border-radius:6px;padding:6px 4px}
.mini .v{font-family:'DM Mono',monospace;font-size:.85em;color:var(--accent);font-weight:600}
.mini .l{font-size:.6em;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.toggle-row{display:flex;align-items:center;justify-content:space-between;font-size:.72em;color:var(--muted2);font-family:'DM Mono',monospace;margin-bottom:8px}
.toggle-row:last-child{margin-bottom:0}
.sw{width:30px;height:17px;background:rgba(0,217,255,0.08);border:1px solid var(--border);border-radius:9px;cursor:pointer;position:relative;transition:background .25s;flex-shrink:0;pointer-events:auto}
.sw.on{background:var(--accent);box-shadow:0 0 8px rgba(0,217,255,0.6)}
.sw::after{content:'';position:absolute;width:11px;height:11px;background:#fff;border-radius:50%;top:2px;left:2px;transition:left .25s}
.sw.on::after{left:15px}

.res-link{display:flex;align-items:center;gap:8px;font-size:.72em;color:var(--muted2);font-family:'DM Mono',monospace;text-decoration:none;padding:7px 8px;border-radius:6px;transition:all .2s;margin-bottom:6px;background:var(--surface2);border:1px solid var(--border);cursor:pointer;pointer-events:auto}
.res-link:last-child{margin-bottom:0}
.res-link:hover{color:var(--accent);border-color:var(--accent);box-shadow:0 0 8px rgba(0,217,255,0.15)}
.res-link .ic{font-size:1em}

.center{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:20px;position:relative;min-width:0}

.orb-wrap{width:150px;height:150px;position:relative}
.orb{
  width:100%;height:100%;border-radius:50%;position:relative;
  background:radial-gradient(circle at 35% 30%, #7ff5ff, #00d9ff 45%, #0369a1 100%);
  box-shadow:0 0 30px rgba(0,217,255,0.6), 0 0 70px rgba(0,217,255,0.25), inset 0 0 20px rgba(255,255,255,0.4);
  animation:breathe 3s ease-in-out infinite;
  transition:background .4s, box-shadow .4s;
  display:flex;align-items:center;justify-content:center;
}
.orb-wrap::before{
  content:'';position:absolute;inset:-16px;border-radius:50%;
  border:1px solid rgba(0,217,255,0.35);
  animation:ring-spin 8s linear infinite;
}
.orb-wrap::after{
  content:'';position:absolute;inset:-32px;border-radius:50%;
  border:1px dashed rgba(0,217,255,0.18);
  animation:ring-spin 18s linear infinite reverse;
}
@keyframes breathe{0%,100%{transform:scale(1);filter:brightness(1)}50%{transform:scale(1.04);filter:brightness(1.2)}}
@keyframes ring-spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
.orb.active{
  background:radial-gradient(circle at 35% 30%, #baffc9, #34d399 45%, #047857 100%);
  box-shadow:0 0 30px rgba(52,211,153,0.6), 0 0 70px rgba(52,211,153,0.25), inset 0 0 20px rgba(255,255,255,0.4);
}
.orb.busy{animation:breathe 1.1s ease-in-out infinite, pulse-hot 1.1s ease-in-out infinite}
@keyframes pulse-hot{0%,100%{box-shadow:0 0 30px rgba(0,217,255,0.6),0 0 70px rgba(0,217,255,0.25)}50%{box-shadow:0 0 45px rgba(0,217,255,1),0 0 100px rgba(0,217,255,0.5)}}

.wave{display:flex;align-items:center;gap:3px;height:20px;opacity:0;transition:opacity .3s}
.wave span{width:3px;background:rgba(255,255,255,0.85);border-radius:2px;animation:wave-bar 0.9s ease-in-out infinite}
.wave span:nth-child(1){animation-delay:0s}
.wave span:nth-child(2){animation-delay:.1s}
.wave span:nth-child(3){animation-delay:.2s}
.wave span:nth-child(4){animation-delay:.3s}
.wave span:nth-child(5){animation-delay:.15s}
@keyframes wave-bar{0%,100%{height:4px}50%{height:18px}}

.mist-title{font-family:'Orbitron',sans-serif;font-size:1.6em;font-weight:700;letter-spacing:.15em;color:var(--text);text-shadow:0 0 14px rgba(0,217,255,0.4)}
.mist-status{font-size:.78em;color:var(--muted2);font-family:'DM Mono',monospace;display:flex;align-items:center;gap:6px;background:var(--surface2);border:1px solid var(--border);padding:5px 14px;border-radius:20px}
.mist-status .dot{width:6px;height:6px;border-radius:50%;background:var(--amber);box-shadow:0 0 6px var(--amber)}
.mist-status.ready .dot{background:var(--green);box-shadow:0 0 6px var(--green)}
.mist-status.listening .dot{background:var(--accent);box-shadow:0 0 6px var(--accent)}

.control-bar{display:flex;gap:22px;margin-top:6px}
.control-item{display:flex;flex-direction:column;align-items:center;gap:6px}
.ctrl-btn{width:44px;height:44px;border-radius:12px;background:var(--surface2);border:1px solid var(--border);color:var(--muted2);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1em;pointer-events:auto;transition:all .2s}
.ctrl-btn:hover{border-color:var(--accent);color:var(--accent)}
.ctrl-btn.active{background:var(--accent);color:#04141c;box-shadow:0 0 14px rgba(0,217,255,0.5);border-color:var(--accent)}
.ctrl-label{font-size:.62em;font-family:'DM Mono',monospace;color:var(--muted2);text-transform:uppercase;letter-spacing:.04em;text-align:center}

.convo{width:320px;flex-shrink:0;border-left:1px solid var(--border);display:flex;flex-direction:column;min-height:0}
.convo-head{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.convo-head h3{font-family:'Orbitron',sans-serif;font-size:.85em;color:var(--accent);letter-spacing:.05em}
.btn-clear{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);border-radius:6px;padding:5px 10px;font-size:.68em;font-family:'DM Mono',monospace;cursor:pointer;pointer-events:auto;transition:all .2s;text-transform:uppercase;letter-spacing:.03em}
.btn-clear:hover{color:var(--red);border-color:var(--red)}

#chat{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:6px;scroll-behavior:smooth;min-height:0}
#chat::-webkit-scrollbar{width:3px}
#chat::-webkit-scrollbar-thumb{background:rgba(0,217,255,0.15);border-radius:3px}
.msg{max-width:92%;padding:8px 12px;border-radius:8px;font-size:.82em;line-height:1.5;animation:fadeUp .2s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
.msg.user{background:linear-gradient(135deg,#0e3a52,#0a2540);border:1px solid rgba(0,217,255,0.25);align-self:flex-end;border-radius:10px 10px 2px 10px}
.msg.assistant{background:var(--surface2);border:1px solid var(--border);align-self:flex-start;border-radius:2px 10px 10px 10px}
.msg.system{background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.18);align-self:center;color:var(--amber);font-size:.75em;font-family:'DM Mono',monospace}
.msg.step{background:rgba(0,217,255,.06);border:1px solid var(--border);align-self:flex-start;color:var(--accent2);font-size:.73em;font-family:'DM Mono',monospace;max-width:96%}
.msg.wake{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.18);align-self:center;color:var(--green);font-size:.74em}
.msg.suggestion{background:rgba(0,217,255,.08);border:1px solid var(--border);align-self:flex-end;font-style:italic;font-size:.8em;color:var(--accent2)}
.msg.memory{background:rgba(94,234,212,.06);border:1px solid rgba(94,234,212,.18);align-self:center;font-size:.72em;color:var(--accent2);font-family:'DM Mono',monospace}
.msg.error{background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.25);align-self:center;color:var(--red);font-size:.76em}

.empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;color:var(--muted);text-align:center;padding:0 16px}
.empty p{font-size:.75em;line-height:1.6;color:var(--muted2)}
.empty .hints{margin-top:12px;display:flex;flex-direction:column;gap:4px;width:100%}
.hint-chip{background:var(--surface2);border:1px solid var(--border);padding:6px 10px;border-radius:14px;font-size:.7em;color:var(--muted2);cursor:pointer;transition:all .2s;pointer-events:auto;font-family:'DM Mono',monospace}
.hint-chip:hover{border-color:var(--accent);color:var(--accent);box-shadow:0 0 10px rgba(0,217,255,0.2)}

.step-progress{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin:4px 0;font-size:.72em;font-family:'DM Mono',monospace;color:var(--muted2);align-self:stretch;display:flex;align-items:center;gap:10px}
.step-ring{width:30px;height:30px;flex-shrink:0;position:relative}
.step-ring svg{transform:rotate(-90deg)}
.step-ring circle{fill:none;stroke-width:3}
.step-ring .bg{stroke:rgba(0,217,255,0.1)}
.step-ring .fg{stroke:var(--accent);stroke-linecap:round;transition:stroke-dashoffset .3s ease;filter:drop-shadow(0 0 3px var(--accent))}
.step-ring .n{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.7em;color:var(--accent);font-weight:600}
.step-text{flex:1;min-width:0}
.step-header{color:var(--accent2);margin-bottom:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.step-param{color:var(--muted2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.92em}

.convo-input{padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:8px;flex-shrink:0}
#inp{flex:1;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:.82em;font-family:'DM Sans',sans-serif;transition:border-color .2s, box-shadow .2s;pointer-events:auto;min-width:0}
#inp:focus{outline:none;border-color:var(--accent);box-shadow:0 0 10px rgba(0,217,255,0.2)}
#inp::placeholder{color:var(--muted)}
#inp:disabled{opacity:.4}
.btn-send{width:34px;height:34px;background:var(--accent);border:none;border-radius:8px;color:#04141c;font-size:13px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s;flex-shrink:0;pointer-events:auto;box-shadow:0 0 10px rgba(0,217,255,0.4)}
.btn-send:hover:not(:disabled){background:var(--accent2);box-shadow:0 0 16px rgba(0,217,255,0.6)}
.btn-send:disabled{opacity:.35;cursor:not-allowed;box-shadow:none}
#btn-stop{background:var(--red);color:#2c0a0a;box-shadow:0 0 10px rgba(248,113,113,0.4)}
#btn-stop:hover{background:#fca5a5;box-shadow:0 0 16px rgba(248,113,113,0.6)}

.modal{display:none;position:fixed;inset:0;background:rgba(0,5,8,0.8);z-index:1000;align-items:center;justify-content:center;backdrop-filter:blur(2px)}
.modal.open{display:flex}
.modal-content{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;max-width:400px;width:90%;animation:slideUp .3s ease;box-shadow:0 0 30px rgba(0,217,255,0.15)}
@keyframes slideUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.modal-title{font-family:'Orbitron',sans-serif;font-size:1em;font-weight:700;margin-bottom:14px;color:var(--accent);letter-spacing:.04em}
.modal-input{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:.86em;font-family:inherit;margin-bottom:12px}
.modal-buttons{display:flex;gap:8px}
.btn-save{flex:1;background:var(--accent);color:#04141c;border:none;padding:8px;border-radius:6px;cursor:pointer;font-size:.8em;font-weight:600}
.btn-save:hover{background:var(--accent2)}
.btn-cancel{flex:1;background:var(--surface2);color:var(--text);border:1px solid var(--border);padding:8px;border-radius:6px;cursor:pointer;font-size:.8em}
.btn-cancel:hover{border-color:var(--accent)}
.card-note{font-size:.66em;color:var(--muted2);font-family:'DM Mono',monospace;margin-top:6px}
.card-note.error{color:var(--red)}
.card-note:empty{display:none}
select{background:var(--surface2);color:var(--text);border:1px solid var(--border);padding:6px 8px;border-radius:6px;font-size:.78em;cursor:pointer;font-family:'DM Mono',monospace;pointer-events:auto;width:100%;margin-top:6px}
</style>
</head>
<body>

<header>
  <div class="brand">
    <span class="brand-name">M.I.S.T.</span>
    <div class="pill"><div class="dot" id="online-dot"></div><span id="online-text">Checking...</span></div>
  </div>
  <div class="clock"><b id="clock-time">--:--:--</b><span id="clock-date">-- -- ----</span></div>
  <div class="header-right">
    <button class="gear-btn" onclick="openAPIModal()" title="Settings">&#9881;</button>
  </div>
</header>

<div class="layout">
  <div class="sidebar">
    <div class="card">
      <div class="card-title">Session Stats</div>
      <div class="stat-row"><span>Messages</span><b id="stat-msgs">0</b></div>
      <div class="stat-row"><span>Actions Run</span><b id="stat-actions">0</b></div>
      <div class="mini-grid">
        <div class="mini"><div class="v" id="stat-facts">0</div><div class="l">Facts</div></div>
        <div class="mini"><div class="v" id="stat-reminders">0</div><div class="l">Reminders</div></div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">System</div>
      <div class="toggle-row"><span>Wake Word</span><div class="sw" id="sw-wake"></div></div>
      <div class="toggle-row"><span>Captions</span><div class="sw" id="sw-cap"></div></div>
    </div>

    <div class="card">
      <div class="card-title">Resources</div>
      <div class="res-link" onclick="openLink('https://mistai.org/docs')"><span class="ic">&#128214;</span>Docs & Get API Key</div>
      <div class="res-link" onclick="openLink('https://github.com/Misto0o/MistAI-Desktop')"><span class="ic">&#9733;</span>GitHub Repo</div>
    </div>

    <div class="card">
      <div class="card-title">Gender Address</div>
      <select id="sel-gender"><option value="none">Neutral</option><option value="male">Sir</option><option value="female">Ma'am</option></select>
    </div>

    <div class="card">
      <div class="card-title">Model</div>
      <!-- Options come from Api.get_model_options() (config.MODEL_CHOICES). -->
      <select id="sel-model"></select>
      <div class="card-note" id="model-note">Loading models...</div>
    </div>
  </div>

  <div class="center">
    <div class="orb-wrap">
      <div class="orb" id="orb">
        <div class="wave" id="wave"><span></span><span></span><span></span><span></span><span></span></div>
      </div>
    </div>
    <div class="mist-title">M.I.S.T.</div>
    <div class="mist-status ready" id="mist-status"><div class="dot"></div><span id="mist-status-text">Ready</span></div>

    <div class="control-bar">
      <div class="control-item">
        <button class="ctrl-btn" id="ctrl-wake" title="Toggle wake word listening">&#127908;</button>
        <div class="ctrl-label">Wake Word</div>
      </div>
      <div class="control-item">
        <button class="ctrl-btn" id="ctrl-caption" title="Toggle on-screen captions">&#128190;</button>
        <div class="ctrl-label">Captions</div>
      </div>
    </div>
  </div>

  <div class="convo">
    <div class="convo-head">
      <h3>CONVERSATION</h3>
      <button class="btn-clear" onclick="clearChat()">Clear Chat</button>
    </div>
    <div id="chat">
      <div class="empty" id="empty">
        <p>JARVIS-style desktop control — UIA navigation, browser automation, and system-level actions.</p>
        <div class="hints">
          <div class="hint-chip" onclick="fillAndSend(this)">Open Firefox and go to YouTube</div>
          <div class="hint-chip" onclick="fillAndSend(this)">List all my open windows</div>
          <div class="hint-chip" onclick="fillAndSend(this)">Take a screenshot and save it</div>
          <div class="hint-chip" onclick="fillAndSend(this)">What's in my clipboard?</div>
        </div>
      </div>
    </div>
    <div class="convo-input">
      <input id="inp" type="text" placeholder="Tell MistAI what to do..." autocomplete="off"/>
      <button class="btn-send" id="btn-send">&#9654;</button>
      <button class="btn-send" id="btn-stop">■</button>
    </div>
  </div>
</div>

<div class="modal" id="apiModal">
  <div class="modal-content">
    <div class="modal-title">API CONFIGURATION</div>
    <input type="text" class="modal-input" id="apiKeyInput" placeholder="Enter your MistAI API key (mistai_...)"/>
    <label id="backend-row" style="font-size:.75em;color:var(--muted);margin-bottom:12px;display:none">
      <input type="checkbox" id="useBackend" checked/> Use MistAI backend
    </label>
    <div class="modal-buttons">
      <button class="btn-save" onclick="saveAPIKey()">Save</button>
      <button class="btn-cancel" onclick="closeAPIModal()">Cancel</button>
    </div>
  </div>
</div>

<script>
let busy=false,wakeOn=false,capOn=false,ready=false;
// sendInFlight: a UI-initiated ask_mistai() promise is pending. While true,
// the periodic agent_busy poll must not flip the UI idle -- the promise's own
// finally() is the authority for UI-initiated tasks. For VOICE-initiated tasks
// (no promise in flight) the poll is the only source of truth.
let sendInFlight=false,stopping=false;
const chat=document.getElementById('chat'),inp=document.getElementById('inp');
const btnSend=document.getElementById('btn-send'),orb=document.getElementById('orb');
const swWake=document.getElementById('sw-wake');
const swCap=document.getElementById('sw-cap'),wave=document.getElementById('wave');
const selGender=document.getElementById('sel-gender'),selModel=document.getElementById('sel-model');
const apiModal=document.getElementById('apiModal');
const mistStatus=document.getElementById('mist-status'),mistStatusText=document.getElementById('mist-status-text');
const ctrlWake=document.getElementById('ctrl-wake'),ctrlCaption=document.getElementById('ctrl-caption');
let stepEl=null;

function tickClock(){
  const d=new Date();
  document.getElementById('clock-time').textContent=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  document.getElementById('clock-date').textContent=d.toLocaleDateString([],{month:'long',day:'numeric',year:'numeric'});
}
tickClock();setInterval(tickClock,1000);

function openLink(url){
  window.open(url, '_blank');
}

// Startup. All state lives in Python (memory.db): pywebview loads this page
// with NavigateToString, an opaque origin where every browser storage API
// throws (tests/test_ui_model_card.py checks none is used). The intervals start first, and each step runs on its own
// (not awaited one after another), so a step that throws or never answers
// cannot stop the others.
async function runStep(name,step){
  try{await step();}catch(e){console.error('[startup] '+name+' failed:',e);}
}
window.addEventListener('pywebviewready',()=>{
  ready=true;
  setInterval(updateStats,4000);
  setInterval(checkStatus,20000);
  setInterval(syncBusy,1500);
  runStep('flags',loadFlags);
  runStep('gender',loadGender);
  runStep('models',loadModels);
  runStep('stats',updateStats);
  runStep('status',checkStatus);
});

// Developer-only controls stay hidden (and unused) unless Python says this is
// a dev run (Api.get_ui_flags); if the flags never load, the page acts as a build.
let devMode=false;
async function loadFlags(){
  const f=await pywebview.api.get_ui_flags();
  devMode=!!(f&&f.is_dev===true);
  document.getElementById('backend-row').style.display=devMode?'block':'none';
}
async function loadGender(){
  selGender.value=await pywebview.api.get_user_gender();
}
selGender.onchange=async e=>{
  if(!ready)return;
  try{
    const r=await pywebview.api.set_user_gender(e.target.value);
    if(!(r&&r.success)){addMsg((r&&r.error)||'Could not save the gender address','error');await loadGender();}
  }catch(err){console.error('[gender] save failed:',err);addMsg('Could not save the gender address: '+err,'error');}
};

// The model choice lives in Python; the UI shows labels and sends ids. A
// running request keeps its model.
const modelNote=document.getElementById('model-note');
function setModelNote(text,isError){
  modelNote.textContent=text;
  modelNote.className='card-note'+(isError?' error':'');
}
async function loadModels(){
  try{
    const o=await pywebview.api.get_model_options();
    const choices=(o&&Array.isArray(o.choices))?o.choices:[];
    if(!choices.length)throw new Error('no models were returned');
    selModel.innerHTML='';
    for(const c of choices){
      const opt=document.createElement('option');
      opt.value=String(c.id);opt.textContent=String(c.label||c.id);
      selModel.appendChild(opt);
    }
    selModel.value=o.current;
    setModelNote('',false);
  }catch(e){
    console.error('[models] could not load the model list:',e);
    setModelNote('Could not load the model list: '+((e&&e.message)||e),true);
  }
}
selModel.onchange=async e=>{
  if(!ready)return;
  try{
    const r=await pywebview.api.set_model(e.target.value);
    if(r&&r.success){
      addMsg('Model: '+(r.label||r.model)+' (applies from the next request)','system');
    }else{
      addMsg((r&&r.error)||'Could not change the model','error');
      await loadModels();   // show the model that is really in use
    }
  }catch(err){
    console.error('[models] set_model failed:',err);
    addMsg('Error: '+err,'error');await loadModels();
  }
};

function setMistStatus(state,text){
  mistStatus.className='mist-status '+state;
  mistStatusText.textContent=text;
}

async function toggleWake(){
  wakeOn=!wakeOn;
  swWake.classList.toggle('on',wakeOn);ctrlWake.classList.toggle('active',wakeOn);
  orb.classList.toggle('active',wakeOn);
  if(ready)try{
    if(wakeOn){await pywebview.api.start_wake_word_detection();setMistStatus('listening','Listening for wake word...');}
    else{await pywebview.api.stop_wake_word_detection();setMistStatus('ready','Ready');}
    addMsg('Wake: '+(wakeOn?'active':'off'),'system');
  }catch(e){console.error('Wake toggle error:',e);addMsg('Wake error: '+e,'error');}
}
swWake.onclick=toggleWake;
ctrlWake.onclick=toggleWake;

async function toggleCaptions(){
  capOn=!capOn;
  swCap.classList.toggle('on',capOn);ctrlCaption.classList.toggle('active',capOn);
  if(ready)try{
    await pywebview.api.toggle_captions(capOn);
    addMsg(capOn?'Captions on':'Captions off','system');
  }catch(e){console.error('Caption toggle error:',e);addMsg('Caption error: '+e,'error');}
}
swCap.onclick=toggleCaptions;
ctrlCaption.onclick=toggleCaptions;

function showAskUserPrompt(question){
  addMsg(question, 'assistant');
  inp.placeholder = "Type your answer...";
  inp.focus();
  // Unlock the input so the user can answer, but the agent is still
  // running -- keep the orb/status showing that.
  busy=false;
  btnSend.disabled=inp.disabled=false;
  setMistStatus('listening','Waiting for your answer...');
  inp.dataset.answering = "true";
}

function openAPIModal(){apiModal.classList.add('open');}
function closeAPIModal(){apiModal.classList.remove('open');}
async function saveAPIKey(){
  if(!ready)return;
  const key=document.getElementById('apiKeyInput').value.trim();
  const useBackend=document.getElementById('useBackend').checked;
  if(key){
    try{
      await pywebview.api.set_api_key(key);
      addMsg('API key configured','system');
    }catch(e){addMsg('Error: '+e,'error');}
  }
  // Dev runs only: a saved API key plus an unchecked box means direct
  // authenticated API mode. In a build the route stays what
  // MISTAI_USE_BACKEND chose; Save only sets the key.
  if(devMode && (key || useBackend)){
    try{
      const r=await pywebview.api.set_api_mode(useBackend);
      if(r&&r.success===false)addMsg(r.error||'Could not change the API route','error');
      else addMsg(useBackend?'Using MistAI backend':'Using direct API mode (agent-compatible endpoint required)','system');
    }catch(e){addMsg('Error: '+e,'error');}
  }
  closeAPIModal();
}

btnSend.onclick=send;
inp.onkeypress=e=>{if(e.key==='Enter'&&!busy)send()};

function fillAndSend(el){
  inp.value=el.textContent;send();
}

function clearChat(){
  chat.innerHTML='';
  const e=document.createElement('div');
  e.className='empty';e.id='empty';
  e.innerHTML='<p>Conversation cleared.</p>';
  chat.appendChild(e);
}

async function send() {
  if (!ready || !window.pywebview) return;

  const msg = inp.value.trim();
  if (!msg || busy) return;

  if (typeof pywebview.api.ask_mistai !== 'function') {
    addMsg('System not fully ready yet — try again in a second.', 'error');
    return;
  }

  if (inp.dataset.answering === "true") {
    inp.dataset.answering = "false";
    inp.placeholder = "Tell MistAI what to do...";
    addMsg(msg, 'user');
    inp.value = '';

    // Hand the answer to the (still running) agent. Do NOT go idle after:
    // the original send()'s promise is still pending and its finally()
    // will clear busy when the task really ends.
    setBusy(true);
    try {
      await pywebview.api.submit_answer(msg);
    } catch(e) {
      addMsg('Error: ' + e, 'error');
    }
    return;
  }

  inp.value = '';
  setBusy(true);
  sendInFlight = true;
  addMsg(msg, 'user');
  stepEl = null;

  try {
    // No client-side timeout: the backend now waits for the agent to REALLY
    // finish (or reports still_running / busy honestly). A UI timer that
    // gave up after 130s used to unlock the input while the loop was still
    // driving the mouse -- which is how two agents ended up running at once.
    // model + gender are resolved on the Python side (Api.current_model(),
    // set from the Model card, and the saved profile) -- the UI passes null.
    const res = await pywebview.api.ask_mistai(msg, null, null);
    stepEl = null;

    if (res.success) {
      if (res.command && res.command.speech) {
        addMsg(res.command.speech, 'assistant');
        flashWave();
      }
      if (res.steps) addMsg(`Completed in ${res.steps} step${res.steps !== 1 ? 's' : ''}`, 'memory');
      updateStats();
    } else if (res.error === 'busy') {
      addMsg(res.command && res.command.speech ? res.command.speech : 'Mist is still working on a previous task.', 'system');
    } else if (res.error === 'still_running') {
      addMsg('Still working on this in the background — the status will update when it finishes.', 'system');
    } else {
      addMsg('Error: ' + (res.error || 'Unknown'), 'error');
    }
  } catch(e) {
    addMsg('Error: ' + e, 'error');
  } finally {
    sendInFlight = false;
    // Only go idle if the backend agrees nothing is running.
    syncBusy();
    inp.focus();
  }
}

function setBusy(b){
  busy=b;
  btnSend.disabled=inp.disabled=b;
  orb.classList.toggle('busy',b);
  if(b) setMistStatus('listening', stopping ? 'Stopping...' : 'Working...');
  else if(!wakeOn) setMistStatus('ready','Ready');
  else setMistStatus('listening','Listening for wake word...');
}

// Reconcile the UI with the runtime's actual agent state. Voice-initiated
// tasks have no promise in flight, so this is the only way the UI learns
// about them; for UI-initiated tasks it also confirms a Stop has completed.
async function syncBusy(){
  if(!ready)return;
  try{
    const s=await pywebview.api.is_agent_busy();
    if(s.busy){
      if(!busy && inp.dataset.answering!=="true") setBusy(true);
    }else{
      stopping=false;
      if(busy && !sendInFlight) setBusy(false);
      if(inp.dataset.answering==="true"){inp.dataset.answering="false";inp.placeholder="Tell MistAI what to do...";}
    }
  }catch(e){}
}

function flashWave(){
  wave.style.opacity='1';
  setTimeout(()=>wave.style.opacity='0', 2200);
}

function addMsg(text,type){
  const e=document.getElementById('empty');
  if(e)e.remove();
  const d=document.createElement('div');
  d.className='msg '+type;d.textContent=text;
  chat.appendChild(d);chat.scrollTop=chat.scrollHeight;
  return d;
}

function handleAgentStep(n,action,param,speech){
  if(!stepEl){
    stepEl=document.createElement('div');
    stepEl.className='step-progress';
    const e=document.getElementById('empty');if(e)e.remove();
    chat.appendChild(stepEl);
    // Static markup only. Action/parameter text is assigned with textContent
    // below because both can originate from a model response.
    stepEl.innerHTML=
      '<div class="step-ring">'+
        '<svg width="30" height="30" viewBox="0 0 30 30">'+
          '<circle class="bg" cx="15" cy="15" r="13"></circle>'+
          '<circle class="fg" cx="15" cy="15" r="13"></circle>'+
        '</svg>'+
        '<div class="n"></div>'+
      '</div>'+
      '<div class="step-text">'+
        '<div class="step-header"></div>'+
        '<div class="step-param"></div>'+
      '</div>';
  }
  const stepNumber=Math.max(0,Number.parseInt(n,10)||0);
  const pct=Math.min(stepNumber*15,95);
  const r=13,c=2*Math.PI*r,offset=c-(pct/100)*c;
  const fg=stepEl.querySelector('.fg');
  fg.setAttribute('stroke-dasharray',String(c));
  fg.setAttribute('stroke-dashoffset',String(offset));
  stepEl.querySelector('.n').textContent=String(stepNumber);
  stepEl.querySelector('.step-header').textContent=String(action||'');
  stepEl.querySelector('.step-param').textContent=param?String(param).substring(0,50):'';
  chat.scrollTop=chat.scrollHeight;
}

function handleWakeWordDetected(){addMsg('Listening...','wake');setMistStatus('listening','Heard wake word...');}
function handleVoiceResult(text){
  inp.value=String(text||'');
  send();
}
function handleVoiceError(message){addMsg('Voice: '+String(message||'Could not listen'),'error');}
function handleVoiceEnd(){}
function handleTtsError(message){addMsg('Speech: '+String(message||'Voice output unavailable'),'error');}

async function checkStatus(){
  if(!ready)return;
  try{
    const r=await pywebview.api.check_api_status();
    document.getElementById('online-text').textContent=r.online?'Online':'Offline';
    document.getElementById('online-dot').style.background=r.online?'var(--green)':'var(--red)';
    document.getElementById('online-dot').style.boxShadow=r.online?'0 0 6px var(--green)':'0 0 6px var(--red)';
  }catch(e){
    console.error('[status] check failed:',e);
    document.getElementById('online-text').textContent='Unknown';
    document.getElementById('online-dot').style.background='var(--amber)';
  }
}

async function updateStats(){
  if(!ready)return;
  try{
    const s=await pywebview.api.get_memory_stats();
    document.getElementById('stat-msgs').textContent=s.conversation_length;
    document.getElementById('stat-actions').textContent=s.actions_performed;
    document.getElementById('stat-facts').textContent=s.facts_remembered;
    document.getElementById('stat-reminders').textContent=s.pending_reminders;
  }catch(e){console.error('[stats] update failed:',e);}
}

const btnStop = document.getElementById('btn-stop');
btnStop.onclick = async () => {
  const r = await pywebview.api.stop_current_task();
  if (r && r.was_running) {
    // The loop halts at its next step boundary; it may be mid LLM call.
    // Stay busy (showing "Stopping...") until is_agent_busy() confirms.
    stopping = true;
    setBusy(true);
    addMsg('Stopping after the current step...', 'system');
  } else {
    addMsg('Nothing running.', 'system');
    setBusy(false);
  }
};

</script>
</body>
</html>"""
