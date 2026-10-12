const $=id=>document.getElementById(id);
const input=$("number-input"),display=$("dial-display"),hint=$("dial-hint"),state=$("connection-state");
const installBtn=$("install-btn"),remoteAudio=$("remote-audio"),settingsDialog=$("settings-dialog"),loginScreen=$("login-screen"),phoneApp=$("phone-app");
let deferredInstall=null,ua=null,currentSession=null,currentCallAnswered=false,currentCallStatus="completed",callStartedAt=null,timer=null,muted=false,held=false;

/* A sign-in the admin console handed this window for one extension. It lives in
   memory only: nothing from it is written to storage. */
let handoff=null;
const storeKey="eip-phone-settings";
const secretKey="eip-phone-sip-password";
const recentsKey="eip-phone-recents";
const contactsKey="eip-phone-contacts";

function settings(){
  if(handoff)return {...handoff};
  const publicSettings=JSON.parse(localStorage.getItem(storeKey)||"{}");
  return {...publicSettings,password:sessionStorage.getItem(secretKey)||""};
}
function saveSettings(s){
  if(handoff){handoff={...s};return}
  const publicSettings={extension:s.extension||"",username:s.username||"",domain:s.domain||"",wss:s.wss||"",apiToken:s.apiToken||""};
  localStorage.setItem(storeKey,JSON.stringify(publicSettings));
  if(s.password)sessionStorage.setItem(secretKey,s.password);
}
/* Microphone capture for a phone call: the browser's echo canceller, noise
   suppression and automatic gain keep the far end intelligible on a laptop or a
   handset without a headset, which is what a softphone is judged on. */
