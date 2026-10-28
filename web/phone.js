const doc=document;
const $=id=>doc.getElementById(id);
const input=$("number-input"),display=$("dial-display"),hint=$("dial-hint"),state=$("connection-state");
const installBtn=$("install-btn"),remoteAudio=$("remote-audio"),settingsDialog=$("settings-dialog"),loginScreen=$("login-screen"),phoneApp=$("phone-app");
let deferredInstall=null,ua=null,currentSession=null,currentCallAnswered=false,currentCallStatus="completed",callStartedAt=null,timer=null,muted=false,held=false;
let currentSipSettings=null,remoteStream=null,audioPlayPending=false,sipEverRegistered=false;
let keepaliveTimer=null,reconnectTimer=null,qualityTimer=null,preparationTimer=null,currentIceCleanup=null;
/* JsSIP waits for ICE gathering to finish before sending an INVITE or a 200
   answer. Some unreachable STUN/TURN servers never finish before the PBX's
   ring timeout. Once candidates exist, bound that wait without inventing any. */
const ICE_GATHER_LIMIT_MS=8000,CALL_SETUP_LIMIT_MS=20000;
/* A REGISTER is both a contact refresh and a WebSocket keepalive. Some reverse
   proxies/CDNs close an idle WSS around 100-120 seconds; without traffic the
   phone silently stops receiving calls until the page is reloaded. */
const KEEPALIVE_MS=30000,RECONNECT_WATCHDOG_MS=9000;

/* A sign-in the admin console handed this window for one extension. It lives in
   memory only: nothing from it is written to storage. */
let handoff=null;
const storeKey="eip-phone-settings";
const secretKey="eip-phone-sip-password";
const recentsKey="eip-phone-recents";
const contactsKey="eip-phone-contacts";

