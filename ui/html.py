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
  --text:#dff6ff;--muted:#6b8796;--muted2:#7ea3b3;
  /* --muted was #4c6472 (2.9:1 on surface2); #6b8796 is 4.7:1 (WCAG AA). */
  --radius-sm:6px;--radius:8px;--radius-lg:12px;--pill:20px;
  --focus:0 0 0 2px var(--bg),0 0 0 4px var(--accent);
  --ease:.2s;
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

/* The orb: the shared look (ui/html.py orb_css, the same as the floating
   orb's page) sized for this page; the disc's green rim says the wake word is on. */
.orb-wrap{width:150px;height:150px;position:relative;--s:150px}
.orb-wrap .disc.active{border-color:rgba(140,236,180,.9)}
/*%ORB_CSS%*/

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
.msg.user.voice::before{content:'🎤';margin-right:5px;opacity:.8}
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
.msg.confirm .confirm-buttons{display:flex;gap:8px;margin-top:8px}
.btn-cancel{flex:1;background:var(--surface2);color:var(--text);border:1px solid var(--border);padding:8px;border-radius:6px;cursor:pointer;font-size:.8em}
.btn-cancel:hover{border-color:var(--accent)}
.card-note{font-size:.66em;color:var(--muted2);font-family:'DM Mono',monospace;margin-top:6px}
.card-note.error{color:var(--red)}
.card-note:empty{display:none}
select{background:var(--surface2);color:var(--text);border:1px solid var(--border);padding:6px 8px;border-radius:6px;font-size:.78em;cursor:pointer;font-family:'DM Mono',monospace;pointer-events:auto;width:100%;margin-top:6px}

/* Shared states: one keyboard focus ring for everything focusable, the app
   font and the same easing on every text button, a visible pressed state,
   and switches that show their dependency. */
:focus{outline:none}
:focus-visible{outline:none;box-shadow:var(--focus)}
#inp:focus-visible,.modal-input:focus-visible{border-color:var(--accent);box-shadow:var(--focus)}
.btn-save,.btn-cancel{font-family:'DM Mono',monospace;transition:all var(--ease)}
.btn-save:disabled,.btn-cancel:disabled{opacity:.4;cursor:not-allowed}
button:active:not(:disabled),.sw:active,.hint-chip:active,.res-link:active{transform:translateY(1px)}
.modal-input,select{transition:border-color var(--ease)}
.modal-input:hover,select:hover{border-color:rgba(0,217,255,0.3)}
.sw:hover{border-color:rgba(0,217,255,0.35)}
.toggle-row.inactive{opacity:.5}
.modal-hint{font-size:.66em;color:var(--muted2);font-family:'DM Mono',monospace;margin:-6px 0 12px}
.sr-only{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
@media (prefers-reduced-motion: reduce){
  .msg,.modal-content{animation:none !important}
  #chat{scroll-behavior:auto}
  *,*::before,*::after{transition:none !important}
  button:active:not(:disabled),.sw:active,.hint-chip:active,.res-link:active{transform:none}
}
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
    <button class="gear-btn" id="btn-settings" onclick="openAPIModal()" title="Settings" aria-label="Settings" aria-haspopup="dialog">&#9881;</button>
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
      <div class="toggle-row"><span>Wake word (beta)</span><div class="sw" id="sw-wake"></div></div>
      <div class="toggle-row"><span>Captions</span><div class="sw" id="sw-cap"></div></div>
      <div class="toggle-row"><span>Floating orb</span><div class="sw" id="sw-orb" title="A small always-on-top orb that stays visible while Mist works"></div></div>
      <div class="toggle-row" id="row-orb_flight"><span>Orb follows the task</span><div class="sw" id="sw-orb_flight" title="Needs the floating orb. The orb flies beside the window Mist is working in"></div></div>
      <div class="toggle-row" id="row-orb_panels"><span>Task panels</span><div class="sw" id="sw-orb_panels" title="Needs the floating orb. Two panels at the top of the screen: the steps Mist takes and its answer"></div></div>
      <div class="toggle-row" id="row-firefox_signins"><span>Use my Firefox sign-ins</span><div class="sw" id="sw-firefox_signins" title="Copies your Firefox profile (cookies and saved logins) into MistAI's own browser so it can use sites you are signed in to. Off by default. Takes effect the next time MistAI's browser starts."></div></div>
    </div>

    <div class="card">
      <div class="card-title">Resources</div>
      <div class="res-link" role="link" tabindex="0" onclick="openLink('https://mistai.org/docs')"><span class="ic" aria-hidden="true">&#128214;</span>Docs &amp; Get API Key</div>
      <div class="res-link" role="link" tabindex="0" onclick="openLink('https://github.com/Misto0o/MistAI-Desktop')"><span class="ic" aria-hidden="true">&#9733;</span>GitHub Repo</div>
    </div>

    <div class="card">
      <div class="card-title" title="How Mist addresses you in replies">How Mist addresses you</div>
      <select id="sel-gender"><option value="none">Neutral</option><option value="male">Sir</option><option value="female">Ma'am</option></select>
    </div>

  </div>

  <div class="center">
    <div class="orb-wrap" id="orb-wrap" data-state="idle" data-level="live">
<!--%ORB_LAYERS%-->
    </div>
    <div class="mist-title">M.I.S.T.</div>
    <div class="mist-status ready" id="mist-status" role="status" aria-live="polite"><div class="dot" aria-hidden="true"></div><span id="mist-status-text">Ready</span></div>

    <div class="control-bar">
      <div class="control-item">
        <button class="ctrl-btn" id="btn-ptt" title="Push to talk: say one command">&#127908;</button>
        <div class="ctrl-label" id="ptt-label">Talk</div>
      </div>
      <div class="control-item">
        <button class="ctrl-btn" id="ctrl-caption" title="Toggle on-screen captions" aria-label="Captions">&#128172;</button>
        <div class="ctrl-label">Captions</div>
      </div>
    </div>
  </div>

  <div class="convo">
    <div class="convo-head">
      <h3>CONVERSATION</h3>
      <button class="btn-clear" onclick="clearChat()">Clear chat</button>
    </div>
    <div id="chat" role="log" aria-live="polite" aria-label="Conversation">
      <div class="empty" id="empty">
        <p>Type a request or press Talk. Mist works in your apps, files and its own browser, and asks before anything that can't be undone.</p>
        <div class="hints">
          <div class="hint-chip" role="button" tabindex="0" onclick="fillAndSend(this)">Open Firefox and go to YouTube</div>
          <div class="hint-chip" role="button" tabindex="0" onclick="fillAndSend(this)">List all my open windows</div>
          <div class="hint-chip" role="button" tabindex="0" onclick="fillAndSend(this)">Take a screenshot and save it</div>
          <div class="hint-chip" role="button" tabindex="0" onclick="fillAndSend(this)">What's in my clipboard?</div>
        </div>
      </div>
    </div>
    <div class="convo-input">
      <input id="inp" type="text" placeholder="Tell MistAI what to do..." autocomplete="off" aria-label="Message to Mist"/>
      <button class="btn-send" id="btn-send" title="Send" aria-label="Send">&#9654;</button>
      <button class="btn-send" id="btn-stop" title="Stop the current task" aria-label="Stop">&#9632;</button>
    </div>
  </div>
</div>

<div class="modal" id="apiModal">
  <div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="settings-title">
    <div class="modal-title" id="settings-title">SETTINGS</div>
    <div class="card-title">API key</div>
    <input type="password" class="modal-input" id="apiKeyInput" placeholder="Enter your MistAI API key (mistai_...)" autocomplete="off" aria-label="MistAI API key"/>
    <div class="modal-hint">Saved when you press Save; leave empty to keep the current key.</div>
    <label id="backend-row" style="font-size:.75em;color:var(--muted);margin-bottom:12px;display:none">
      <input type="checkbox" id="useBackend" checked/> Use MistAI backend
    </label>
    <div style="margin-bottom:12px">
      <button class="btn-cancel" id="btn-import-facts" title="Add facts from your my_facts.json file">Import my facts</button>
      <div class="card-note" id="facts-note"></div>
    </div>
    <div style="margin-bottom:12px">
      <div class="card-title">Ask before sending a message</div>
      <!-- Choices and explanations come from Api.get_ask_before_sending_options() (config). -->
      <select id="sel-send"></select>
      <div class="card-note" id="send-note"></div>
      <div class="card-note" id="approvals-note"></div>
      <button class="btn-cancel" id="btn-forget-approvals" title="Forget every remembered approval">Forget all</button>
    </div>
    <div class="modal-buttons">
      <button class="btn-save" onclick="saveAPIKey()">Save</button>
      <button class="btn-cancel" onclick="closeAPIModal()">Close</button>
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
const orbWrap=document.getElementById('orb-wrap');
const swWake=document.getElementById('sw-wake');
const swCap=document.getElementById('sw-cap');
const selGender=document.getElementById('sel-gender');
const apiModal=document.getElementById('apiModal');
const mistStatus=document.getElementById('mist-status'),mistStatusText=document.getElementById('mist-status-text');
const btnPtt=document.getElementById('btn-ptt'),ctrlCaption=document.getElementById('ctrl-caption');
const pttLabel=document.getElementById('ptt-label');
let stepEl=null;

// Settings switches are divs styled as switches: give them the switch role,
// keyboard focus and Space/Enter, and keep aria-checked in step with 'on'.
function setSw(sw,on){
  sw.classList.toggle('on',!!on);
  sw.setAttribute('aria-checked',on?'true':'false');
}
function makeSwitch(sw,label){
  sw.setAttribute('role','switch');
  sw.setAttribute('aria-label',label);
  sw.setAttribute('aria-checked','false');
  sw.tabIndex=0;
  sw.onkeydown=e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();if(sw.onclick)sw.onclick();}};
}
for(const [id,label] of [['sw-wake','Wake word'],['sw-cap','Captions'],['sw-orb','Floating orb'],
  ['sw-orb_flight','Orb follows the task'],['sw-orb_panels','Task panels'],
  ['sw-firefox_signins','Use my Firefox sign-ins']]){
  const sw=document.getElementById(id);if(sw)makeSwitch(sw,label);
}
// Hint chips and resource links are divs too: Enter or Space clicks them.
function keyClick(e){
  const t=e.target;
  if(!(e.key==='Enter'||e.key===' ')||!t||!t.classList||!t.onclick)return;
  if(t.classList.contains('hint-chip')||t.classList.contains('res-link')){e.preventDefault();t.onclick();}
}

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
// throws (tests/test_ui_page.py checks none is used). The intervals start first, and each step runs on its own
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
  runStep('stats',updateStats);
  runStep('status',checkStatus);
  runStep('orb',loadOrbSetting);
  runStep('signins',loadFirefoxSignins);
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

