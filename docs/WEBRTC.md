# Browser WebRTC

Asterisk WebRTC support uses PJSIP with a WSS transport. Modern browsers normally require a trusted HTTPS context for microphone/WebRTC use.

## Production topology

```text
EngineerIP CRM page
      |
      | authenticated short-lived browser capability
      v
Reverse proxy / TLS
      |
      | WSS
      v
Asterisk 8089
      |
      | SIP/RTP
      v
SIP trunk / phone endpoint
```

The ARI management API stays on the Docker network and is not exposed to the browser.

## Provisioning recommendation

Store WebRTC usernames/passwords against the CRM member/extension on the server side. Do not return the ARI master password or provider password to JavaScript.

A production browser session should obtain an expiring token or a narrowly scoped SIP credential from EngineerIP after normal CRM authentication. Revoke/rotate credentials when an employee is disabled.

## ICE / NAT

For remote clients behind NAT, configure STUN/TURN appropriate to your deployment. The example endpoint leaves `ice_servers` empty because the correct ICE infrastructure is environment-specific.

## Browser limitations

Microphone permission, HTTPS, firewall rules, NAT behavior, and SIP provider compatibility cannot be validated from a Git-only environment. These must be tested on the actual deployed hostname/network with a real SIP account.