function settings(){
  if(handoff)return {...handoff};
  const saved=localStorage.getItem(storeKey);
  const publicSettings=JSON.parse(saved||"{}");
  // Older builds persisted the SIP domain as the default socket host.
  // Migrate that exact legacy default once; keep other custom endpoints.
  if (saved && !publicSettings.wssOriginVersion) {
    const legacy="wss://"+String(publicSettings.domain||"").replace(/^https?:\/\//,"").replace(/\/$/,"")+"/ws";
    if (publicSettings.wss===legacy) publicSettings.wss=defaultWss();
    publicSettings.wssOriginVersion=1;
    localStorage.setItem(storeKey,JSON.stringify(publicSettings));
  }
  return {...publicSettings,password:sessionStorage.getItem(secretKey)||""};
}
function saveSettings(s){
  if(handoff){handoff={...s};return}
  const publicSettings={extension:s.extension||"",username:s.username||"",domain:s.domain||"",wss:s.wss||"",apiToken:s.apiToken||"",wssOriginVersion:1};
  localStorage.setItem(storeKey,JSON.stringify(publicSettings));
  if(s.password)sessionStorage.setItem(secretKey,s.password);
}
/* Microphone capture for a phone call: the browser's echo canceller, noise
   suppression and automatic gain keep the far end intelligible on a laptop or a
   handset without a headset, which is what a softphone is judged on. */
const MIC_CONSTRAINTS={audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false};
/* How the browser finds a path its audio can travel. With no STUN server a
   browser behind a router only offers its private address - Asterisk has
   nothing to send audio to and the call rings in silence. These public STUN
   servers let it discover its own public address; the console can hand the
   phone a different list (own STUN, or TURN where the network needs it) with
   the extension's sign-in. */
const DEFAULT_ICE_SERVERS=[{urls:"stun:stun.l.google.com:19302"},{urls:"stun:stun1.l.google.com:19302"}];
let iceServers=DEFAULT_ICE_SERVERS;
const iceConfig=()=>({iceServers:Array.isArray(iceServers)&&iceServers.length?iceServers:DEFAULT_ICE_SERVERS});
const cleanNumber=v=>String(v||"").replace(/[^0-9+*#]/g,"");
const sipDomain=()=>settings().domain||location.hostname;
/* Where the SIP WebSocket lives: this page's own host, never the SIP domain.
   The SIP address is for REGISTER addressing only - it is deliberately a bare
   IP or an unproxied DNS record (SIP/RTP are UDP and cannot traverse a web
   proxy), so a wss:// URL built from it either fails TLS outright (an IP
   literal cannot hold a certificate) or needs a second HTTPS vhost nobody
   builds. The host serving this page already has HTTPS with a real
   certificate; one proxied /ws path on it carries the signaling. */
const defaultWss=()=>"wss://"+location.host+"/ws";
const sipTarget=n=>{const d=sipDomain();return /^sip:/i.test(n)?n:"sip:"+cleanNumber(n)+"@"+d};

function tickClock(){$("sb-time").textContent=new Date().toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}
tickClock();setInterval(tickClock,15000);
function setState(text,mode=false){
  const cls=mode===true?"online":mode==="connecting"?"connecting":"offline";
  state.className="state "+cls;state.innerHTML="<i></i> "+text
}
function setCallStatus(text){const el=$("active-status");if(el)el.textContent=text}
function setCallPhase(phase){const el=$("active-call-view");if(el)el.dataset.phase=phase}
function showAudioUnlock(visible,message){
  const btn=$("enable-audio-btn");if(!btn)return;
  btn.classList.toggle("hidden",!visible);
  if(message)btn.textContent=message
}
function playRemoteAudio(){
  audioPlayPending=true;
  remoteAudio.muted=false;
  let attempt;
  try{attempt=remoteAudio.play()}catch{attempt=null}
  if(attempt&&typeof attempt.then==="function"){
    attempt.then(()=>{audioPlayPending=false;showAudioUnlock(false)})
      .catch(()=>showAudioUnlock(true,"🔊 Tap to enable call audio"))
  }else{audioPlayPending=false;showAudioUnlock(false)}
}
function resetRemoteAudio(){
  try{remoteAudio.pause()}catch{}
  remoteStream=null;remoteAudio.srcObject=null;remoteAudio.muted=false;
  audioPlayPending=false;showAudioUnlock(false)
}
/* Browsers sometimes need a user gesture before an auto-playing media element
   will make a sound. The call and answer buttons are gestures; a later tap
   anywhere can still unlock the remote track if that first attempt was blocked. */
doc.addEventListener("pointerdown",()=>{if(audioPlayPending)playRemoteAudio()},{capture:true,passive:true});
function showNumber(){const v=input.value.trim();display.textContent=v||"Enter number";hint.classList.remove("error");hint.textContent=v?"Ready to call":"Direct paste is supported"}
function showDialError(message){nav("dialer-view");hint.classList.add("error");hint.textContent=message}
function addRecent(number,direction="outgoing",status="completed"){const rows=JSON.parse(localStorage.getItem(recentsKey)||"[]");rows.unshift({number,direction,status,at:Date.now()});localStorage.setItem(recentsKey,JSON.stringify(rows.slice(0,100)))}
function formatTime(ts){const d=new Date(ts);return d.toLocaleTimeString([], {hour:"numeric",minute:"2-digit"})}
function renderRecents(rows){
  const box=$("recents-list");box.innerHTML="";
  if(!rows.length){box.innerHTML='<div class="empty-state compact"><div class="empty-icon">◷</div><h3>No recent calls</h3><p>Your calls will appear here.</p></div>';return}
  rows.forEach(r=>{
    const el=doc.createElement("div");el.className="list-item";
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
function nav(viewId){doc.querySelectorAll(".view").forEach(v=>v.classList.remove("active"));$(viewId).classList.add("active");doc.querySelectorAll(".nav-item").forEach(n=>n.classList.toggle("active",n.dataset.view===viewId));if(viewId==="recents-view")loadRecents()}
doc.querySelectorAll(".nav-item").forEach(b=>b.onclick=()=>nav(b.dataset.view));

doc.querySelectorAll(".keypad button").forEach(b=>b.onclick=()=>{input.value+=b.dataset.key;showNumber()});
$("clear-btn").onclick=()=>{input.value="";showNumber();input.focus()};
$("backspace-btn").onclick=()=>{input.value=input.value.slice(0,-1);showNumber()};
$("paste-btn").onclick=async()=>{try{input.value=cleanNumber(await navigator.clipboard.readText());showNumber();input.focus()}catch{input.focus()}};
input.addEventListener("input",showNumber);
/* A physical keyboard is the fastest dial pad in a desktop browser: the digits
   behave like the on-screen keys, Enter calls and Escape hangs up. Anything
   typed into a field - the search box, the sign-in form, the settings dialog -
   keeps its own behaviour. */
doc.addEventListener("keydown",e=>{
  if(e.ctrlKey||e.metaKey||e.altKey)return;
  const target=e.target||doc.body;
  const typing=/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName||"");
  const dialogOpen=settingsDialog.hasAttribute("open");
  const phoneVisible=!phoneApp.classList.contains("hidden");
  if(dialogOpen||!phoneVisible)return;
  // Enter dials from anywhere the phone owns - but a focused button or link
  // keeps its native Enter activation instead of being hijacked into a call.
  if(e.key==="Enter"){
    if(currentSession)return;
    if(!/^(BUTTON|A|SUMMARY)$/.test(target.tagName||"")){e.preventDefault();startCall()}
    return
  }
  if(e.key==="Escape"){if(currentSession){e.preventDefault();endCall("Call ended",true,"completed")}return}
  if(typing)return;
  // During a call, the keyboard is an IVR keypad rather than a second dialer.
  if(currentSession&&/^[0-9*#]$/.test(e.key)){e.preventDefault();sendDtmf(e.key);return}
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

function ensureRemoteStream(preferred){
  if(preferred)remoteStream=preferred;
  else if(!remoteStream){
    const Stream=window.MediaStream||window.webkitMediaStream;
    if(Stream)remoteStream=new Stream();
    else{
      // jsdom/older engines: a minimal MediaStream-compatible holder.
      const tracks=[];
      remoteStream={getTracks:()=>tracks.slice(),addTrack:t=>{if(!tracks.includes(t))tracks.push(t)}};
    }
  }
  remoteAudio.srcObject=remoteStream;
  return remoteStream
}
function addRemoteTrack(ev){
  const preferred=ev&&ev.streams&&ev.streams[0];
  const track=ev&&ev.track;
  if(!preferred&&!track)return;
  if(!preferred&&track.kind&&track.kind!=="audio")return;
  const stream=ensureRemoteStream(preferred);
  if(track&&(!track.kind||track.kind==="audio")&&typeof stream.getTracks==="function"&&typeof stream.addTrack==="function"&&!stream.getTracks().includes(track)){
    try{stream.addTrack(track)}catch{}
  }
  playRemoteAudio()
}
function bindPeer(pc){
  if(!pc||pc.__eipAudioBound)return;
  pc.__eipAudioBound=true;
  const handler=ev=>{if(!currentSession||currentSession.connection!==pc)return;if(ev)ev.__eipHandled=true;addRemoteTrack(ev)};
  if(typeof pc.addEventListener==="function"){
    pc.addEventListener("track",handler);
    // Keep ontrack for browsers/tests that dispatch through the property, but
    // never handle one event twice when both dispatchers fire.
    pc.ontrack=ev=>{if(!ev||!ev.__eipHandled)handler(ev)};
    pc.addEventListener("iceconnectionstatechange",()=>{
      if(!currentSession||currentSession.connection!==pc)return;
      const ice=pc.iceConnectionState;
      if(ice==="checking")setCallStatus(currentCallAnswered?"Connecting audio…":"Preparing call and network connection…");
      else if((ice==="connected"||ice==="completed")&&currentCallAnswered)setCallStatus("Connected");
      else if(ice==="disconnected")setCallStatus("Network interrupted — reconnecting audio…");
      else if(ice==="failed")setCallStatus("Media connection failed. Check ICE/TURN and the server's RTP UDP ports.")
    })
  }else{
    const previous=pc.ontrack;
    pc.ontrack=ev=>{if(typeof previous==="function")previous(ev);if(!ev||!ev.__eipHandled)handler(ev)}
  }
  // An outgoing ua.call() creates its peer connection and emits the
  // "peerconnection" event synchronously, before attachSession() can subscribe.
  // Bind the already-created connection too, and recover any receiver tracks.
  if(typeof pc.getReceivers==="function"){
    try{pc.getReceivers().forEach(receiver=>{if(receiver.track)addRemoteTrack({track:receiver.track,streams:[]})})}catch{}
  }
}
function attachRemoteAudio(session){
  bindPeer(session&&session.connection);
  session.on("peerconnection",e=>{if(currentSession===session)bindPeer((e&&e.peerconnection)||e)})
}
function clearPreparationTimer(){if(preparationTimer){clearTimeout(preparationTimer);preparationTimer=null;}}
function startPreparationTimer(session){
  clearPreparationTimer();
  preparationTimer=setTimeout(()=>{
    preparationTimer=null;
    if(currentSession!==session||currentCallAnswered)return;
    endCall("Call setup timed out. Allow microphone access and check the ICE/STUN/TURN connection, then try again.",true,"failed");
  },CALL_SETUP_LIMIT_MS)
}
function attachSession(session,incoming=false){
  currentSession=session;currentCallAnswered=false;currentCallStatus="completed";
  muted=false;held=false;resetRemoteAudio();
  ["mute-btn","hold-btn","speaker-btn","dtmf-btn"].forEach(id=>$(id).classList.remove("active"));
  $("dtmf-panel").classList.add("hidden");$("dtmf-btn").setAttribute("aria-expanded","false");
  $("active-number").textContent=input.value||"Unknown";
  $("active-call-view").classList.remove("hidden");
  $("incoming-actions").classList.toggle("hidden",!incoming);
  $("hangup-btn").classList.toggle("hidden",incoming);
  $("active-label").textContent=incoming?"Incoming call":"Calling";
  $("call-btn").disabled=true;
  callStartedAt=null;startTimer();
  setCallStatus(incoming?"Incoming call":"Preparing call… Allow microphone access if prompted.");setCallPhase(incoming?"incoming":"dialing");
  const active=()=>currentSession===session;
  // JsSIP normally waits for ICE gathering to finish before emitting the
  // outgoing INVITE or the incoming 200 OK. Give STUN/TURN a chance, but once
  // at least one candidate exists, do not wait beyond the PBX's ring timeout.
  let candidateTimer=null,releaseCandidate=null;
  const clearCandidateTimer=()=>{if(candidateTimer!==null)clearTimeout(candidateTimer);candidateTimer=null;releaseCandidate=null};
  currentIceCleanup=clearCandidateTimer;
  session.on("icecandidate",event=>{
    if(!active()||!event||typeof event.ready!=="function")return;
    releaseCandidate=event.ready;
    if(candidateTimer===null)candidateTimer=setTimeout(()=>{
      const ready=releaseCandidate;candidateTimer=null;releaseCandidate=null;
      if(active()&&ready)ready()
    },ICE_GATHER_LIMIT_MS)
  });
  session.on("sdp",event=>{if(event?.originator==="local")clearCandidateTimer()});
  session.on("sending",()=>{if(active())clearPreparationTimer()});
  session.on("progress",()=>{if(active()){setCallStatus("Ringing…");setCallPhase("ringing")}});
  const accepted=()=>{
    if(!active())return;
    clearPreparationTimer();currentCallAnswered=true;$("active-label").textContent="Call";
    const ice=session.connection?.iceConnectionState;
    setCallStatus(ice==="connected"||ice==="completed"?"Connected":"Connecting audio…");
    setCallPhase("connected");playRemoteAudio();callStartedAt=callStartedAt||Date.now();startTimer();startQualityWatch(session)
  };
  session.on("accepted",accepted);session.on("confirmed",accepted);
  session.on("getusermediafailed",()=>{if(active())endCall("Microphone unavailable. Allow microphone access and retry.",true,"failed")});
  // JsSIP can emit failed before getusermediafailed, and an incoming SDP
  // creation error may otherwise leave the screen stuck after it replies 500.
  ["peerconnection:createofferfailed","peerconnection:createanswerfailed","peerconnection:setlocaldescriptionfailed"].forEach(name=>
    session.on(name,()=>{if(active()&&!currentCallAnswered)endCall("Could not prepare call audio. Check microphone and browser audio permissions.",true,"failed")})
  );
  const mediaFailure="Media connection failed. Check ICE/TURN and the server's RTP UDP ports.";
  session.on("ended",e=>{
    clearCandidateTimer();if(!active())return;
    if(/RTP Timeout/i.test(String(e?.cause||"")))endCall(mediaFailure,false,"failed");
    else endCall("Call ended",false,"completed")
  });
  session.on("failed",e=>{
    clearCandidateTimer();if(!active())return;
    const cause=String(e?.cause||"");
    const message=/user denied media access/i.test(cause)?"Microphone unavailable. Allow microphone access and retry.":/RTP Timeout/i.test(cause)?mediaFailure:"Call failed"+(cause?": "+cause:"");
    endCall(message,false,"failed")
  });
  attachRemoteAudio(session);
  if(!incoming)startPreparationTimer(session)
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
 * reports back through the session's own "ended" event synchronously. Claim
 * currentSession before terminating, so that nested event cannot overwrite the
 * reason (for example "Microphone unavailable") with a generic "Call ended".
 */
function endCall(message="Call ended",terminate=true,status="completed"){
  const session=currentSession;
  if(!session)return;
  currentSession=null;
  clearPreparationTimer();if(currentIceCleanup)currentIceCleanup();currentIceCleanup=null;
  if(terminate){currentCallStatus=status;try{if(!session.isEnded?.())session.terminate()}catch{}}
  const number=input.value.trim();
  const direction=session.direction==="incoming"?"incoming":"outgoing";
  const outcome=status==="completed"?currentCallStatus:status;
  if(number)addRecent(number,direction,outcome==="completed"&&direction==="incoming"&&!currentCallAnswered?"missed":outcome);
  clearInterval(timer);callStartedAt=null;stopQualityWatch();resetRemoteAudio();setCallPhase("ended");
  $("dtmf-panel").classList.add("hidden");$("dtmf-btn").setAttribute("aria-expanded","false");
  setCallStatus(message);$("active-call-view").classList.add("hidden");$("call-timer").textContent="00:00";
  if(status==="failed")showDialError(message); // Keep the reason visible after the call view closes.
  $("call-btn").disabled=false;$("incoming-actions").classList.add("hidden");$("hangup-btn").classList.remove("hidden");loadRecents()
}

/* The SIP stack is vendored at /jssip.min.js and loaded before this file, with
   the pinned upstream build as a fallback in the page. A copy of the page
   cached from before the vendoring - or a fetch that failed while the page was
   loading - can still arrive here with no JsSIP, so try both copies again
   before giving up. */
function loadScript(src){return new Promise(resolve=>{const el=doc.createElement("script");el.src=src;el.onload=()=>resolve(true);el.onerror=()=>resolve(false);doc.head.appendChild(el)})}
async function ensureSipLibrary(){
  if(window.JsSIP)return true;
  await loadScript("/jssip.min.js");
  if(!window.JsSIP)await loadScript("https://cdn.jsdelivr.net/gh/versatica/JsSIP@3.10.1/dist/jssip.min.js");
  return !!window.JsSIP;
}

/* "SIP WebSocket disconnected." cannot say on its own whether the reverse
   proxy has no /ws route, cannot reach Asterisk's listener, or the host is
   unreachable. Ask the HTTPS endpoint behind the wss:// URL what it answers
   and report the evidence. A plain HTTP probe is not a WebSocket handshake:
   a 404 may come from the proxy OR Asterisk, and 400/426 can be normal for a
   handler that requires Upgrade and the SIP subprotocol. */
async function wssDiagnosis(wssUrl){
  let probe;
  try{probe=new URL(wssUrl)}catch{return" "+wssUrl+" is not a valid WebSocket address."}
  const isIp=/^\d{1,3}(\.\d{1,3}){3}$/.test(probe.hostname)||probe.hostname.includes(":");
  if(isIp){
    return" The WebSocket address points at a bare IP ("+probe.hostname+"): a TLS certificate cannot cover an IP literal, so every browser refuses it before connecting. Use the host this page is served from (wss://"+location.host+"/ws) or a DNS name with a valid certificate.";
  }
  probe.protocol=probe.protocol==="wss:"?"https:":"http:";
  try{
    const r=await fetch(probe.href);
    if(r.status===404)return" "+probe.href+" answered HTTP 404. This path has no working WebSocket handler: nginx may be sending /ws to the web app, or Asterisk may not have its WebSocket handler loaded. Check the active HTTPS vhost routes /ws to Asterisk and that res_http_websocket and res_pjsip_transport_websocket are loaded. Rebuilding the web app does not install a host nginx route.";
    if(r.status===400||r.status===426)return" "+probe.href+" answered HTTP "+r.status+" to a plain HTTP probe; a WebSocket handler may be reachable. Verify a real HTTP 101 upgrade with the SIP subprotocol and check nginx forwards Upgrade, Connection and Sec-WebSocket-Protocol. This probe alone cannot identify why the socket closed.";
    if(r.status===502||r.status===503||r.status===504)return" "+probe.href+" answered HTTP "+r.status+" - the reverse proxy cannot reach Asterisk's WebSocket listener: check the asterisk container is up and docker-compose publishes 127.0.0.1:8088:8088.";
    if(r.status>=500)return" "+probe.href+" answered HTTP "+r.status+" - the reverse proxy cannot reach Asterisk's WebSocket listener.";
    return" "+probe.href+" answered HTTP "+r.status+" instead of a WebSocket upgrade.";
  }catch{
    return" The WebSocket connection to "+wssUrl+" failed. The HTTP diagnostic probe was also blocked (possibly CORS on a different host); it cannot determine whether DNS, TLS, the proxy or Asterisk caused the disconnect. The default socket for this page is "+defaultWss()+".";
  }
}

/* Registration and connection resilience ---------------------------------- */
function stopKeepalive(){clearInterval(keepaliveTimer);keepaliveTimer=null}
function stopReconnectWatchdog(){clearTimeout(reconnectTimer);reconnectTimer=null}
function sendRegistrationKeepalive(){
  if(!ua)return;
  if(typeof ua.isConnected==="function"&&!ua.isConnected())return;
  try{if(typeof ua.register==="function")ua.register()}catch{}
}
function startKeepalive(){
  stopKeepalive();
  keepaliveTimer=setInterval(sendRegistrationKeepalive,KEEPALIVE_MS)
}
function ensureSipConnection(){
  if(!ua)return;
  try{
    if(typeof ua.isConnected==="function"&&!ua.isConnected()){
      // JsSIP auto-recovers with exponential backoff; this is a bounded
      // watchdog for a browser that suspended timers while the tab slept.
      ua.start()
    }else if(typeof ua.isRegistered==="function"&&!ua.isRegistered()){
      ua.register()
    }
  }catch{}
}
function scheduleReconnect(delay=RECONNECT_WATCHDOG_MS){
  stopReconnectWatchdog();
  reconnectTimer=setTimeout(()=>{reconnectTimer=null;ensureSipConnection()},delay)
}

async function connectSip(s){
  if(!(await ensureSipLibrary())){
    /* "Not loaded" alone cannot tell a stale cached page from a server that is
       running an image without the vendored stack at all, so probe the one URL
       that matters and put the answer in the error. */
    let why="the SIP stack could not be downloaded";
    try{
      const probe=await fetch("/jssip.min.js",{method:"HEAD"});
      why=probe.ok?"/jssip.min.js loaded but did not start":"/jssip.min.js answered HTTP "+probe.status+" - the server is running an image without the phone's SIP stack";
    }catch{}
    throw new Error("SIP library is not loaded: "+why+". Reload the page; if this persists, rebuild the telephony-api image - it must contain web/jssip.min.js.");
  }
  if(!s.username||!s.password||!s.domain||!s.wss)throw new Error("Enter SIP username, password and domain.");
  return new Promise((resolve,reject)=>{
    try{
      stopKeepalive();stopReconnectWatchdog();sipEverRegistered=false;
      if(ua){try{ua.stop()}catch{}ua=null}
      currentSipSettings={...s};
      const socket=new JsSIP.WebSocketInterface(s.wss);
      ua=new JsSIP.UA({
        sockets:[socket],uri:"sip:"+s.username+"@"+s.domain,password:s.password,
        // Session timers keep long WebRTC calls and NAT bindings refreshed.
        session_timers:true,session_timers_refresh_method:"UPDATE",
        register:true,register_expires:300,
        connection_recovery_min_interval:2,connection_recovery_max_interval:8
      });
      let settled=false;
      const fail=message=>{if(settled)return;settled=true;setState("Registration failed");reject(new Error(message||"SIP registration failed"))};
      ua.on("connected",()=>{
        setState(sipEverRegistered?"Reconnecting…":"Online",sipEverRegistered?"connecting":true);
        if(sipEverRegistered)scheduleReconnect(12000)
      });
      ua.on("disconnected",()=>{
        stopKeepalive();setState("Reconnecting…","connecting");
        if(sipEverRegistered)scheduleReconnect();
        if(settled)return;
        /* A refused proxy path, an unreachable listener and a dead host all
           arrive as this one event before registration - probe the endpoint
           and report the evidence without assuming which hop failed. */
        wssDiagnosis(s.wss).then(hint=>fail("SIP WebSocket disconnected."+hint))
      });
      ua.on("registered",()=>{
        sipEverRegistered=true;stopReconnectWatchdog();startKeepalive();
        setState("Ready",true);showPhone();
        $("login-status").className="login-status ok";$("login-status").textContent="Authenticated and registered.";
        if(!settled){settled=true;resolve(true)}
      });
      ua.on("unregistered",()=>{if(sipEverRegistered&&settled)setState("Reconnecting…","connecting")});
      ua.on("registrationFailed",e=>{
        if(settled){setState("Registration failed");setCallStatus("SIP registration failed. Check credentials or tap Reconnect.");return}
        fail(e.cause||"SIP authentication failed.")
      });
      ua.on("newRTCSession",e=>{
        const session=e.session;
        if(session.direction==="incoming"){
          if(currentSession){try{session.terminate()}catch{};return}
          const caller=session.remote_identity?.uri?.user||"Unknown";
          input.value=caller;showNumber();attachSession(session,true)
        }
      });
      ua.start()
    }catch(err){stopKeepalive();stopReconnectWatchdog();ua=null;reject(err)}
  })
}

async function startCall(){
  const number=cleanNumber(input.value);if(!number){input.focus();return}
  if(!ua){showLogin("Sign in to your SIP account first.");return}
  const disconnected=(typeof ua.isConnected==="function"&&!ua.isConnected())||
                     (typeof ua.isRegistered==="function"&&!ua.isRegistered());
  if(disconnected){ensureSipConnection();$("dial-hint").textContent="Reconnecting to SIP — try the call again in a moment.";return}
  if(currentSession)return;
  showNumber();playRemoteAudio();  // Unlock the media element inside the user's click.
  try{
    const session=ua.call(sipTarget(number),{mediaConstraints:MIC_CONSTRAINTS,pcConfig:iceConfig(),rtcOfferConstraints:{offerToReceiveAudio:true,offerToReceiveVideo:false}});
    attachSession(session,false)
  }catch(err){showDialError("Could not start call: "+(err?.message||err))}
}
$("call-btn").onclick=startCall;

$("answer-btn").onclick=()=>{
  const session=currentSession;if(!session)return;
  playRemoteAudio();  // The answer gesture is the best chance to unlock audio.
  setCallStatus("Preparing answer… Allow microphone access if prompted.");setCallPhase("answering");
  $("incoming-actions").classList.add("hidden");$("hangup-btn").classList.remove("hidden");$("active-label").textContent="Answering";
  startPreparationTimer(session);
  try{
    session.answer({mediaConstraints:MIC_CONSTRAINTS,pcConfig:iceConfig(),rtcAnswerConstraints:{offerToReceiveAudio:true,offerToReceiveVideo:false}});
    // Some JsSIP builds create the peer connection during answer(); bind it
    // even if the peerconnection event was emitted before this returned.
    if(currentSession===session)attachRemoteAudio(session)
  }catch(e){if(currentSession===session)endCall("Could not answer: "+(e?.message||e),true,"failed")}
};
$("decline-btn").onclick=()=>endCall("Call declined",true,"declined");
$("hangup-btn").onclick=()=>endCall("Call ended",true,"completed");
$("mute-btn").onclick=()=>{if(!currentSession)return;muted=!muted;currentSession.mute({audio:muted});$("mute-btn").classList.toggle("active",muted)};
$("hold-btn").onclick=()=>{if(!currentSession)return;held=!held;held?currentSession.hold():currentSession.unhold();$("hold-btn").classList.toggle("active",held)};
/* In-call DTMF: a real softphone must be able to navigate an IVR without
   leaving the call screen. The same keys work from the physical keyboard. */
function sendDtmf(tone){
  if(!currentSession||!/^[0-9*#]$/.test(String(tone)))return;
  try{if(typeof currentSession.sendDTMF==="function")currentSession.sendDTMF(String(tone))}catch{}
  const key=doc.querySelector(`#dtmf-panel [data-dtmf="${tone}"]`);
  if(key){key.classList.add("pressed");setTimeout(()=>key.classList.remove("pressed"),120)}
}
$("dtmf-btn").onclick=()=>{
  const panel=$("dtmf-panel"),open=panel.classList.toggle("hidden")===false;
  $("dtmf-btn").classList.toggle("active",open);
  $("dtmf-btn").setAttribute("aria-expanded",open?"true":"false")
};
doc.querySelectorAll("#dtmf-panel [data-dtmf]").forEach(key=>key.onclick=()=>sendDtmf(key.dataset.dtmf));
$("enable-audio-btn").onclick=()=>playRemoteAudio();
$("speaker-btn").onclick=async()=>{
  const on=!$("speaker-btn").classList.contains("active");
  /* Route the remote audio to another output where the browser can pick one.
     "speaker" is not a device id - setSinkId only accepts an id the browser
     itself reported - and a failed switch must never fall back to muting the
     far end. Where the browser cannot route outputs at all (iOS Safari), the
     button says so and leaves the call playing exactly where it was. */
  const say=message=>{const el=currentSession?$("active-status"):$("dial-hint");if(el)el.textContent=message;$("speaker-btn").title=message};
  if(typeof remoteAudio.setSinkId!=="function"){
    say("This browser cannot switch audio output. The call keeps playing on the default output.");
    return;
  }
  try{
    let target="";
    if(on){
      const devices=navigator.mediaDevices&&navigator.mediaDevices.enumerateDevices
        ? await navigator.mediaDevices.enumerateDevices() : [];
      const outputs=devices.filter(d=>d.kind==="audiooutput"&&d.deviceId);
      const chosen=outputs.find(d=>d.deviceId!=="default")||outputs[0];
      if(!chosen){
        say("No second audio output was found. The call keeps playing on the default output.");
        return;
      }
      target=chosen.deviceId;
    }
    await remoteAudio.setSinkId(target);   // "" returns to the default output
    $("speaker-btn").classList.toggle("active",on);
    $("speaker-btn").title=on?"Remote audio is on the loudspeaker":"Remote audio is on the default output";
  }catch(err){
    $("speaker-btn").classList.toggle("active",false);
    say("Could not switch audio output: "+(err&&err.message||err)+". The call keeps playing on the current output.");
  }
};

$("login-form").onsubmit=async e=>{
  e.preventDefault();
  const domain=$("login-domain").value.trim().replace(/^https?:\/\//,"").replace(/\/$/,"");
  const s={...settings(),username:$("login-username").value.trim(),password:$("login-password").value,domain,wss:$("login-wss").value.trim()||defaultWss()};
  if(!s.username||!s.password||!s.domain)return;
  saveSettings(s);setLoginBusy(true);$("login-status").className="login-status";$("login-status").textContent="Connecting to SIP server…";
  try{await connectSip(s)}catch(err){$("login-status").className="login-status error";$("login-status").textContent=err.message||"Authentication failed.";setState("Offline");stopKeepalive();stopReconnectWatchdog();sipEverRegistered=false;try{if(ua)ua.stop()}catch{}ua=null}
  finally{setLoginBusy(false)}
};

$("settings-btn").onclick=()=>{
  const s=settings();
  $("sip-extension").value=s.extension||"";
  $("sip-username").value=s.username||"";
  $("sip-domain").value=s.domain||"";
  $("sip-wss").value=s.wss||defaultWss();
  $("api-token").value=s.apiToken||"";
  settingsDialog.showModal()
};
$("connect-btn").onclick=async()=>{
  const old=settings(),domain=$("sip-domain").value.trim().replace(/^https?:\/\//,"").replace(/\/$/,"");
  const s={...old,extension:$("sip-extension").value.trim(),username:$("sip-username").value.trim(),domain,
           wss:$("sip-wss").value.trim()||defaultWss(),apiToken:$("api-token").value.trim()};
  saveSettings(s);$("settings-status").className="settings-status";$("settings-status").textContent="Reconnecting…";
  try{await connectSip(s);$("settings-status").className="settings-status ok";$("settings-status").textContent="Phone connected."}catch(err){$("settings-status").className="settings-status error";$("settings-status").textContent=err.message}
};
$("logout-btn").onclick=()=>{
  stopKeepalive();stopReconnectWatchdog();sipEverRegistered=false;
  try{if(ua)ua.stop()}catch{}ua=null;handoff=null;iceServers=DEFAULT_ICE_SERVERS;
  currentSession=null;clearPreparationTimer();if(currentIceCleanup)currentIceCleanup();currentIceCleanup=null;resetRemoteAudio();clearInterval(timer);stopQualityWatch();
  sessionStorage.removeItem(secretKey);localStorage.removeItem(storeKey);settingsDialog.close();setState("Offline");showLogin("You have been logged out.");$("login-password").focus()
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
  rows.forEach(r=>{const el=doc.createElement("div");el.className="list-item";el.innerHTML='<div class="avatar">'+escapeHtml((r.name||"?")[0].toUpperCase())+'</div><div class="item-main"><strong>'+escapeHtml(r.name)+'</strong><small>'+escapeHtml(r.phone)+'</small></div><button class="quick-call">☎</button>';el.querySelector("button").onclick=()=>{input.value=r.phone;showNumber();nav("dialer-view");startCall()};box.appendChild(el)})
}
window.addEventListener("beforeinstallprompt",e=>{e.preventDefault();deferredInstall=e;installBtn.classList.remove("hidden")});
installBtn.onclick=async()=>{if(!deferredInstall)return;deferredInstall.prompt();await deferredInstall.userChoice;deferredInstall=null;installBtn.classList.add("hidden")};
window.addEventListener("appinstalled",()=>installBtn.classList.add("hidden"));
/* Mobile and laptop browsers throttle timers in background tabs. Recover the
   signalling connection as soon as the network or the tab becomes active,
   instead of waiting for a call that Asterisk can no longer deliver. */
window.addEventListener("online",()=>{if(ua)ensureSipConnection()});
window.addEventListener("offline",()=>{stopKeepalive();setState("Offline")});
doc.addEventListener("visibilitychange",()=>{if(!doc.hidden&&ua)ensureSipConnection()});
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
  // The console may hand over the platform's ICE servers (its own STUN/TURN);
  // without them the built-in STUN list applies.
  iceServers=Array.isArray(m.iceServers)&&m.iceServers.length?m.iceServers:DEFAULT_ICE_SERVERS;
  handoff={extension:m.extension||"",username:m.sip_username,password:m.sip_password,domain:m.domain,wss:m.wss||defaultWss(),apiToken:""};
  try{if(ua)ua.stop()}catch{}ua=null;
  showConsoleScreen(label,`Signing in as ${label}…`);
  try{await connectSip(handoff);doc.title=`Softphone · ${label}`;$("brand-title").textContent=label}
  catch(err){showLogin(err.message||"Could not sign this extension in.",true)}
}