function setMistStatus(state,text){
  mistStatus.className='mist-status '+state;
  mistStatusText.textContent=text;
}

// The status line when nothing temporary is showing: a push-to-talk listen,
// then a running task, then the wake word mode.
function restoreStatus(){
  if(pttState==='waiting') setMistStatus('listening','Waiting for mic...');
  else if(pttState==='listening') setMistStatus('listening','Listening...');
  else if(busy) setMistStatus('listening', stopping ? 'Stopping...' : 'Working...');
  else if(!wakeOn) setMistStatus('ready','Ready');
  else setMistStatus('listening','Listening for wake word...');
}

// Wake word: one switch (sidebar), off at startup.
async function toggleWake(){
  wakeOn=!wakeOn;
  setSw(swWake,wakeOn);
  orb.classList.toggle('active',wakeOn);
  if(ready)try{
    if(wakeOn)await pywebview.api.start_wake_word_detection();
    else await pywebview.api.stop_wake_word_detection();
    restoreStatus();
    addMsg('Wake: '+(wakeOn?'active':'off'),'system');
  }catch(e){console.error('Wake toggle error:',e);addMsg('Wake error: '+e,'error');}
}
swWake.onclick=toggleWake;

// Push-to-talk: Python runs the turn (Api.push_to_talk) and reports the mic
// through handleListenState; the page only shows it. A short note (a refusal,
// "Didn't catch that") replaces the status line for a few seconds.
const PTT_NOTE_MS=3000,PTT_SAFETY_MS=45000;
let pttState='idle',pttNoteTimer=null,pttSafetyTimer=null;
// The orb shows one state from what the page knows: the push-to-talk mic
// (handleListenState), Mist's voice (is_agent_busy().speaking, polled) and a
// running task (busy). The mic wins, then the voice, then the task.
let speaking=false;
function orbState(s){
  if(s.ptt==='waiting'||s.ptt==='listening')return 'listening';
  if(s.speaking)return 'speaking';
  if(s.busy)return 'thinking';
  return 'idle';
}
function setOrb(){
  const state=orbState({ptt:pttState,speaking:speaking,busy:busy});
  orbWrap.dataset.state=state;
  followAudio(state);
  return state;
}

