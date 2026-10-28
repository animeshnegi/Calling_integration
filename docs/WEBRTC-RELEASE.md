# WebRTC call setup / ICE mapping release

This update changes **both** the API image (phone and configuration sync) and
the Asterisk image (ICE candidate include at container start). The existing
Compose file must already mount the shared dynamic configuration volume in both
images and publish the configured RTP UDP range unchanged, as in the supplied
Compose file. No new VM source tree, host-side script, proxy modification, or
server image build is required. Do not recreate Asterisk in the middle of calls.

## Windows PowerShell: build and export

Use an updated workstation checkout and Docker Desktop in Linux-container mode.
The two `docker build` commands do not need the VM's `.env` or secrets.

```powershell
$Tag = "webrtc-20261028"
$ImageTar = "engineerip-telephony-$Tag.tar"
docker build --platform linux/amd64 -t "engineerip/telephony-api:$Tag" .
docker build --platform linux/amd64 -t "engineerip/asterisk:$Tag" ./asterisk
docker image save --output $ImageTar "engineerip/telephony-api:$Tag" "engineerip/asterisk:$Tag"
$ImageHash = (Get-FileHash $ImageTar -Algorithm SHA256).Hash.ToLower()
"$ImageHash  $ImageTar" | Set-Content -Encoding ascii "${ImageTar}.sha256"
scp $ImageTar "${ImageTar}.sha256" engineerip7@ipseekers:/tmp/
```

Or copy both files to `/tmp` with WinSCP. Use an appropriate unique tag if this
example tag already exists.

## VM: import and start without building

**Schedule a maintenance window:** recreating Asterisk disconnects active calls.
Keep the existing `.env`, `SECRET_KEY`, database and voicemail volumes. No
`docker compose down -v`, source update, or host script is needed.

```bash
TAG=webrtc-20261028
cd /tmp
sha256sum -c "engineerip-telephony-$TAG.tar.sha256"
docker image load --input "engineerip-telephony-$TAG.tar"
cd /opt/Calling_integration
IMAGE_TAG="$TAG" docker compose up -d --no-build --pull never --force-recreate asterisk telephony-ari telephony-api
docker compose ps asterisk telephony-ari telephony-api
```

The `IMAGE_TAG=...` assignment above applies only to that command. Ensure the
VM's existing `.env` `IMAGE_TAG` is set to the same new tag for future Compose
commands, without changing or displaying the rest of `.env`. Back up `.env`
before editing, keep it private, and preserve every other value.

The browser should reload the phone page after the upgrade (the shell version is
11). Check the generated numeric IPv4 mapping and test both directions from the
real devices, including audio both ways:

```bash
docker exec engineerip-asterisk sh -c 'cat /etc/asterisk/dynamic/rtp.ice.conf'
docker exec engineerip-asterisk asterisk -rx 'rtp show settings'
docker compose logs --tail=80 telephony-ari asterisk
```

The mapping should contain the container's actual IPv4 address on the left and
the server's public media IPv4 on the right. This is not proof of ICE/RTP
reachability. The Service address must resolve to that public IP (not a CDN),
the RTP UDP range must reach Asterisk, and restrictive networks can require
TURN. See [`WEBRTC.md`](WEBRTC.md) for limits and verification. A local
headless-browser test passed both ways, but it does not test the user's VM,
carrier, firewall or live phones.
