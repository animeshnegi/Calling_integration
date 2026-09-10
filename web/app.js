import { UserAgent, Registerer, Inviter, SessionState } from "https://cdn.jsdelivr.net/npm/sip.js@0.22.0/lib/esm/index.js";

const $ = (id) => document.getElementById(id);
const status = $("status");
const registerButton = $("register");
const callButton = $("call");
const hangupButton = $("hangup");
let userAgent = null;
let registerer = null;
let session = null;

function setStatus(text) {
  status.textContent = text;
}

function socketUri() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}

async function register() {
  const extension = $("extension").value.trim();
  if (!extension) return;

  // WebRTC credentials should be provisioned per user by CRM in production.
  const uri = UserAgent.makeURI(`sip:${extension}-web@${location.hostname}`);
  userAgent = new UserAgent({
    uri,
    transportOptions: { server: socketUri() },
    authorizationUsername: `${extension}-web`,
    authorizationPassword: window.prompt("WebRTC SIP password for this extension:" ) || "",
    delegate: {
      onInvite(invitation) {
        session = invitation;
        invitation.stateChange.addListener(() => setStatus(`Incoming call: ${invitation.state}`));
        invitation.accept({ sessionDescriptionHandlerOptions: { constraints: { audio: true, video: false } } });
        bindSession(invitation);
      },
    },
    sessionDescriptionHandlerFactoryOptions: {
      peerConnectionConfiguration: { iceServers: [] },
    },
  });

  userAgent.delegate = userAgent.delegate || {};
  await userAgent.start();
  registerer = new Registerer(userAgent);
  await registerer.register();
  setStatus(`Registered ${extension}`);
  callButton.disabled = false;
  hangupButton.disabled = true;
}

function bindSession(current) {
  current.stateChange.addListener(() => {
    const state = current.state;
    setStatus(`Call: ${state}`);
    if (state === SessionState.Established) {
      callButton.disabled = true;
      hangupButton.disabled = false;
    }
    if (state === SessionState.Terminated) {
      callButton.disabled = false;
      hangupButton.disabled = true;
      session = null;
    }
  });

  const pc = current.sessionDescriptionHandler?.peerConnection;
  if (pc) {
    pc.ontrack = (event) => {
      const stream = event.streams?.[0];
      if (stream) $("remoteAudio").srcObject = stream;
    };
  }
}

async function makeCall() {
  if (!userAgent) return;
  const number = $("number").value.trim();
  if (!number) return;

  const target = UserAgent.makeURI(`sip:${number}@${location.hostname}`);
  session = new Inviter(userAgent, target, {
    sessionDescriptionHandlerOptions: { constraints: { audio: true, video: false } },
  });
  bindSession(session);
  await session.invite();
  callButton.disabled = true;
  hangupButton.disabled = false;
}

async function hangup() {
  if (!session) return;
  try {
    if (session.state === SessionState.Established) {
      await session.bye();
    } else if (session.cancel) {
      await session.cancel();
    }
  } catch (error) {
    console.error(error);
  }
}

registerButton.addEventListener("click", () => register().catch((e) => setStatus(`Register failed: ${e.message}`)));
callButton.addEventListener("click", () => makeCall().catch((e) => setStatus(`Call failed: ${e.message}`)));
hangupButton.addEventListener("click", () => hangup());