/*%ORB_LEVEL_JS%*/
// While the orb is listening or speaking its core follows the mic or Mist's
// voice: Api.audio_level every AUDIO_POLL_MS, smoothed by orbLevel. In every
// other state nothing is polled and the core rests (level 0).
const AUDIO_POLL_MS=80;
const orbLvl=orbLevel(orbWrap);
let audioPoll=null;
const audioLive=(st)=>st==='listening'||st==='speaking';
function followAudio(state){
  if(!audioLive(state)){
    if(audioPoll!==null){clearTimeout(audioPoll);audioPoll=null;}
    orbLvl.set(0);
    return;
  }
  if(audioPoll!==null||typeof pywebview==='undefined'||!pywebview.api
     ||typeof pywebview.api.audio_level!=='function')return;
  const tick=async()=>{
    try{
      const r=(await pywebview.api.audio_level())||{};
      const st=orbWrap.dataset.state;
      if(st==='listening')orbLvl.set(Number(r.mic)||0);
      else if(st==='speaking')orbLvl.set(r.tts===null?null:(Number(r.tts)||0));
    }catch(e){}
    if(audioPoll===null)return;                  // stopped meanwhile
    audioPoll=audioLive(orbWrap.dataset.state)?setTimeout(tick,AUDIO_POLL_MS):null;
    if(audioPoll===null)orbLvl.set(0);
  };
  audioPoll=setTimeout(tick,0);
}
function showPttNote(text){
  clearTimeout(pttNoteTimer);
  setMistStatus('notice',String(text));
  pttNoteTimer=setTimeout(()=>{pttNoteTimer=null;restoreStatus();},PTT_NOTE_MS);
}
function handleListenState(state,message){
  pttState=(state==='waiting'||state==='listening')?state:'idle';
  btnPtt.classList.toggle('active',pttState!=='idle');
  // While listening, a second press ends the listen (Api.push_to_talk).
  pttLabel.textContent=pttState==='listening'?'Done':'Talk';
  clearTimeout(pttSafetyTimer);pttSafetyTimer=null;
  // If Python never reports the end (a lost notification), don't stay
  // "Listening". Every waiting/listening message restarts it, so a follow-up
  // listen gets its own full window (up to 6s wait + 30s phrase + STT).
  if(pttState!=='idle')pttSafetyTimer=setTimeout(()=>handleListenState('idle',''),PTT_SAFETY_MS);
  if(pttState==='idle'&&message)showPttNote(message);
  else{clearTimeout(pttNoteTimer);pttNoteTimer=null;restoreStatus();}
  setOrb();
}
btnPtt.onclick=async()=>{
  if(!ready)return;
  try{
    const r=await pywebview.api.push_to_talk();
    if(!(r&&r.success))showPttNote((r&&r.message)||'Push-to-talk is not available right now');
  }catch(e){
    console.error('[ptt] push_to_talk failed:',e);
    showPttNote('Push-to-talk failed: '+((e&&e.message)||e));
  }
};

async function toggleCaptions(){
  capOn=!capOn;
  setSw(swCap,capOn);ctrlCaption.classList.toggle('active',capOn);
  if(ready)try{
    await pywebview.api.toggle_captions(capOn);
    addMsg(capOn?'Captions on':'Captions off','system');
  }catch(e){console.error('Caption toggle error:',e);addMsg('Caption error: '+e,'error');}
}
swCap.onclick=toggleCaptions;
ctrlCaption.onclick=toggleCaptions;

// Floating orb (off by default): Python saves the setting and shows or hides
// a small always-on-top window with only the orb (core/floating_orb.py).
const swOrb=document.getElementById('sw-orb');
let orbOn=false;
// The two orb options only act while the floating orb is on: dim them then.
function showOrbDependents(){
  for(const name of ['orb_flight','orb_panels']){
    const row=document.getElementById('row-'+name);
    if(row)row.classList.toggle('inactive',!orbOn);
  }
}
async function loadOrbSetting(){
  if(typeof pywebview.api.get_floating_orb!=='function')return;
  orbOn=!!(await pywebview.api.get_floating_orb());
  setSw(swOrb,orbOn);
  showOrbDependents();
  if(typeof pywebview.api.get_orb_options!=='function')return;
  const opts=(await pywebview.api.get_orb_options())||{};
  for(const name of ['orb_flight','orb_panels']){
    const sw=document.getElementById('sw-'+name);
    setSw(sw,!!opts[name]);
    sw.onclick=async()=>{
      try{
        const r=await pywebview.api.set_orb_option(name,!sw.classList.contains('on'));
        setSw(sw,!!(r&&r.enabled));
        if(r&&r.error)addMsg('Orb: '+r.error,'error');
      }catch(e){console.error('Orb option toggle error:',e);}
    };
  }
}
swOrb.onclick=async()=>{
  if(!ready)return;
  try{
    const r=await pywebview.api.set_floating_orb(!orbOn);
    orbOn=!!(r&&r.enabled);
    setSw(swOrb,orbOn);
    showOrbDependents();
    if(r&&r.error)addMsg('Floating orb: '+r.error,'error');
  }catch(e){console.error('Floating orb toggle error:',e);}
};

// "Use my Firefox sign-ins" (off by default): Python saves it; MistAI's
// browser reads it the next time it starts (Api.get_firefox_signins).
async function loadFirefoxSignins(){
  if(typeof pywebview.api.get_firefox_signins!=='function')return;
  const sw=document.getElementById('sw-firefox_signins');
  setSw(sw,!!(await pywebview.api.get_firefox_signins()));
  sw.onclick=async()=>{
    try{
      const r=await pywebview.api.set_firefox_signins(!sw.classList.contains('on'));
      setSw(sw,!!(r&&r.enabled));
      if(r&&r.error)addMsg('Firefox sign-ins: '+r.error,'error');
      else addMsg((r&&r.enabled?'Firefox sign-ins on':'Firefox sign-ins off')
        +': applies the next time the Mist browser starts','system');
    }catch(e){console.error('Firefox sign-ins toggle error:',e);}
  };
}

function showAskUserPrompt(question){
  addMsg(question, 'assistant');
  unlockForAnswer("Type your answer...");
}
function unlockForAnswer(placeholder){
  inp.placeholder = placeholder;
  inp.focus();
  // Unlock the input so the user can answer, but the agent is still
  // running -- keep the orb/status showing that.
  busy=false;
  btnSend.disabled=inp.disabled=false;
  setMistStatus('listening','Waiting for your answer...');
  inp.dataset.answering = "true";
}

