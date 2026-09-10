const status = document.getElementById("status");
const ext = document.getElementById("extension");
const number = document.getElementById("number");
const registerButton = document.getElementById("register");
const callButton = document.getElementById("call");
const hangupButton = document.getElementById("hangup");

let registered = false;
let activeCall = null;

function setStatus(message) {
  status.textContent = message;
}

registerButton.addEventListener("click", async () => {
  const extension = ext.value.trim();
  if (!extension) {
    setStatus("Enter an extension first.");
    return;
  }
  setStatus(`Extension ${extension} selected. Browser SIP credentials must be provisioned by the CRM.`);
  registered = true;
  callButton.disabled = false;
});

callButton.addEventListener("click", async () => {
  if (!registered) return;
  const phone = number.value.trim();
  if (!phone) {
    setStatus("Enter an E.164 number, for example +16235551234.");
    return;
  }
  setStatus(`Requesting call to ${phone}...`);
  try {
    const response = await fetch("/api/v1/browser/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone, extension: ext.value.trim() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Call request failed");
    activeCall = data.call.call_id;
    setStatus(`Call started: ${activeCall}`);
    hangupButton.disabled = false;
    callButton.disabled = true;
  } catch (error) {
    setStatus(error.message);
  }
});

hangupButton.addEventListener("click", async () => {
  if (!activeCall) return;
  try {
    const response = await fetch(`/api/v1/calls/${encodeURIComponent(activeCall)}/hangup`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Hangup failed");
    setStatus("Call ended.");
  } catch (error) {
    setStatus(error.message);
  } finally {
    activeCall = null;
    hangupButton.disabled = true;
    callButton.disabled = false;
  }
});
