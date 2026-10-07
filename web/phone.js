const $=id=>document.getElementById(id);
const input=$("number-input"),display=$("dial-display"),hint=$("dial-hint"),state=$("connection-state");
const installBtn=$("install-btn"),remoteAudio=$("remote-audio"),settingsDialog=$("settings-dialog");
let deferredInstall=null,ua=null,currentSession=null,callStartedAt=null,timer=null,muted=false,held=false;

const storeKey="eip-phone-settings";
const recentsKey="eip-phone-recents";
const contactsKey="eip-phone-contacts";
const settings=()=>JSON.parse(localStorage.getItem(storeKey)||"{}");
const saveSettings=s=>localStorage.setItem(storeKey,JSON.stringify(s));
const cleanNumber=v=>String(v||"").replace(/[^0-9+*#]/g,"");
const sipDomain=()=>settings().domain||location.hostname;
const sipTarget=n=>{const d=sipDomain();return /^sip:/i.test(n)?n:"sip:"+cleanNumber(n)+"@"+d};

function setState(text,online=false){state.className="state "+(online?"online":"offline");state.innerHTML="<i></i> "+text}
function showNumber(){const v=input.value.trim();display.textContent=v||"Enter number";hint.textContent=v?"Ready to call":"Direct paste is supported"}
function addRecent(number,direction="outgoing",status="completed"){const rows=JSON.parse(localStorage.getItem(recentsKey)||"[]");rows.unshift({number,direction,status,at:Date.now()});localStorage.setItem(recentsKey,JSON.stringify(rows.slice(0,100)))}
function formatTime(ts){const d=new Date(ts);return d.toLocaleTimeString([], {hour:"numeric",minute:"2-digit"})}
function renderRecents(rows){const box=$("recents-list");box.innerHTML="";if(!rows.length){box.innerHTML='<div class="empty-state compact"><div class="empty-icon">◷</div><h3>No recent calls</h3><p>Your calls will appear here.</p></div>';return}rows.forEach(r=>{const el=document.createElement("div");el.className="list-item";el.innerHTML='<div class="avatar">'+(r.direction==="incoming"?"↓":"↑")+'</div><div class="item-main"><strong>'+escapeHtml(r.number)+'</strong><small>'+escapeHtml(r.status||"completed")+'</small></div><div class="item-side"><small>'+formatTime(r.at||Date.now())+'</small><button class="quick-call" title="Call">☎</button></div>';el.querySelector(".quick-call").onclick=()=>{input.value=r.number;showNumber();startCall()};box.appendChild(el)})}
function escapeHtml(v){return String(v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]))}
async function loadRecents(){let rows=JSON.parse(localStorage.getItem(recentsKey)||"[]");const s=settings();if(s.apiToken){try{const r=await fetch("/api/v1/calls?limit=50",{headers:{Authorization:"Bearer "+s.apiToken}});if(r.ok){const d=await r.json();rows=d.calls.map(c=>({number:c.customer_number||c.destination||c.phone||c.call_id,status:c.status,direction:c.direction||"outgoing",at:c.started_at?Date.parse(c.started_at):Date.now()}));}}catch{}}renderRecents(rows)}
function nav(viewId){document.querySelectorAll(".view").forEach(v=>v.classList.remove("active"));$(viewId).classList.add("active");document.querySelectorAll(".nav-item").forEach(n=>n.classList.toggle("active",n.dataset.view===viewId));if(viewId==="recents-view")loadRecents()}
document.querySelectorAll(".nav-item").forEach(b=>b.onclick=()=>nav(b.dataset.view));

document.querySelectorAll(".keypad button").forEach(b=>b.onclick=()=>{input.value+=b.dataset.key;showNumber()});
$("clear-btn").onclick=()=>{input.value="";showNumber();input.focus()};
$("backspace-btn").onclick=()=>{input.value=input.value.slice(0,-1);showNumber()};
$("paste-btn").onclick=async()=>{try{input.value=cleanNumber(await navigator.clipboard.readText());showNumber();input.focus()}catch{input.focus();document.execCommand("paste")}};
input.addEventListener("input",showNumber);
input.addEventListener("paste",()=>setTimeout(()=>{input.value=cleanNumber(input.value);showNumber()},0));

function attachSession(session){
 currentSession=session; $("active-number").textContent=input.value||"Unknown"; $("active-call-view").classList.remove("hidden");callStartedAt=Date.now();startTimer();$("active-status").textContent="Connecting…";
 session.on("progress",()=>{$("active-status").textContent="Ringing…"});
 session.on("accepted",()=>{$("active-status").textContent="Connected";callStartedAt=callStartedAt||Date.now()});
 session.on("confirmed",()=>{$("active-status").textContent="Connected"});
 session.on("ended",()=>endCall("Call ended"));
 session.on("failed",()=>endCall("Call failed"));
 session.on("muted",()=>{});
 session.on("peerconnection",e=>{const pc=e.peerconnection||e;pc.ontrack=ev=>{if(ev.streams&&ev.streams[0]){remoteAudio.srcObject=ev.streams[0];remoteAudio.play().catch(()=>{})}}});
}
function startTimer(){clearInterval(timer);timer=setInterval(()=>{const sec=Math.max(0,Math.floor((Date.now()-(callStartedAt||Date.now()))/1000));$("call-timer").textContent=String(Math.floor(sec/60)).padStart(2,"0")+":"+String(sec%60).padStart(2,"0")},500)}
function endCall(message){if(currentSession&&currentSession.isEstablished?.()){};if(input.value)addRecent(input.value,"outgoing",message.toLowerCase().includes("failed")?"failed":"completed");if(currentSession){try{currentSession.terminate()}catch{}}currentSession=null;clearInterval(timer);$("active-status").textContent=message;$("active-call-view").classList.add("hidden");$("call-timer").textContent="00:00";$("call-btn").disabled=false;loadRecents()}