// A confirmation (Api.ask_confirmation): the question as text plus Yes and No
// buttons. A click sends its answer once; the buttons go away after any answer
// (a click, speech or the text box), and Python closes the prompt either way.
// The text box keeps working as for any question.
let confirmPrompt=null;
function showConfirmPrompt(question){
  const offerRemember=arguments[1]===true;   // messages only, from Python
  closeConfirmPrompt();
  const box=addMsg('', 'assistant confirm');
  const q=document.createElement('div');
  q.textContent=String(question||'');
  box.appendChild(q);
  const row=document.createElement('div');
  row.className='confirm-buttons';
  // offerRemember: a third button, a yes that Python also remembers for this person.
  const buttons=[['yes','Yes','btn-save'],['no','No','btn-cancel']];
  if(offerRemember)buttons.push(['remember','Always for this person','btn-cancel']);
  for(const [answer,label,cls] of buttons){
    const b=document.createElement('button');
    b.className=cls;b.textContent=label;
    b.onclick=()=>answerConfirm(answer,label);
    row.appendChild(b);
  }
  box.appendChild(row);
  confirmPrompt={row};
  unlockForAnswer("Type yes or no, or press a button...");
}
function closeConfirmPrompt(){
  if(!confirmPrompt)return false;
  confirmPrompt.row.remove();
  confirmPrompt=null;
  if(inp.dataset.answering==="true"){
    inp.dataset.answering="false";
    inp.placeholder="Tell MistAI what to do...";
    setBusy(true);
  }
  return true;
}
async function answerConfirm(answer,label){
  if(!closeConfirmPrompt())return;     // already answered: never a second submit
  addMsg(label,'user');
  try{await pywebview.api.submit_answer(answer,'button');}
  catch(e){addMsg('Error: '+e,'error');}
}

function openAPIModal(){
  apiModal.classList.add('open');
  document.getElementById('apiKeyInput').focus();
  if(ready){runStep('sending',loadSendSetting);runStep('approvals',loadApprovals);}
}
function closeAPIModal(){
  if(!apiModal.classList.contains('open'))return;
  apiModal.classList.remove('open');
  document.getElementById('btn-settings').focus();
}
// Escape closes Settings; Enter in the key field saves it.
window.addEventListener('keydown',e=>{
  if(e.key==='Escape')closeAPIModal();
  else if(e.key==='Enter'&&e.target&&e.target.id==='apiKeyInput')saveAPIKey();
  else keyClick(e);
});

