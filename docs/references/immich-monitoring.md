---
title: "Immich Monitoring and Logging"
last-updated: 2026-03-19
---

# Immich Monitoring and Logging

The goal of the adapter is to implement the Immich OpenAPI as accurately as possible. At times you will find that the Immich documentation does not deeply describe the format of data returned by endpoints or the actual data itself (such as `/sync/stream`). Monitoring real Immich client traffic is the best way to understand the expected behavior.

## Immich Web Client

The Immich web client is easy to monitor — use the developer tools in the web browser of your choice.

## Immich Mobile Clients

To monitor a mobile client, you will need a proxy server to be "in the middle" between the mobile client and the server. Unfortunately Immich uses the Flutter framework which is able to bypass the system proxy settings — your proxy server will not see any of the calls from the client to the server.

To get around this, you'll need to set up a reverse proxy — the mobile client thinks it is talking to the Immich server, but it is actually talking to a proxy server on your development machine (which logs the traffic) which then forwards the traffic on to the actual Immich server.

### Example Generic Reverse Proxy Setup

* Choose local listen endpoint: `http://<dev-machine-ip>:<port>`
* Configure upstream: `https://<real-immich-host>:<port>`
* Set Immich mobile "Server Endpoint URL" to the local listen endpoint
* Ensure device can reach dev machine IP (same Wi-Fi/VPN)
* _TLS note:_ if intercepting HTTPS, you may need to trust a local CA on the device and set "Allow self-signed SSL certificates" in the Advanced section of the mobile client Settings; if not intercepting, use simple pass-through/forwarding mode

### Example Proxyman Setup

* Select "Reverse Proxy..." from the "Tools Menu"
* Check "Enable Reverse Proxy Tool" if not already checked
* Click "+" in the lower left to create a new reverse proxy
* Specify a name for the proxy, the local port, the remote host or IP address, and the remote port
* If you are using OAuth with the Immich mobile client, you will need to run immich-adapter with a SSL certificate, and you will need to check "Force Using SSL when connecting to Remote Port"
* Click "Add" to create and start the reverse proxy

With Proxyman, if you are using OAuth, you will not specify https for the protocol of the immich-adapter server as the SSL connection is handled by Proxyman.