const MIC_CONSTRAINTS={audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false};
const cleanNumber=v=>String(v||"").replace(/[^0-9+*#]/g,"");
const sipDomain=()=>settings().domain||location.hostname;
const defaultWss=domain=>"wss://"+String(domain||location.hostname).replace(/^https?:\/\//,"").replace(/\/$/,"")+"/ws";
const sipTarget=n=>{const d=sipDomain();return /^sip:/i.test(n)?n:"sip:"+cleanNumber(n)+"@"+d};

function tickClock(){$("sb-time").textContent=new Date().toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}
tickClock();setInterval(tickClock,15000);
function setState(text,online=false){state.className="state "+(online?"online":"offline");state.innerHTML="<i></i> "+text}
function showNumber(){const v=input.value.trim();display.textContent=v||"Enter number";hint.textContent=v?"Ready to call":"Direct paste is supported"}
function addRecent(number,direction="outgoing",status="completed"){const rows=JSON.parse(localStorage.getItem(recentsKey)||"[]");rows.unshift({number,direction,status,at:Date.now()});localStorage.setItem(recentsKey,JSON.stringify(rows.slice(0,100)))}
function formatTime(ts){const d=new Date(ts);return d.toLocaleTimeString([], {hour:"numeric",minute:"2-digit"})}
function renderRecents(rows){
  const box=$("recents-list");box.innerHTML="";
  if(!rows.length){box.innerHTML='<div class="empty-state compact"><div class="empty-icon">◷</div><h3>No recent calls</h3><p>Your calls will appear here.</p></div>';return}
  rows.forEach(r=>{
    const el=document.createElement("div");el.className="list-item";
    el.innerHTML='<div class="avatar">'+(r.direction==="incoming"?"↓":"↑")+'</div><div class="item-main"><strong>'+escapeHtml(r.number||"Unknown")+'</strong><small>'+escapeHtml(r.status||"completed")+'</small></div><div class="item-side"><small>'+formatTime(r.at||Date.now())+'</small><button class="quick-call" title="Call">☎</button></div>';
    el.querySelector(".quick-call").onclick=()=>{input.value=r.number||"";showNumber();startCall()};
    box.appendChild(el)
  })
}
function escapeHtml(v){return String(v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]))}
async function loadRecents(){
  let rows=JSON.parse(localStorage.getItem(recentsKey)||"[]");const s=settings();
  if(s.apiToken){
    try{const r=await fetch("/api/v1/calls?limit=50",{headers:{Authorization:"Bearer "+s.apiToken}});
      if(r.ok){const d=await r.json();rows=(d.calls||[]).map(c=>({number:c.customer_number||c.destination||c.phone||c.call_id,status:c.status,direction:c.direction||"outgoing",at:c.started_at?Date.parse(c.started_at):Date.now()}))}
    }catch{}
  }
  renderRecents(rows)
}
function nav(viewId){document.querySelectorAll(".view").forEach(v=>v.classList.remove("active"));$(viewId).classList.add("active");document.querySelectorAll(".nav-item").forEach(n=>n.classList.toggle("active",n.dataset.view===viewId));if(viewId==="recents-view")loadRecents()}
document.querySelectorAll(".nav-item").forEach(b=>b.onclick=()=>nav(b.dataset.view));

document.querySelectorAll(".keypad button").forEach(b=>b.onclick=()=>{input.value+=b.dataset.key;showNumber()});
$("clear-btn").onclick=()=>{input.value="";showNumber();input.focus()};
$("backspace-btn").onclick=()=>{input.value=input.value.slice(0,-1);showNumber()};
$("paste-btn").onclick=async()=>{try{input.value=cleanNumber(await navigator.clipboard.readText());showNumber();input.focus()}catch{input.focus()}};
input.addEventListener("input",showNumber);
/* A physical keyboard is the fastest dial pad in a desktop browser: the digits
   behave like the on-screen keys, Enter calls and Escape hangs up. Anything
   typed into a field - the search box, the sign-in form, the settings dialog -
   keeps its own behaviour. */
document.addEventListener("keydown",e=>{
  if(e.ctrlKey||e.metaKey||e.altKey)return;
  const target=e.target||document.body;
  const typing=/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName||"");
  const dialogOpen=settingsDialog.hasAttribute("open");
  const phoneVisible=!phoneApp.classList.contains("hidden");
  if(dialogOpen||!phoneVisible)return;
  // Enter dials from anywhere the phone owns - but a focused button or link
  // keeps its native Enter activation instead of being hijacked into a call.
  if(e.key==="Enter"){
    if(!/^(BUTTON|A|SUMMARY)$/.test(target.tagName||"")){e.preventDefault();startCall()}
    return
  }
  if(e.key==="Escape"){if(currentSession){e.preventDefault();endCall("Call ended",true,"completed")}return}
  if(typing)return;
  if(/^[0-9*#]$/.test(e.key)){e.preventDefault();input.value+=e.key;showNumber();return}
  if(e.key==="Backspace"){e.preventDefault();input.value=input.value.slice(0,-1);showNumber()}
});
input.addEventListener("paste",()=>setTimeout(()=>{input.value=cleanNumber(input.value);showNumber()},0));

/* One screen at a time: the sign-in form, the console hand-off, or the phone. */
function showPhone(){loginScreen.classList.add("hidden");$("console-screen").classList.add("hidden");phoneApp.classList.remove("hidden")}
function showConsoleScreen(title,text){
  loginScreen.classList.add("hidden");phoneApp.classList.add("hidden");$("console-screen").classList.remove("hidden");
  $("console-title").textContent=title;$("console-status").textContent=text;
}
function showLogin(message="",error=false){
  $("console-screen").classList.add("hidden");
  phoneApp.classList.add("hidden");loginScreen.classList.remove("hidden");
  $("login-status").className="login-status"+(error?" error":"");$("login-status").textContent=message;
  $("login-password").value="";
  $("login-username").value=settings().username||"";
  $("login-domain").value=settings().domain||"";
  $("login-wss").value=settings().wss||"";
}
function setLoginBusy(busy){$("login-btn").disabled=busy;$("login-btn").textContent=busy?"Authenticating…":"Connect & Sign In"}

/* ---------------------------------------------------------------- HD audio --
   Wideband codecs (G.722, Opus) are negotiated by the SIP stack, not by this
   page, so the phone reports what was actually negotiated and how the network
   is treating it. That is also the honest answer to "is this call HD": the far
   end and the carrier have a say, and the badge names the codec in use. */
const WIDEBAND=/g722|opus|slin16/i;
let qualityTimer=null;

function statsRows(report){
  const rows=[];
  if(!report)return rows;
  if(typeof report.forEach==="function")report.forEach(row=>rows.push(row));
  else if(Array.isArray(report))rows.push(...report);
  else if(typeof report[Symbol.iterator]==="function")rows.push(...report);
  return rows;
}
function readQuality(report){
  const rows=statsRows(report);
  const codecs=new Map(rows.filter(r=>r.type==="codec").map(r=>[r.id,r]));
  const audio=r=>r.type.endsWith("-rtp")&&(r.kind==="audio"||r.mediaType==="audio");
  const inbound=rows.filter(r=>r.type==="inbound-rtp"&&audio(r));
  const leg=[...inbound,...rows.filter(r=>r.type==="outbound-rtp"&&audio(r))]
    .find(r=>r.codecId&&codecs.get(r.codecId)&&!/telephone-event/i.test(codecs.get(r.codecId).mimeType||""));
  if(!leg)return null;
  const name=String(codecs.get(leg.codecId).mimeType||"").split("/").pop().toUpperCase();
  const received=Number(inbound[0]?.packetsReceived)||0;
  const lost=Number(inbound[0]?.packetsLost)||0;
  const loss=(received+lost)>0?lost/(received+lost):0;
  const jitter=Number(inbound[0]?.jitter)||0;
  // Wideband is "HD" (G.722 is 16 kHz); anything else is a normal narrowband
  // call. High loss or jitter means the network, not the codec, is the problem.
  const hd=WIDEBAND.test(name);
  const unstable=loss>0.05||jitter>0.04;
  return {name,hd,unstable,label:(hd?"HD · ":"Standard · ")+name+(unstable?" · unstable network":"")};
}
function stopQualityWatch(){clearInterval(qualityTimer);qualityTimer=null;$("call-quality").classList.add("hidden")}
function startQualityWatch(session){
  stopQualityWatch();
  const badge=$("call-quality");
  const show=report=>{
    const quality=readQuality(report);
    if(!quality){badge.classList.add("hidden");return}
    badge.textContent=quality.label;
    badge.classList.toggle("good",quality.hd&&!quality.unstable);
    badge.classList.toggle("warn",quality.unstable);
    badge.classList.remove("hidden")
  };
  const tick=async()=>{
    const pc=session.connection;
    if(!pc||typeof pc.getStats!=="function"){badge.classList.add("hidden");return}
    try{show(await pc.getStats())}catch{badge.classList.add("hidden")}
  };
  badge.textContent="Checking audio…";badge.classList.remove("hidden","good","warn");
  tick();
  qualityTimer=setInterval(tick,2000)
}

function attachRemoteAudio(session){
  session.on("peerconnection",e=>{
    const pc=e.peerconnection||e;
    pc.ontrack=ev=>{if(ev.streams&&ev.streams[0]){remoteAudio.srcObject=ev.streams[0];remoteAudio.play().catch(()=>{})}}
  })
}
function attachSession(session,incoming=false){
  currentSession=session;currentCallAnswered=false;currentCallStatus="completed";
  muted=false;held=false;remoteAudio.muted=false;
  ["mute-btn","hold-btn","speaker-btn"].forEach(id=>$(id).classList.remove("active"));
  $("active-number").textContent=input.value||"Unknown";
  $("active-call-view").classList.remove("hidden");
  $("incoming-actions").classList.toggle("hidden",!incoming);
  $("hangup-btn").classList.toggle("hidden",incoming);
  $("active-label").textContent=incoming?"Incoming call":"Calling";
  callStartedAt=incoming?null:Date.now();startTimer();
  $("active-status").textContent=incoming?"Incoming call":"Connecting…";
  session.on("progress",()=>{$("active-status").textContent="Ringing…"});
  session.on("accepted",()=>{currentCallAnswered=true;$("active-status").textContent="Connected";callStartedAt=callStartedAt||Date.now();startTimer();startQualityWatch(session)});
  session.on("confirmed",()=>{currentCallAnswered=true;$("active-status").textContent="Connected";callStartedAt=callStartedAt||Date.now();startTimer();startQualityWatch(session)});
  session.on("ended",()=>endCall("Call ended",false,"completed"));
  session.on("failed",e=>endCall("Call failed"+(e?.cause?": "+e.cause:""),false,"failed"));
  attachRemoteAudio(session)
}
function startTimer(){
  clearInterval(timer);timer=setInterval(()=>{
    if(!callStartedAt)return;
    const sec=Math.max(0,Math.floor((Date.now()-callStartedAt)/1000));
    $("call-timer").textContent=String(Math.floor(sec/60)).padStart(2,"0")+":"+String(sec%60).padStart(2,"0")
  },500)
}
/**
 * Close the active call: end the SIP session if this call ends it, file the
 * call in recents once, and put the dialer back.
 *
 * A session can also end itself - the far end hangs up, or terminate()
 * reports back through the session's own "ended" event before this function
 * resumes. Whichever path runs first closes the screen, and the other one
 * finds currentSession pointing elsewhere (or at nothing) and stops, so one
 * call can never be filed twice.
 */
function endCall(message="Call ended",terminate=true,status="completed"){
  const session=currentSession;
  if(!session)return;
  if(terminate){currentCallStatus=status;try{if(!session.isEnded?.())session.terminate()}catch{}}
  if(currentSession!==session)return;
  const number=input.value.trim();
  const direction=session.direction==="incoming"?"incoming":"outgoing";
  // The pressing of a button and the session's own "ended" event can both
  // reach here; whichever ran last decided why the call ended.
  const outcome=status==="completed"?currentCallStatus:status;
  if(number)addRecent(number,direction,outcome==="completed"&&direction==="incoming"&&!currentCallAnswered?"missed":outcome);
  currentSession=null;clearInterval(timer);callStartedAt=null;stopQualityWatch();
  $("active-status").textContent=message;$("active-call-view").classList.add("hidden");$("call-timer").textContent="00:00";
  $("call-btn").disabled=false;$("incoming-actions").classList.add("hidden");$("hangup-btn").classList.remove("hidden");loadRecents()
}

function connectSip(s){
  return new Promise((resolve,reject)=>{
    if(!window.JsSIP)return reject(new Error("SIP library is not loaded."));
    if(!s.username||!s.password||!s.domain||!s.wss)return reject(new Error("Enter SIP username, password and domain."));
    try{
      if(ua){try{ua.stop()}catch{}ua=null}
      const socket=new JsSIP.WebSocketInterface(s.wss);
      ua=new JsSIP.UA({sockets:[socket],uri:"sip:"+s.username+"@"+s.domain,password:s.password,session_timers:false});
      let settled=false;
      const fail=message=>{if(settled)return;settled=true;setState("Registration failed");reject(new Error(message||"SIP registration failed"))};
      ua.on("connected",()=>setState("Online",true));
      ua.on("disconnected",()=>{setState("Offline");if(!settled)fail("SIP WebSocket disconnected.")});
      ua.on("registered",()=>{setState("Ready",true);showPhone();$("login-status").className="login-status ok";$("login-status").textContent="Authenticated and registered.";if(!settled){settled=true;resolve(true)}});
      ua.on("registrationFailed",e=>fail(e.cause||"SIP authentication failed."));
      ua.on("newRTCSession",e=>{
        const session=e.session;
        if(session.direction==="incoming"){
          if(currentSession){try{session.terminate()}catch{};return}
          const caller=session.remote_identity?.uri?.user||"Unknown";
          input.value=caller;showNumber();attachSession(session,true)
        }
      });
      ua.start()
    }catch(err){ua=null;reject(err)}
  })
}

async function startCall(){
  const number=cleanNumber(input.value);if(!number){input.focus();return}
  if(!ua){showLogin("Sign in to your SIP account first.");return}
  if(currentSession)return;
  try{
    const session=ua.call(sipTarget(number),{mediaConstraints:MIC_CONSTRAINTS,pcConfig:{iceServers:[]},rtcOfferConstraints:{offerToReceiveAudio:true,offerToReceiveVideo:false}});
    attachSession(session,false);$("call-btn").disabled=true
  }catch(err){$("dial-hint").textContent=err.message}
}
$("call-btn").onclick=startCall;

$("answer-btn").onclick=()=>{
  if(!currentSession)return;
  try{currentSession.answer({mediaConstraints:MIC_CONSTRAINTS,pcConfig:{iceServers:[]}});currentCallAnswered=true;$("incoming-actions").classList.add("hidden");$("hangup-btn").classList.remove("hidden");$("active-label").textContent="Call";callStartedAt=Date.now();startTimer()}catch(e){$("active-status").textContent=e.message}
};
$("decline-btn").onclick=()=>endCall("Call declined",true,"declined");
$("hangup-btn").onclick=()=>endCall("Call ended",true,"completed");
$("mute-btn").onclick=()=>{if(!currentSession)return;muted=!muted;currentSession.mute({audio:muted});$("mute-btn").classList.toggle("active",muted)};
$("hold-btn").onclick=()=>{if(!currentSession)return;held=!held;held?currentSession.hold():currentSession.unhold();$("hold-btn").classList.toggle("active",held)};
$("speaker-btn").onclick=()=>{
  const on=!$("speaker-btn").classList.contains("active");
  $("speaker-btn").classList.toggle("active",on);
  // Chrome can move the call to the loudspeaker; where the browser has no
  // output routing (iOS Safari), the button falls back to muting the earpiece
  // so the caller's own microphone is never what gets silenced.
  if(typeof remoteAudio.setSinkId==="function"){
    remoteAudio.setSinkId(on?"speaker":"default").catch(()=>{remoteAudio.muted=on})
    return
  }
  remoteAudio.muted=on
};

$("login-form").onsubmit=async e=>{
  e.preventDefault();
  const domain=$("login-domain").value.trim().replace(/^https?:\/\//,"").replace(/\/$/,"");
  const s={...settings(),username:$("login-username").value.trim(),password:$("login-password").value,domain,wss:$("login-wss").value.trim()||defaultWss(domain)};
  if(!s.username||!s.password||!s.domain)return;
  saveSettings(s);setLoginBusy(true);$("login-status").className="login-status";$("login-status").textContent="Connecting to SIP server…";
  try{await connectSip(s)}catch(err){$("login-status").className="login-status error";$("login-status").textContent=err.message||"Authentication failed.";setState("Offline");try{if(ua)ua.stop()}catch{}ua=null}
  finally{setLoginBusy(false)}
};

$("settings-btn").onclick=()=>{
  const s=settings();
  $("sip-extension").value=s.extension||"";
  $("sip-username").value=s.username||"";
  $("sip-domain").value=s.domain||"";
  $("sip-wss").value=s.wss||defaultWss(s.domain);
  $("api-token").value=s.apiToken||"";
  settingsDialog.showModal()
};
$("connect-btn").onclick=async()=>{
  const old=settings(),domain=$("sip-domain").value.trim().replace(/^https?:\/\//,"").replace(/\/$/,"");
  const s={...old,extension:$("sip-extension").value.trim(),username:$("sip-username").value.trim(),domain,
           wss:$("sip-wss").value.trim()||defaultWss(domain),apiToken:$("api-token").value.trim()};
  saveSettings(s);$("settings-status").className="settings-status";$("settings-status").textContent="Reconnecting…";
  try{await connectSip(s);$("settings-status").className="settings-status ok";$("settings-status").textContent="Phone connected."}catch(err){$("settings-status").className="settings-status error";$("settings-status").textContent=err.message}
};
$("logout-btn").onclick=()=>{
  try{if(ua)ua.stop()}catch{}ua=null;handoff=null;currentSession=null;sessionStorage.removeItem(secretKey);localStorage.removeItem(storeKey);settingsDialog.close();setState("Offline");showLogin("You have been logged out.");$("login-password").focus()
};
$("refresh-recents").onclick=loadRecents;
$("contacts-btn").onclick=()=>nav("contacts-view");
$("new-message").onclick=()=>alert("Messaging provider integration can be enabled here.");
$("add-contact").onclick=()=>{
  const name=prompt("Contact name"),phone=prompt("Phone number");if(!name||!phone)return;
  const rows=JSON.parse(localStorage.getItem(contactsKey)||"[]");rows.push({name,phone});localStorage.setItem(contactsKey,JSON.stringify(rows));renderContacts()
};
function renderContacts(){
  const rows=JSON.parse(localStorage.getItem(contactsKey)||"[]"),box=$("contacts-list");box.innerHTML="";$("contacts-empty").classList.toggle("hidden",!!rows.length);
  rows.forEach(r=>{const el=document.createElement("div");el.className="list-item";el.innerHTML='<div class="avatar">'+escapeHtml((r.name||"?")[0].toUpperCase())+'</div><div class="item-main"><strong>'+escapeHtml(r.name)+'</strong><small>'+escapeHtml(r.phone)+'</small></div><button class="quick-call">☎</button>';el.querySelector("button").onclick=()=>{input.value=r.phone;showNumber();nav("dialer-view");startCall()};box.appendChild(el)})
}
window.addEventListener("beforeinstallprompt",e=>{e.preventDefault();deferredInstall=e;installBtn.classList.remove("hidden")});
installBtn.onclick=async()=>{if(!deferredInstall)return;deferredInstall.prompt();await deferredInstall.userChoice;deferredInstall=null;installBtn.classList.add("hidden")};
window.addEventListener("appinstalled",()=>installBtn.classList.add("hidden"));
if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js").catch(()=>{});

const fromConsole=new URLSearchParams(location.search).get("connect")==="extension"&&!!window.opener;
const saved=settings();
$("login-username").value=saved.username||"";
$("login-domain").value=saved.domain||"";
$("login-wss").value=saved.wss||"";
renderContacts();loadRecents();showNumber();setState("Offline");
if(!fromConsole&&saved.username&&saved.domain&&saved.password){
  $("login-status").textContent="Previous SIP session found. Reconnecting…";
  connectSip(saved).catch(()=>showLogin("Please sign in again.",true));
}

/* Opened from the admin console's Softphone button (`/phone?connect=extension`).
   The console hands this window one extension's sign-in by postMessage - never in
   the URL - and only from the window that opened it. A previous phone session in
   this browser is not reused. Until that sign-in arrives the window says so; if it
   never does, the sign-in form is offered instead of a spinner that never stops. */
let consoleHandoffSeen=false;
if(fromConsole){
  showConsoleScreen("Softphone","Waiting for the EngineerIP console to sign this window in…");
  window.addEventListener("message",event=>{
    if(event.source!==window.opener||event.origin!==location.origin)return;
    const m=event.data||{};
    if(m.type==="eip-softphone:connect"&&m.sip_username&&m.sip_password){consoleHandoffSeen=true;startConsoleSession(m)}
  });
  setTimeout(()=>{if(!consoleHandoffSeen&&!ua)showLogin("The console did not sign this window in. Enter the extension's SIP details here, or close this window.",true)},15000);
  window.opener.postMessage({type:"eip-softphone:ready"},location.origin);
}
async function startConsoleSession(m){
  const label=m.label||m.sip_username;
  handoff={extension:m.extension||"",username:m.sip_username,password:m.sip_password,domain:m.domain,wss:m.wss||defaultWss(m.domain),apiToken:""};
  try{if(ua)ua.stop()}catch{}ua=null;
  showConsoleScreen(label,`Signing in as ${label}…`);
  try{await connectSip(handoff);document.title=`Softphone · ${label}`;$("brand-title").textContent=label}
  catch(err){showLogin(err.message||"Could not sign this extension in.",true)}
}
