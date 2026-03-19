---
title: "Running with Immich Mobile"
last-updated: 2026-03-19
---

# Running with Immich Mobile

The Immich mobile app requires HTTPS to complete the OAuth login flow. For local development, use [mkcert](https://github.com/FiloSottile/mkcert) to generate a locally-trusted certificate.

## 1. Install mkcert and set up the local CA

```bash
# macOS
brew install mkcert

# Linux — see https://github.com/FiloSottile/mkcert#installation
```

Install the local CA into your system trust store (one-time setup):

```bash
mkcert -install
```

## 2. Generate a certificate for your local IP

Mobile devices connect over your local network, so `localhost` won't work — use your machine's IP.

```bash
# Find your local IP
# macOS
ipconfig getifaddr en0
# Linux
hostname -I | awk '{print $1}'
```

Generate the certificate (replace `192.168.1.100` with your IP):

```bash
mkcert -key-file key.pem -cert-file cert.pem 192.168.1.100
```

This creates `key.pem` and `cert.pem` in the current directory.

## 3. Start the adapter with HTTPS

```bash
uv run uvicorn main:app --reload --port 3001 \
  --host 192.168.1.100 \
  --ssl-keyfile=key.pem \
  --ssl-certfile=cert.pem
```

This uses `uvicorn` directly because the `fastapi` CLI doesn't expose SSL options. `--host` is set to your machine's IP (the same one from step 2) because the default (`127.0.0.1`) only accepts connections from the local machine. Using the specific IP rather than `0.0.0.0` limits access to your LAN interface instead of exposing the server on all network interfaces.

The server will now be available at `https://192.168.1.100:3001`.

## 4. Trust the local CA on your mobile device

The mkcert root CA must be installed on your mobile device so it trusts certificates you generate. This is a one-time step — any future certs from `mkcert` will be trusted automatically.

Find the CA certificate:

```bash
mkcert -CAROOT
# e.g. /Users/you/Library/Application Support/mkcert
```

The file you need is `rootCA.pem` in that directory.

**iOS:**

1. Transfer `rootCA.pem` to your device (AirDrop, email, or host it on a local HTTP server)
2. Open the file on the device — it will prompt you to install a configuration profile
3. Go to **Settings > General > VPN & Device Management** and install the profile
4. Go to **Settings > General > About > Certificate Trust Settings** and enable full trust for the certificate

**Android:**

1. Transfer `rootCA.pem` to your device
2. Go to **Settings > Security > Encryption & Credentials > Install a certificate > CA certificate**
3. Select the file and install it

## 5. Connect the Immich mobile app

1. Open the Immich app
2. Set the server URL to `https://192.168.1.100:3001` (your machine's IP)
3. Log in — the app will redirect to Clerk for OAuth, then back to the app

## Troubleshooting

- **"Certificate not trusted" or connection refused**: Verify the certificate is installed _and_ trusted on the device (on iOS, both steps — install and enable trust — are required)
- **"Hostname mismatch"**: The IP in the server URL must match the SAN in the certificate. Regenerate the cert if your IP has changed
- **OAuth callback fails**: Ensure `OAUTH_MOBILE_REDIRECT_URI` is set correctly in your `.env` (default: `app.immich:///oauth-callback`)
- **Can't reach server**: Ensure your mobile device and dev machine are on the same network, and that no firewall is blocking port 3001

## Monitoring Traffic

To inspect traffic from the Immich mobile client, you need a reverse proxy between the mobile client and the server. Immich uses the Flutter framework, which bypasses system proxy settings — a standard forward proxy won't see the traffic.

Set up a reverse proxy so the mobile client connects to your dev machine (which logs the traffic), and the proxy forwards requests to the actual server.

### Generic Reverse Proxy Setup

* Choose local listen endpoint: `http://<dev-machine-ip>:<port>`
* Configure upstream: `https://<real-immich-host>:<port>`
* Set Immich mobile "Server Endpoint URL" to the local listen endpoint
* Ensure device can reach dev machine IP (same Wi-Fi/VPN)
* _TLS note:_ if intercepting HTTPS, you may need to trust a local CA on the device and set "Allow self-signed SSL certificates" in the Advanced section of the mobile client Settings; if not intercepting, use simple pass-through/forwarding mode

### Proxyman Setup

* Select "Reverse Proxy..." from the "Tools Menu"
* Check "Enable Reverse Proxy Tool" if not already checked
* Click "+" in the lower left to create a new reverse proxy
* Specify a name for the proxy, the local port, the remote host or IP address, and the remote port
* If you are using OAuth with the Immich mobile client, you will need to run immich-adapter with a SSL certificate, and you will need to check "Force Using SSL when connecting to Remote Port"
* Click "Add" to create and start the reverse proxy

With Proxyman, if you are using OAuth, you will not specify https for the protocol of the immich-adapter server as the SSL connection is handled by Proxyman.
