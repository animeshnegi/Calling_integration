const $=id=>document.getElementById(id);
const input=$("number-input"),display=$("dial-display"),hint=$("dial-hint"),state=$("connection-state");
const installBtn=$("install-btn"),remoteAudio=$("remote-audio"),settingsDialog=$("settings-dialog"),loginScreen=$("login-screen"),phoneApp=$("phone-app");
let deferredInstall=null,ua=null,currentSession=null,callStartedAt=null,timer=null,muted=false,held=false;

const storeKey="eip-phone-settings";
const secretKey="eip-phone-sip-password";
const recentsKey="eip-phone-recents";
const contactsKey="eip-phone-contacts";

function settings(){
  const publicSettings=JSON.parse(localStorage.getItem(storeKey)||"{}");
  return {...publicSettings,password:sessionStorage.getItem(secretKey)||""};
}
function saveSettings(s){
  const publicSettings={extension:s.extension||"",username:s.username||"",domain:s.domain||"",wss:s.wss||"",apiToken:s.apiToken||""};
  localStorage.setItem(storeKey,JSON.stringify(publicSettings));
  if(s.password)sessionStorage.setItem(secretKey,s.password);
}
const cleanNumber=v=>String(v||"").replace(/[^0-9+*#]/g,"");
const sipDomain=()=>settings().domain||location.hostname;
const defaultWss=domain=>"wss://"+String(domain||location.hostname).replace(/^https?:\/\//,"").replace(/\/$/,"")+"/ws";
const sipTarget=n=>{const d=sipDomain();return /^sip:/i.test(n)?n:"sip:"+cleanNumber(n)+"@"+d};

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
input.addEventListener("paste",()=>setTimeout(()=>{input.value=cleanNumber(input.value);showNumber()},0));

function showPhone(){loginScreen.classList.add("hidden");phoneApp.classList.remove("hidden")}
function showLogin(message="",error=false){
  phoneApp.classList.add("hidden");loginScreen.classList.remove("hidden");
  $("login-status").className="login-status"+(error?" error":"");$("login-status").textContent=message;
  $("login-password").value="";
  $("login-username").value=settings().username||"";
  $("login-domain").value=settings().domain||"";
  $("login-wss").value=settings().wss||"";
}
function setLoginBusy(busy){$("login-btn").disabled=busy;$("login-btn").textContent=busy?"Authenticating…":"Connect & Sign In"}

function attachRemoteAudio(session){
  session.on("peerconnection",e=>{
    const pc=e.peerconnection||e;
    pc.ontrack=ev=>{if(ev.streams&&ev.streams[0]){remoteAudio.srcObject=ev.streams[0];remoteAudio.play().catch(()=>{})}}
  })
}
function attachSession(session,incoming=false){
  currentSession=session;
  $("active-number").textContent=input.value||"Unknown";
  $("active-call-view").classList.remove("hidden");
  $("incoming-actions").classList.toggle("hidden",!incoming);
  $("hangup-btn").classList.toggle("hidden",incoming);
  $("active-label").textContent=incoming?"Incoming call":"Calling";
  callStartedAt=incoming?null:Date.now();startTimer();
  $("active-status").textContent=incoming?"Incoming call":"Connecting…";
  session.on("progress",()=>{$("active-status").textContent="Ringing…"});
  session.on("accepted",()=>{$("active-status").textContent="Connected";callStartedAt=callStartedAt||Date.now();startTimer()});
  session.on("confirmed",()=>{$("active-status").textContent="Connected";callStartedAt=callStartedAt||Date.now();startTimer()});
  session.on("ended",()=>endCall("Call ended",false));
  session.on("failed",e=>endCall("Call failed"+(e?.cause?": "+e.cause:""),false));
  attachRemoteAudio(session)
}
function startTimer(){
  clearInterval(timer);timer=setInterval(()=>{
    if(!callStartedAt)return;
    const sec=Math.max(0,Math.floor((Date.now()-callStartedAt)/1000));
    $("call-timer").textContent=String(Math.floor(sec/60)).padStart(2,"0")+":"+String(sec%60).padStart(2,"0")
  },500)
}
function endCall(message="Call ended",terminate=true){
  const number=input.value;
  if(currentSession&&terminate){try{if(!currentSession.isEnded?.())currentSession.terminate()}catch{}}
  if(number)addRecent(number,currentSession?.direction==="incoming"?"incoming":"outgoing",message.toLowerCase().includes("failed")?"failed":"completed");
  currentSession=null;clearInterval(timer);callStartedAt=null;
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
    const session=ua.call(sipTarget(number),{mediaConstraints:{audio:true,video:false},pcConfig:{iceServers:[]},rtcOfferConstraints:{offerToReceiveAudio:true,offerToReceiveVideo:false}});
    attachSession(session,false);$("call-btn").disabled=true
  }catch(err){$("dial-hint").textContent=err.message}
}
$("call-btn").onclick=startCall;

$("answer-btn").onclick=()=>{
  if(!currentSession)return;
  try{currentSession.answer({mediaConstraints:{audio:true,video:false},pcConfig:{iceServers:[]}});$("incoming-actions").classList.add("hidden");$("hangup-btn").classList.remove("hidden");$("active-label").textContent="Call";callStartedAt=Date.now();startTimer()}catch(e){$("active-status").textContent=e.message}
};
$("decline-btn").onclick=()=>endCall("Call declined");
$("hangup-btn").onclick=()=>endCall("Call ended");
$("mute-btn").onclick=()=>{if(!currentSession)return;muted=!muted;currentSession.mute({audio:muted});$("mute-btn").classList.toggle("active",muted)};
$("hold-btn").onclick=()=>{if(!currentSession)return;held=!held;held?currentSession.hold():currentSession.unhold();$("hold-btn").classList.toggle("active",held)};
$("speaker-btn").onclick=()=>{remoteAudio.muted=!remoteAudio.muted;$("speaker-btn").classList.toggle("active",remoteAudio.muted)};

$("login-form").onsubmit=async e=>{
  e.preventDefault();
  const domain=$("login-domain").value.trim().replace(/^https?:\/\//,"").replace(/\/$/,"");
  const s={username:$("login-username").value.trim(),password:$("login-password").value,domain,wss:$("login-wss").value.trim()||defaultWss(domain),extension:"",apiToken:""};
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
  const s={...old,username:$("sip-username").value.trim(),domain,wss:$("sip-wss").value.trim()||defaultWss(domain)};
  saveSettings(s);$("settings-status").className="settings-status";$("settings-status").textContent="Reconnecting…";
  try{await connectSip(s);$("settings-status").className="settings-status ok";$("settings-status").textContent="Phone connected."}catch(err){$("settings-status").className="settings-status error";$("settings-status").textContent=err.message}
};
$("logout-btn").onclick=()=>{
  try{if(ua)ua.stop()}catch{}ua=null;currentSession=null;sessionStorage.removeItem(secretKey);localStorage.removeItem(storeKey);settingsDialog.close();setState("Offline");showLogin("You have been logged out.");$("login-password").focus()
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

const saved=settings();
$("login-username").value=saved.username||"";
$("login-domain").value=saved.domain||"";
$("login-wss").value=saved.wss||"";
renderContacts();loadRecents();showNumber();setState("Offline");
if(saved.username&&saved.domain&&saved.password){
  $("login-status").textContent="Previous SIP session found. Reconnecting…";
  connectSip(saved).catch(()=>showLogin("Please sign in again.",true));
}