async function connectSip(){
 const s=settings();
 if(!window.JsSIP){$("settings-status").textContent="SIP library is not loaded.";return false}
 if(!s.username||!s.password||!s.wss){$("settings-status").textContent="Enter SIP username, password and WSS server.";return false}
 try{
   const socket=new JsSIP.WebSocketInterface(s.wss);
   ua=new JsSIP.UA({sockets:[socket],uri:"sip:"+s.username+"@"+(s.domain||location.hostname),password:s.password,session_timers:false});
   ua.on("connected",()=>{setState("Online",true);$("settings-status").className="settings-status ok";$("settings-status").textContent="Phone connected."});
   ua.on("disconnected",()=>setState("Offline"));
   ua.on("registered",()=>setState("Ready",true));
   ua.on("registrationFailed",e=>{setState("Registration failed");$("settings-status").className="settings-status error";$("settings-status").textContent=e.cause||"SIP registration failed"});
   ua.on("newRTCSession",e=>{const session=e.session;if(session.direction==="incoming"){currentSession=session;input.value=session.remote_identity?.uri?.user||"";showNumber();$("active-number").textContent=input.value||"Incoming call";$("active-call-view").classList.remove("hidden");$("active-status").textContent="Incoming call";session.on("peerconnection",ev=>{const pc=ev.peerconnection||ev;pc.ontrack=x=>{if(x.streams?.[0]){remoteAudio.srcObject=x.streams[0];remoteAudio.play().catch(()=>{})}}});}});
   ua.start();return true
 }catch(err){$("settings-status").className="settings-status error";$("settings-status").textContent=err.message;return false}
}

async function startCall(){
 const number=cleanNumber(input.value);if(!number){input.focus();return}
 if(!ua){settingsDialog.showModal();$("settings-status").textContent="Connect your SIP phone first.";return}
 if(currentSession){return}
 try{
   const session=ua.call(sipTarget(number),{mediaConstraints:{audio:true,video:false},pcConfig:{iceServers:[]},rtcOfferConstraints:{offerToReceiveAudio:true,offerToReceiveVideo:false}});
   attachSession(session);$("call-btn").disabled=true;
 }catch(err){$("dial-hint").textContent=err.message}
}
$("call-btn").onclick=startCall;
$("hangup-btn").onclick=()=>endCall("Call ended");
$("mute-btn").onclick=()=>{if(!currentSession)return;muted=!muted;currentSession.mute({audio:muted});$("mute-btn").classList.toggle("active",muted)};
$("hold-btn").onclick=()=>{if(!currentSession)return;held=!held;held?currentSession.hold():currentSession.unhold();$("hold-btn").classList.toggle("active",held)};
$("speaker-btn").onclick=()=>{remoteAudio.muted=!remoteAudio.muted;$("speaker-btn").classList.toggle("active",remoteAudio.muted)};
$("settings-btn").onclick=()=>{const s=settings();$("sip-extension").value=s.extension||"";$("sip-username").value=s.username||"";$("sip-password").value=s.password||"";$("sip-domain").value=s.domain||sipDomain();$("sip-wss").value=s.wss||("wss://"+(s.domain||location.hostname)+"/ws");$("api-token").value=s.apiToken||"";settingsDialog.showModal()};
$("connect-btn").onclick=async()=>{const s={extension:$("sip-extension").value.trim(),username:$("sip-username").value.trim(),password:$("sip-password").value,domain:$("sip-domain").value.trim(),wss:$("sip-wss").value.trim(),apiToken:$("api-token").value.trim()};saveSettings(s);await connectSip()};
$("refresh-recents").onclick=loadRecents;
$("contacts-btn").onclick=()=>nav("contacts-view");
$("new-message").onclick=()=>alert("Messaging provider integration can be enabled here.");
$("add-contact").onclick=()=>{const name=prompt("Contact name");const phone=prompt("Phone number");if(!name||!phone)return;const rows=JSON.parse(localStorage.getItem(contactsKey)||"[]");rows.push({name,phone});localStorage.setItem(contactsKey,JSON.stringify(rows));renderContacts()};
function renderContacts(){const rows=JSON.parse(localStorage.getItem(contactsKey)||"[]"),box=$("contacts-list");box.innerHTML="";$("contacts-empty").classList.toggle("hidden",!!rows.length);rows.forEach(r=>{const el=document.createElement("div");el.className="list-item";el.innerHTML='<div class="avatar">'+escapeHtml(r.name[0].toUpperCase())+'</div><div class="item-main"><strong>'+escapeHtml(r.name)+'</strong><small>'+escapeHtml(r.phone)+'</small></div><button class="quick-call">☎</button>';el.querySelector("button").onclick=()=>{input.value=r.phone;showNumber();nav("dialer-view");startCall()};box.appendChild(el)})}
window.addEventListener("beforeinstallprompt",e=>{e.preventDefault();deferredInstall=e;installBtn.classList.remove("hidden")});
installBtn.onclick=async()=>{if(!deferredInstall)return;deferredInstall.prompt();await deferredInstall.userChoice;deferredInstall=null;installBtn.classList.add("hidden")};
window.addEventListener("appinstalled",()=>installBtn.classList.add("hidden"));
if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js").catch(()=>{});
$("sip-extension").value=settings().extension||"";renderContacts();loadRecents();showNumber();setState("Offline");