// Ask before sending (messages only): the choices, labels and explanations
// come from Python, which also validates and saves the choice; the page only
// shows labels and sends ids. Loaded when Settings opens.
const selSend=document.getElementById('sel-send'),sendNote=document.getElementById('send-note');
let sendHelp=new Map();
function showSendNote(text,isError){
  sendNote.textContent=text;
  sendNote.className='card-note'+(isError?' error':'');
}
async function loadSendSetting(){
  try{
    const o=await pywebview.api.get_ask_before_sending_options();
    const choices=(o&&Array.isArray(o.choices))?o.choices:[];
    if(!choices.length)throw new Error('no choices were returned');
    selSend.innerHTML='';sendHelp=new Map();
    for(const c of choices){
      const opt=document.createElement('option');
      opt.value=String(c.id);opt.textContent=String(c.label||c.id);
      sendHelp.set(String(c.id),String(c.help||''));
      selSend.appendChild(opt);
    }
    selSend.value=o.current;
    showSendNote(sendHelp.get(String(o.current))||'',false);
  }catch(e){
    console.error('[send] could not load the setting:',e);
    showSendNote('Could not load this setting: '+((e&&e.message)||e),true);
  }
}
// Remembered "Always for this person" approvals: only a count is shown.
const approvalsNote=document.getElementById('approvals-note');
async function loadApprovals(){
  try{
    const n=Number(await pywebview.api.get_remembered_approvals_count())||0;
    approvalsNote.textContent='Remembered approvals: '+n;
    approvalsNote.className='card-note';
  }catch(e){
    console.error('[approvals] could not load the count:',e);
    approvalsNote.textContent='Could not load the remembered approvals';
    approvalsNote.className='card-note error';
  }
}
// Forget all asks first, in the page: the first click arms it, a second
// click within 5 s forgets; otherwise it disarms.
const btnForget=document.getElementById('btn-forget-approvals');
let forgetArmed=null;
function disarmForget(){
  if(forgetArmed)clearTimeout(forgetArmed);
  forgetArmed=null;btnForget.textContent='Forget all';
}
btnForget.onclick=async()=>{
  if(!ready)return;
  if(!forgetArmed){
    btnForget.textContent='Click again to forget all';
    forgetArmed=setTimeout(disarmForget,5000);
    return;
  }
  disarmForget();
  try{
    const r=await pywebview.api.forget_remembered_approvals();
    if(!(r&&r.success))addMsg((r&&r.error)||'Could not forget the approvals','error');
  }catch(err){console.error('[approvals] forget failed:',err);}
  await loadApprovals();
};
selSend.onchange=async e=>{
  if(!ready)return;
  try{
    const r=await pywebview.api.set_ask_before_sending(e.target.value);
    if(r&&r.success){
      showSendNote(String(r.help||''),false);
    }else{
      await loadSendSetting();   // show the choice that is really saved
      showSendNote((r&&r.error)||'Could not save this setting',true);
    }
  }catch(err){
    console.error('[send] save failed:',err);
    await loadSendSetting();
  }
};
async function saveAPIKey(){
  if(!ready)return;
  const key=document.getElementById('apiKeyInput').value.trim();
  const useBackend=document.getElementById('useBackend').checked;
  if(key){
    try{
      await pywebview.api.set_api_key(key);
      document.getElementById('apiKeyInput').value='';
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

// "Import my facts": only when the user asks. Python reads the file and
// reports what it added; the page shows that line as text.
document.getElementById('btn-import-facts').onclick=async()=>{
  if(!ready)return;
  const note=document.getElementById('facts-note');
  try{
    const r=await pywebview.api.import_my_facts();
    note.textContent=(r&&r.message)||'Import finished';
    note.className='card-note'+(r&&r.success?'':' error');
    if(r&&r.added)updateStats();
  }catch(e){
    console.error('[facts] import failed:',e);
    note.textContent='Import failed: '+((e&&e.message)||e);
    note.className='card-note error';
  }
};

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
    if (confirmPrompt) { confirmPrompt.row.remove(); confirmPrompt = null; }
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
    // model + gender are resolved on the Python side (Api.current_model()
    // and the saved profile) -- the UI passes null.
    const res = await pywebview.api.ask_mistai(msg, null, null);
    stepEl = null;
    showResult(res);
  } catch(e) {
    addMsg('Error: ' + e, 'error');
  } finally {
    sendInFlight = false;
    // Only go idle if the backend agrees nothing is running.
    syncBusy();
    inp.focus();
  }
}

// The end of a turn in the chat, typed (send) or voice (handleVoiceReply).
function showResult(res) {
  if (res.success) {
    if (res.command && res.command.speech) {
      addMsg(res.command.speech, 'assistant');
    }
    if (res.steps) addMsg(`Completed in ${res.steps} step${res.steps !== 1 ? 's' : ''}`, 'memory');
    updateStats();
  } else if (res.error === 'busy') {
    addMsg(res.command && res.command.speech ? res.command.speech : 'Mist is still working on a previous task.', 'system');
  } else if (res.error === 'still_running') {
    addMsg('Still working on this in the background — the status will update when it finishes.', 'system');
  } else if (res.command && String(res.command.speech || '').trim()) {
    // What Mist said (e.g. "I couldn't read the answer.", or an API error,
    // whose speech is the error text): shown once, not as "Error: ...".
    addMsg(String(res.command.speech), 'assistant');
  } else {
    addMsg('Error: ' + (res.error || 'Unknown'), 'error');
  }
}

function setBusy(b){
  busy=b;
  btnSend.disabled=inp.disabled=b;
  restoreStatus();
  setOrb();
}

// Reconcile the UI with the runtime's actual agent state. Voice-initiated
// tasks have no promise in flight, so this is the only way the UI learns
// about them; for UI-initiated tasks it also confirms a Stop has completed.
async function syncBusy(){
  if(!ready)return;
  try{
    const s=await pywebview.api.is_agent_busy();
    speaking=!!s.speaking;
    setOrb();
    if(s.busy){
      if(!busy && inp.dataset.answering!=="true") setBusy(true);
    }else{
      stopping=false;
      if(busy && !sendInFlight) setBusy(false);
      if(inp.dataset.answering==="true"){inp.dataset.answering="false";inp.placeholder="Tell MistAI what to do...";}
    }
  }catch(e){}
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
// A voice turn (push-to-talk or wake word), sent by Api._voice_ask. The
// bubble shows exactly what Mist HEARD (textContent; the mic is CSS), and a
// new turn gets its own step card.
function handleVoiceTurn(text){
  stepEl=null;
  addMsg(String(text||''),'user').classList.add('voice');
}
function handleVoiceReply(success,speech,steps,error){
  stepEl=null;
  showResult({success:!!success,command:{speech:String(speech||'')},
              steps:Number(steps)||0,error:String(error||'')});
}
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


# The orb's look, shared by the main window (HTML) and the floating orb window
# (ORB_HTML, core/floating_orb.py): a flat frosted-glass disc (config.ORB_TINT)
# whose layers are sized from --s, the orb's side (each page sets it), so one
# block serves the 150 px main orb and the 100vmin floating one. Ten looks, one
# per data-state (core/floating_orb.py orb_state): idle breathes; listening,
# two outward rings; thinking, a rotating half arc; planning, three nodes
# lighting in turn; executing, a slow quarter arc (small, docked); reading, a
# scan line inside the disc; waiting, amber, a slow pause ring and a pause
# glyph; speaking, two ripples and three moving bars; completed, green, a check
# and one ring burst; error, soft red, an exclamation mark and one short shake.
# Shape tells them apart, not colour alone. Every layer stays inside the disc:
# nothing is drawn outside the orb's circle (no outer rings, glows or shadows).
# While listening or speaking the inner core (.heart) follows the audio level:
# --lvl (0..1, smoothed by orbLevel) scales and brightens it; with no level
# (data-level="none": the fallback voice) it breathes instead. Motion stops
# under prefers-reduced-motion and when Windows animation effects are off
# (the bridge's "still": data-motion="still"); glyphs and tints stay.

# ============================================================
# ORB PAGE STATE COLOURS
# ============================================================

ORB_STATE_TINTS = {
    "waiting": ("rgba(255,206,112,.62)", "rgba(214,150,40,.30)", "rgba(255,214,140,.95)"),
    "completed": ("rgba(140,236,180,.60)", "rgba(52,168,108,.28)", "rgba(190,255,215,.95)"),
    "error": ("rgba(255,150,140,.58)", "rgba(196,72,72,.28)", "rgba(255,200,196,.95)"),
}   # (tint centre, tint edge, glyph/ring colour)

ORB_STATES = ("idle", "listening", "thinking", "planning", "executing", "reading", "waiting",
              "speaking", "completed", "error")   # = core/floating_orb.py STATES


# ============================================================
# ORB PAGE STILL RULES
# ============================================================

def _orb_still_rules(w):
    """The static looks for wrapper selector ``w``: no motion, but every state
    keeps its shape, glyph and tint (reduced motion, Windows animations off)."""
    return (
        f"{w},{w} .disc,{w} .ring,{w} .arc,{w} .ripple,{w} .halo,{w} .burst,{w} .scan,"
        f"{w} .heart,{w} .nodes i,{w} .bars i{{animation:none !important}}"
        f'{w}[data-state="listening"] .ring{{opacity:.8;transform:scale(.95)}}'
        f'{w}[data-state="listening"] .ring.r2{{opacity:.45;transform:scale(.8)}}'
        f'{w}[data-state="thinking"] .arc{{opacity:1}}'
        f'{w}[data-state="executing"] .arc{{opacity:.9}}'
        f'{w}[data-state="planning"] .nodes i{{opacity:.4}}'
        f'{w}[data-state="planning"] .nodes i:nth-child(2){{opacity:1}}'
        f'{w}[data-state="reading"] .scan{{opacity:.9;transform:translateY(310%)}}'
        f'{w}[data-state="waiting"] .halo{{opacity:.8;transform:none}}'
        f'{w}[data-state="speaking"] .ripple{{opacity:.5;transform:scale(.9)}}'
        f'{w}[data-state="speaking"] .ripple.r2{{opacity:0}}'
        f'{w}[data-state="speaking"] .bars i{{transform:scaleY(.55)}}'
        f'{w}[data-state="speaking"] .bars i:nth-child(2){{transform:none}}'
        f'{w}[data-state="completed"] .burst{{opacity:0}}'
        f"{w} .heart{{transform:scale(.9)}}{w} .heart::before{{opacity:.75}}"
    )


# ============================================================
# SHARED ORB CSS
# ============================================================

def orb_css():
    """The orb's whole look as one CSS string (built from config.ORB_TINT):
    the layers, the ten state looks, the level-driven core, and the still
    rules (one prefers-reduced-motion block plus data-motion="still"). The
    page sets the wrapper's size and --s."""
    from core.config import ORB_TINT as t

    tint = ""
    for state, (centre, edge, _ink) in ORB_STATE_TINTS.items():
        tint += (f'.orb-wrap[data-state="{state}"] .tint{{opacity:1;'
                 f"background:radial-gradient(circle,{centre} 0%,{edge} 100%)}}")
    w_ink, c_ink, e_ink = (ORB_STATE_TINTS[k][2] for k in ("waiting", "completed", "error"))

    def u(k):                                   # a length: k x the orb's side
        return f"calc(var(--s) * {k})"

    return (
        ".disc,.tint,.heart,.inner,.ring,.arc,.ripple,.halo,.burst{position:absolute;inset:0;"
        "border-radius:50%;pointer-events:none}"
        f".disc{{background:radial-gradient(circle,{t['center']} 0%,{t['mid']} 58%,{t['edge']} 100%);"
        f"border:1px solid {t['border']};box-sizing:border-box;"
        "animation:breathe 3.2s ease-in-out infinite}"
        ".tint{opacity:0;transition:opacity .35s ease}"
        # The audio-driven core: 52% of the disc at rest, at most 62% (scale 1.2).
        # --lvl moves only transform and opacity (compositor-only, no repaint):
        # the size on .heart, the brightness as its glow layer's opacity.
        ".heart{inset:24%;opacity:0;transition:opacity .25s ease;will-change:transform;"
        "transform:scale(calc(.82 + var(--lvl, 0) * .38))}"
        ".heart::before{content:'';position:absolute;inset:0;border-radius:50%;"
        f"background:radial-gradient(circle,{t['center']} 0%,{t['mid']} 60%,rgba(255,255,255,0) 100%);"
        "will-change:opacity;opacity:calc(.5 + var(--lvl, 0) * .5)}"
        '.orb-wrap[data-state="listening"] .heart,.orb-wrap[data-state="speaking"] .heart{opacity:1}'
        '.orb-wrap[data-state="speaking"][data-level="none"] .heart{'
        "animation:heart 1.2s ease-in-out infinite}"
        ".inner{inset:6%;overflow:hidden}"
        f".scan{{position:absolute;left:0;right:0;top:0;height:14%;opacity:0;"
        f"background:linear-gradient(to bottom,transparent,{t['border']},transparent)}}"
        f".ring,.ripple{{inset:8%;border:2px solid {t['glow']};opacity:0}}"
        f".arc{{inset:12%;border:2px solid transparent;border-top-color:{t['border']};"
        f"border-right-color:{t['mid']};opacity:0}}"
        f".halo{{inset:10%;border:2px solid {w_ink};opacity:0}}"
        f".burst{{inset:10%;border:2px solid {c_ink};opacity:0}}"
        ".core{position:absolute;inset:0;display:flex;align-items:center;"
        "justify-content:center;pointer-events:none}"
        ".core>span{position:absolute;display:none;align-items:center;justify-content:center}"
        f".nodes{{gap:{u('.07')}}}.nodes i{{display:block;width:{u('.10')};height:{u('.10')};"
        f"border-radius:50%;background:{t['center']};opacity:.25;"
        "animation:node 1.2s ease-in-out infinite}"
        ".nodes i:nth-child(2){animation-delay:.4s}.nodes i:nth-child(3){animation-delay:.8s}"
        f".bars{{gap:{u('.05')}}}.bars i{{display:block;width:{u('.07')};height:{u('.30')};"
        f"border-radius:{u('.04')};background:{t['center']};transform:scaleY(.4);"
        "animation:bar .9s ease-in-out infinite}"
        ".bars i:nth-child(2){animation-delay:-.3s}.bars i:nth-child(3){animation-delay:-.6s}"
        f".g-pause{{gap:{u('.07')}}}.g-pause i{{display:block;width:{u('.08')};height:{u('.28')};"
        f"border-radius:{u('.02')};background:{w_ink}}}"
        f".g-check{{width:{u('.15')};height:{u('.30')};box-sizing:border-box;"
        f"border-right:{u('.06')} solid {c_ink};border-bottom:{u('.06')} solid {c_ink};"
        f"border-radius:{u('.01')};transform:translateY({u('-.04')}) rotate(45deg)}}"
        f".g-excl{{flex-direction:column;gap:{u('.04')}}}"
        f".g-excl i{{display:block;width:{u('.08')};border-radius:{u('.04')};background:{e_ink}}}"
        f".g-excl i:first-child{{height:{u('.26')}}}.g-excl i:last-child{{height:{u('.08')}}}"
        "@keyframes breathe{0%,100%{filter:brightness(1)}50%{filter:brightness(1.08)}}"
        "@keyframes heart{0%,100%{transform:scale(.84)}50%{transform:scale(1.02)}}"
        "@keyframes pulse{0%{transform:scale(.7);opacity:.9}100%{transform:scale(1.05);opacity:0}}"
        "@keyframes spin{to{transform:rotate(360deg)}}"
        "@keyframes ripple{0%{transform:scale(.5);opacity:.7}100%{transform:scale(1.05);opacity:0}}"
        "@keyframes node{0%,100%{opacity:.25;transform:scale(.85)}33%{opacity:1;transform:scale(1.1)}}"
        "@keyframes scan{from{transform:translateY(-100%)}to{transform:translateY(714%)}}"
        "@keyframes halo{0%,100%{opacity:.35;transform:scale(.92)}50%{opacity:.9;transform:scale(1)}}"
        "@keyframes bar{0%,100%{transform:scaleY(.35)}50%{transform:scaleY(1)}}"
        "@keyframes burst{0%{transform:scale(.6);opacity:.95}100%{transform:scale(1.1);opacity:0}}"
        "@keyframes shake{0%,100%{transform:none}20%{transform:translateX(-4%)}"
        "40%{transform:translateX(4%)}60%{transform:translateX(-3%)}80%{transform:translateX(2%)}}"
        '.orb-wrap[data-state="listening"] .ring{animation:pulse 1.4s ease-out infinite}'
        '.orb-wrap[data-state="listening"] .ring.r2{animation-delay:.7s}'
        '.orb-wrap[data-state="thinking"] .arc{opacity:1;animation:spin 1.2s linear infinite}'
        '.orb-wrap[data-state="executing"] .arc{opacity:.9;border-right-color:transparent;'
        "animation:spin 3.2s linear infinite}"
        '.orb-wrap[data-state="planning"] .nodes{display:flex}'
        '.orb-wrap[data-state="reading"] .scan{opacity:.9;'
        "animation:scan 1.6s ease-in-out infinite alternate}"
        '.orb-wrap[data-state="waiting"] .halo{animation:halo 2.4s ease-in-out infinite}'
        '.orb-wrap[data-state="waiting"] .g-pause{display:flex}'
        '.orb-wrap[data-state="speaking"] .ripple{animation:ripple 1.6s ease-out infinite}'
        '.orb-wrap[data-state="speaking"] .ripple.r2{animation-delay:.8s}'
        '.orb-wrap[data-state="speaking"] .bars{display:flex}'
        '.orb-wrap[data-state="completed"] .burst{animation:burst .9s ease-out 1 both}'
        '.orb-wrap[data-state="completed"] .g-check{display:block}'
        '.orb-wrap[data-state="error"]{animation:shake .45s ease-in-out 1}'
        '.orb-wrap[data-state="error"] .g-excl{display:flex}'
        + tint
        + "@media (prefers-reduced-motion: reduce){" + _orb_still_rules(".orb-wrap") + "}"
        + _orb_still_rules('.orb-wrap[data-motion="still"]')
    )


# ============================================================
# SHARED ORB LAYERS
# ============================================================

def orb_layers(disc_id=""):
    """The orb's layers (inside an .orb-wrap): back to front, the ripples,
    the disc (``disc_id`` names it for the main page's wake-word rim), the
    state tint, the audio core, the scan line, the rings and arcs, and the
    glyphs."""
    disc = f'<div class="disc" id="{disc_id}"></div>' if disc_id else '<div class="disc"></div>'
    return (
        '  <span class="ripple"></span><span class="ripple r2"></span>\n'
        f'  {disc}<div class="tint"></div><span class="heart"></span>\n'
        '  <div class="inner"><span class="scan"></span></div>\n'
        '  <span class="ring"></span><span class="ring r2"></span><span class="arc"></span>\n'
        '  <span class="halo"></span><span class="burst"></span>\n'
        '  <div class="core">\n'
        '    <span class="nodes"><i></i><i></i><i></i></span>\n'
        '    <span class="bars"><i></i><i></i><i></i></span>\n'
        '    <span class="glyph g-pause"><i></i><i></i></span>\n'
        '    <span class="glyph g-check"></span>\n'
        '    <span class="glyph g-excl"><i></i><i></i></span>\n'
        '  </div>'
    )


# ============================================================
# SHARED ORB LEVEL SMOOTHING
# ============================================================

# orbLevel(wrap): the polled audio level (0..1, or null when the voice has no
# level) becomes --lvl on the wrapper, smoothed on animation frames: fast
# attack (ATTACK_S), slower decay (DECAY_S); frames stop once it has settled.
# null sets data-level="none" (the core breathes instead). Under reduced
# motion or data-motion="still" the core rests at 0. Both pages use it.
ORB_LEVEL_JS = r"""function orbLevel(wrap){
  const ATTACK_S=0.05,DECAY_S=0.25;
  let target=0,cur=0,last=0,raf=0;
  const reduced=()=>typeof matchMedia==='function'
    &&matchMedia('(prefers-reduced-motion: reduce)').matches;
  const still=()=>wrap.dataset.motion==='still'||reduced();
  const write=(v)=>{
    if(wrap.style&&typeof wrap.style.setProperty==='function')
      wrap.style.setProperty('--lvl',v.toFixed(3));
  };
  function frame(t){
    const dt=last?Math.min(0.1,(t-last)/1000):1/60;
    last=t;
    const tau=target>cur?ATTACK_S:DECAY_S;
    cur+=(target-cur)*(1-Math.exp(-dt/tau));
    if(Math.abs(target-cur)<0.002)cur=target;
    write(cur);
    if(cur===target){raf=0;last=0;}
    else raf=requestAnimationFrame(frame);
  }
  return {set(level){
    wrap.dataset.level=level===null?'none':'live';
    target=(typeof level==='number'&&isFinite(level))?Math.max(0,Math.min(1,level)):0;
    if(still()||typeof requestAnimationFrame!=='function'){
      cur=still()?0:target;
      write(cur);
      return;
    }
    if(!raf)raf=requestAnimationFrame(frame);
  },
  get value(){return cur;}};
}
"""


# ============================================================
# ORB PAGE
# ============================================================

ORB_POLL_MS = 500        # the floating orb page's state poll
ORB_FAST_POLL_MS = 80    # ...while listening or speaking (the audio-driven core)


def _orb_page():
    from core.config import orb_background

    # The disc fills the window (the round region is the disc's edge) and the
    # page and window background is the rim colour (config.orb_background),
    # so a frame the page has not painted yet never shows black.
    bg = orb_background()
    css = (
        f"html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:{bg}}}"
        "body{display:flex;align-items:center;justify-content:center;user-select:none;"
        "-webkit-user-select:none;cursor:default}"
        ".orb-wrap{position:relative;width:100vmin;height:100vmin;--s:100vmin}"
        + orb_css()
    )
    states = ",".join(f"'{s}'" for s in ORB_STATES)
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title></title>
<style>
""" + css + """
</style>
</head>
<body>
<div class="orb-wrap" id="orb-wrap" data-state="idle" data-motion="on" data-level="live">
""" + orb_layers() + """
</div>
<script>
""" + ORB_LEVEL_JS + """
window.addEventListener('pywebviewready',()=>{
  const wrap=document.getElementById('orb-wrap');
  const STATES=[""" + states + """];
  // The state from the bridge every POLL_MS; every FAST_POLL_MS while the
  // core follows the mic or the voice (listening, speaking), when the reply
  // also carries "level". Each poll schedules the next one, so a slow reply
  // never stacks calls.
  const POLL_MS=""" + str(ORB_POLL_MS) + """,FAST_POLL_MS=""" + str(ORB_FAST_POLL_MS) + """;
  const LIVE=['listening','speaking'];
  const lvl=orbLevel(wrap);
  async function poll(){
    try{
      const s=await pywebview.api.orb_state();
      const st=String(s&&s.state||'idle');
      wrap.dataset.state=STATES.includes(st)?st:'idle';
      wrap.dataset.motion=(s&&s.still)?'still':'on';
      lvl.set(LIVE.includes(wrap.dataset.state)&&s&&'level' in s?s.level:0);
    }catch(e){}
    setTimeout(poll,LIVE.includes(wrap.dataset.state)?FAST_POLL_MS:POLL_MS);
  }
  poll();
  // Hold to talk: a press held HOLD_MS without moving starts push-to-talk and
  // the release sends it; a quick tap toggles (start, then a second tap
  // sends). Moving more than DRAG_PX is a drag (pywebview easy_drag), never talk.
  const HOLD_MS=280,DRAG_PX=6;
  let down=null,moved=false,holding=false,holdTimer=null;
  const talk=(kind)=>{try{pywebview.api.talk(kind);}catch(e){}};
  document.addEventListener('pointerdown',(e)=>{
    if(e.button!==0)return;
    down={x:e.screenX,y:e.screenY};moved=false;holding=false;
    clearTimeout(holdTimer);
    holdTimer=setTimeout(()=>{if(down&&!moved){holding=true;talk('start');}},HOLD_MS);
  });
  document.addEventListener('pointermove',(e)=>{
    if(down&&!moved&&Math.hypot(e.screenX-down.x,e.screenY-down.y)>DRAG_PX){
      moved=true;clearTimeout(holdTimer);
    }
  });
  document.addEventListener('pointerup',()=>{
    if(!down)return;
    clearTimeout(holdTimer);
    if(holding)talk('stop');
    else if(!moved)talk('toggle');
    down=null;holding=false;
  });
});
</script>
</body>
</html>"""


ORB_HTML = _orb_page()


# ============================================================
# MAIN PAGE ORB
# ============================================================

# The main window's orb is the same look (orb_css, orb_layers, orbLevel),
# placed where HTML has its markers; its disc keeps id="orb" (the wake-word rim).
HTML = (HTML.replace("/*%ORB_CSS%*/", orb_css())
        .replace("<!--%ORB_LAYERS%-->", orb_layers("orb"))
        .replace("/*%ORB_LEVEL_JS%*/", ORB_LEVEL_JS))


# A task panel window (core/floating_orb.py TASK PANELS): dark translucent glass, rounded,
# a close button, a short drop in from above. Its js_api is
# PanelBridge (panel_state, dismiss). Text goes in through textContent only.
# The status kinds (waiting: the open question and a hint, amber, a pause
# glyph; error: the plain reason, soft red, "!") are display-only: their page
# removes the close button, so they hold no control at all.
PANEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title></title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#161c26}
body{font:12.5px/1.4 'Segoe UI',system-ui,sans-serif;color:#e6f4f8;user-select:none;-webkit-user-select:none;cursor:default}
.panel{box-sizing:border-box;position:absolute;inset:0;padding:10px 30px 10px 12px;border-radius:12px;
  background:#161c26;border:1px solid rgba(235,252,255,.14);overflow:hidden;
  animation:drop .22s ease-out both}
@keyframes drop{from{transform:translateY(-28px);opacity:0}to{transform:none;opacity:1}}
.close{position:absolute;top:6px;right:6px;width:18px;height:18px;border:0;border-radius:50%;
  background:rgba(255,255,255,.08);color:#cfe7ee;font-size:12px;line-height:18px;padding:0;cursor:pointer}
.close:hover{background:rgba(255,255,255,.18)}
ul{list-style:none;margin:0;padding:0}
li{display:flex;gap:8px;padding:2px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
li .t{width:14px;flex:none;color:rgba(168,222,238,.9)}
li.fail .t{color:#f8a3a3}
li.run{opacity:.75}
.answer{overflow:auto;height:100%}
.panel.status{padding:10px 12px 10px 40px}
.panel.waiting{border-color:rgba(255,206,112,.55)}
.panel.error{border-color:rgba(255,150,140,.55)}
.status .glyph{position:absolute;left:12px;top:10px;width:18px;height:18px;border-radius:50%;
  font-size:11px;font-weight:700;line-height:18px;text-align:center;color:#161c26}
.waiting .glyph{background:rgba(255,214,140,.95)}
.error .glyph{background:rgba(255,200,196,.95)}
.status .q{max-height:56px;overflow:hidden}
.status .hint{margin-top:4px;font-size:11.5px;color:rgba(255,214,140,.95)}
@media (prefers-reduced-motion: reduce){.panel{animation:none !important}}
</style>
</head>
<body>
<div class="panel" id="panel"><div id="body"></div><button class="close" id="close" title="Close">&times;</button></div>
<script>
window.addEventListener('pywebviewready',()=>{
  const body=document.getElementById('body'),panel=document.getElementById('panel');
  const MARK={ok:'\u2713',fail:'\u2717',run:'\u2022'};
  const GLYPH={waiting:'\u275a\u275a',error:'!'};
  const close=document.getElementById('close');
  let last='';
  close.onclick=()=>{pywebview.api.dismiss();};
  function render(s){
    const key=JSON.stringify(s);
    if(key===last)return;
    last=key;
    body.textContent='';
    if(s.kind==='waiting'||s.kind==='error'){
      // Display-only: no close button, nothing to click or focus.
      if(close.parentNode)close.parentNode.removeChild(close);
      panel.className='panel status '+s.kind;
      const g=document.createElement('span');g.className='glyph';g.textContent=GLYPH[s.kind];
      const q=document.createElement('div');q.className='q';q.textContent=String(s.text||'');
      body.appendChild(g);body.appendChild(q);
      if(s.hint){
        const h=document.createElement('div');h.className='hint';h.textContent=String(s.hint);
        body.appendChild(h);
      }
    }else if(s.kind==='steps'){
      const ul=document.createElement('ul');
      for(const st of (s.steps||[])){
        const li=document.createElement('li');
        const state=['ok','fail','run'].includes(st.state)?st.state:'run';
        li.className=state;
        const t=document.createElement('span');t.className='t';t.textContent=MARK[state];
        const l=document.createElement('span');l.textContent=String(st.label||'');
        li.appendChild(t);li.appendChild(l);ul.appendChild(li);
      }
      body.appendChild(ul);
    }else{
      body.className='answer';
      body.textContent=String(s.text||'');
    }
  }
  async function poll(){
    try{render(await pywebview.api.panel_state());}catch(e){}
  }
  poll();
  setInterval(poll,300);
});
</script>
</body>
</html>"""
