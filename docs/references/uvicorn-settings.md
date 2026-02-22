---
title: "Uvicorn Server Settings"
last-updated: 2025-11-12
---

# Uvicorn Server Settings Explained

## Overview

This document provides a deep dive into the three uvicorn server settings recommended for iOS/Flutter client compatibility: `timeout-keep-alive`, `limit-concurrency`, and `backlog`.

These settings control how uvicorn (the ASGI server running your FastAPI application) handles HTTP connections, which is critical for mobile clients that make rapid successive requests.

---

## Table of Contents

1. [timeout-keep-alive](#timeout-keep-alive)
2. [limit-concurrency](#limit-concurrency)
3. [backlog](#backlog)
4. [Where These Settings Come From](#where-these-settings-come-from)
5. [Default Values and Why They Matter](#default-values-and-why-they-matter)
6. [How These Settings Work Together](#how-these-settings-work-together)
7. [Platform-Specific Behavior](#platform-specific-behavior)
8. [Monitoring and Tuning](#monitoring-and-tuning)

---

## timeout-keep-alive

### What It Is

The number of seconds to keep an idle HTTP connection open before closing it.

### Technical Details

**HTTP Keep-Alive** (also called HTTP persistent connections or HTTP connection reuse) allows multiple HTTP requests/responses to be sent over a single TCP connection instead of opening a new connection for each request.

**How it works:**
1. Client makes first request -> server responds
2. Instead of closing the TCP connection, server keeps it open
3. Client sends second request on the same connection -> server responds
4. Connection stays open until either:
   - Client closes it
   - Server closes it due to timeout
   - Server closes it due to max requests limit

**The timeout-keep-alive setting** controls how long the server waits for the next request on an idle connection before closing it.

### Where It Comes From

**HTTP/1.1 Specification (RFC 7230)**
- Section 6.3: "Persistence"
- Keep-alive is the **default behavior** in HTTP/1.1
- Servers must support persistent connections

**Uvicorn Implementation:**
- Uses the `timeout-keep-alive` parameter
- Implemented in `uvicorn.protocols.http.h11_impl.H11Protocol`
- Uses Python's `asyncio` event loop to manage timeouts

### Default Value

**Uvicorn default: 5 seconds**

```python
# From uvicorn/config.py
class Config:
    def __init__(
        self,
        # ... other parameters ...
        timeout_keep_alive: int = 5,  # Default: 5 seconds
    ):
```

### Why This Matters

**Problem with short timeout (5s):**
- Mobile clients often idle between requests (user thinking, network latency)
- 5 seconds is too short for typical mobile usage patterns
- Forces new TCP connection for each request (slow)
- TCP handshake overhead: ~100-200ms on cellular networks

**iOS/Mobile Client Expectations:**
- iOS URLSession default keep-alive: **75 seconds**
- Android OkHttp default: **75 seconds**
- Most modern HTTP clients: **60-120 seconds**

**When client expects longer keep-alive than server provides:**

```text
Client: "I'll reuse this connection for the next 75 seconds"
Server: *closes connection after 5 seconds*
Client: *tries to reuse closed connection*
Result: "Connection closed before full header was received"
```

### Recommended Value

**75 seconds** - matches mobile client expectations

```text
# Command line
uvicorn main:app --timeout-keep-alive 75

# Environment variable
UVICORN_TIMEOUT_KEEP_ALIVE=75

# Programmatic
uvicorn.run("main:app", timeout_keep_alive=75)
```

### Trade-offs

**Longer timeout (75s+):**
- Better mobile client compatibility
- Reduced connection overhead
- Lower latency for subsequent requests
- More idle connections consume resources
- Slightly higher memory usage

**Shorter timeout (5s):**
- Fewer idle connections
- Lower memory footprint
- Mobile clients experience connection errors
- Higher latency (more TCP handshakes)

### How to Observe

**Check current connections:**

```bash
# macOS/Linux
netstat -an | grep ESTABLISHED | grep :8080

# Count established connections
netstat -an | grep ESTABLISHED | grep :8080 | wc -l
```

**Monitor in application logs:**

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# Uvicorn logs connection lifecycle at DEBUG level
```

---

## limit-concurrency

### What It Is

Maximum number of concurrent connections the server will handle. When this limit is reached, new connections receive HTTP 503 (Service Unavailable) responses.

### Technical Details

**Connection limiting** is a protective mechanism that prevents server overload by capping the number of simultaneous client connections.

**How it works:**
1. Client attempts to connect
2. Server checks: `current_connections < limit_concurrency`?
3. If yes: Accept connection, increment counter
4. If no: Reject with 503, send "Retry-After" header
5. When connection closes: Decrement counter

**This is a hard limit** - exceeding it immediately returns 503, no queuing.

### Where It Comes From

**Application-level rate limiting concept:**
- Not from HTTP specification
- Common pattern in web servers (nginx, Apache, etc.)
- Prevents resource exhaustion attacks

**Uvicorn Implementation:**
- Uses `asyncio.Semaphore` for counting
- Implemented in `uvicorn.protocols.http.httptools_impl.HttpToolsProtocol`
- Checked before request processing begins

### Default Value

**Uvicorn default: None (unlimited)**

```python
# From uvicorn/config.py
class Config:
    def __init__(
        self,
        # ... other parameters ...
        limit_concurrency: int | None = None,  # Default: no limit
    ):
```

### Why This Matters

**Without limit (None):**
- Server accepts all connections
- Under heavy load: memory exhaustion, CPU saturation
- Can lead to complete server failure
- All requests become slow (everyone suffers)

**With limit:**
- Protects server from overload
- Some requests get 503 (explicit failure)
- Other requests process normally (graceful degradation)
- Server stays responsive

**Mobile client scenario:**

```text
10 iOS clients each making 5 rapid requests = 50 concurrent connections
Without limit: Server struggles, all requests slow
With limit_concurrency=1000: Server handles normally
```

### Recommended Value

**1000 connections** - generous for most applications

```text
# Command line
uvicorn main:app --limit-concurrency 1000

# Environment variable
UVICORN_LIMIT_CONCURRENCY=1000

# Programmatic
uvicorn.run("main:app", limit_concurrency=1000)
```

### How to Size This Setting

**Formula:**

```text
limit_concurrency = (workers x max_requests_per_worker) + buffer

Where:
- workers = number of uvicorn worker processes
- max_requests_per_worker = how many requests each worker can handle
- buffer = 20-50% extra for burst traffic
```

**Example calculations:**

**Small application (single worker):**

```text
Workers: 1
Max requests/worker: 100 (depends on async I/O)
Buffer: 50%
limit_concurrency = (1 x 100) x 1.5 = 150
```

**Medium application (4 workers):**

```text
Workers: 4
Max requests/worker: 200
Buffer: 25%
limit_concurrency = (4 x 200) x 1.25 = 1000
```

**Large application (8 workers):**

```text
Workers: 8
Max requests/worker: 500
Buffer: 20%
limit_concurrency = (8 x 500) x 1.2 = 4800
```

### Trade-offs

**Higher limit (1000+):**
- Handles traffic spikes
- Fewer 503 errors
- Can overload server if all connections active
- Higher memory usage under load

**Lower limit (100-500):**
- Protects server from overload
- Predictable resource usage
- More 503 errors under normal load
- May reject legitimate traffic

### HTTP 503 Response Behavior

When limit is reached, uvicorn returns:

```text
HTTP/1.1 503 Service Unavailable
Retry-After: 1
Content-Length: 0
```

**Client should:**
- Wait 1 second (Retry-After header)
- Retry the request
- Implement exponential backoff

### How to Observe

**Monitor current connection count:**

```python
# Add to your FastAPI app
from fastapi import Request

@app.middleware("http")
async def count_connections(request: Request, call_next):
    # uvicorn doesn't expose connection count directly
    # Use external monitoring (Prometheus, etc.)
    response = await call_next(request)
    return response
```

**Check 503 responses in logs:**

```bash
# Grep for 503 responses
tail -f /var/log/uvicorn.log | grep "503"
```

---

## backlog

### What It Is

The maximum number of pending connections that can wait in the socket's listen queue before the OS starts rejecting new connections.

### Technical Details

**TCP Connection Queue:**

When a client connects, there's a multi-step process:
1. **Client -> SYN packet** (connection request)
2. **Server -> SYN-ACK** (acknowledgment)
3. **Client -> ACK** (confirmation)
4. **Connection established** -> moves to accept queue

The **backlog** controls the size of the **accept queue** (step 4).

**What happens:**

```text
Listen Socket
    |
SYN Queue (OS managed, separate from backlog)
    |
Accept Queue (size = backlog parameter)
    |
Application accepts connection (uvicorn.run())
```

**When backlog is full:**
- OS rejects new SYN packets (connection refused)
- Or drops SYN packets (client retries)
- Client sees: "Connection refused" or timeout

### Where It Comes From

**POSIX socket API:**

```c
int listen(int sockfd, int backlog);
```

The `backlog` parameter in `listen()` system call.

**Operating System Implementation:**
- Linux: `/proc/sys/net/core/somaxconn` (system max)
- macOS: `kern.ipc.somaxconn` sysctl
- Windows: Registry setting

**Uvicorn Implementation:**

```python
# uvicorn/config.py
class Config:
    def __init__(
        self,
        backlog: int = 2048,  # Passed to socket.listen()
    ):
```

### Default Value

**Uvicorn default: 2048**

**Operating System defaults:**
- Linux: 4096 (modern kernels, was 128 in older versions)
- macOS: 128
- Windows: 200

**Important:** The actual backlog is `min(uvicorn_backlog, os_max_backlog)`

### Why This Matters

**Small backlog (128):**

```text
Burst of 200 connection requests arrives
First 128 -> queued in accept queue
Next 72 -> rejected by OS
Result: "Connection refused" errors
```

**Large backlog (2048+):**

```text
Burst of 200 connection requests arrives
All 200 -> queued in accept queue
Uvicorn processes them in order
Result: All connections succeed (may have latency)
```

**Mobile client scenario:**
- iOS app launches, syncs photos
- Makes 20-50 concurrent connection requests
- Small backlog: some connections rejected
- Large backlog: all queued, processed in order

**Real-world impact:**

```text
1000 iOS clients
Each makes 3 requests simultaneously = 3000 connections
If backlog = 128: Most connections fail
If backlog = 2048: Connections queue, succeed (with delay)
```

### Recommended Value

**2048** - uvicorn's default is already good

For very high traffic:
- **4096-8192** - check OS limits first

```text
# Command line
uvicorn main:app --backlog 2048

# Environment variable
UVICORN_BACKLOG=2048

# Programmatic
uvicorn.run("main:app", backlog=2048)
```

### Checking OS Limits

**Linux:**

```bash
# Check current limit
cat /proc/sys/net/core/somaxconn

# Set to 8192 (requires root)
sudo sysctl -w net.core.somaxconn=8192

# Make permanent
echo "net.core.somaxconn=8192" | sudo tee -a /etc/sysctl.conf
```

**macOS:**

```bash
# Check current limit
sysctl kern.ipc.somaxconn

# Set to 8192 (requires root)
sudo sysctl -w kern.ipc.somaxconn=8192

# Make permanent
echo "kern.ipc.somaxconn=8192" | sudo tee -a /etc/sysctl.conf
```

**Uvicorn will use the minimum:**

```python
actual_backlog = min(uvicorn_backlog, os_somaxconn)
```

### Trade-offs

**Larger backlog (2048+):**
- Handles traffic bursts
- Fewer connection refused errors
- Connections may wait longer in queue
- Slightly more kernel memory

**Smaller backlog (128-512):**
- Lower memory usage
- Fail-fast behavior
- More connection refused errors
- Poor handling of traffic spikes

### Backlog vs limit-concurrency

**Different concepts:**

| Setting | What It Limits | When It Applies | Failure Mode |
|---------|---------------|-----------------|--------------|
| `backlog` | Pending connections in OS queue | Before accept() | Connection refused |
| `limit-concurrency` | Active connections in application | After accept() | HTTP 503 |

**Flow:**

```text
Client connects
    |
OS accept queue (size = backlog)
    |
Uvicorn accepts connection
    |
Connection counter (limit = limit_concurrency)
    |
Request processing
```

### How to Observe

**Check current listen queue:**

```text
# Linux
ss -ltn | grep :8080
# Look for "Send-Q" column (current backlog usage)

# Example output:
# State    Recv-Q    Send-Q    Local Address:Port
# LISTEN   0         2048      0.0.0.0:8080
#          ^current  ^backlog
```

**Monitor connection refused errors:**

```bash
# Check system logs
dmesg | grep "connection refused"

# Application logs
grep "Connection refused" /var/log/uvicorn.log
```

---

## Where These Settings Come From

### Hierarchy of Sources

**1. HTTP/TCP Specifications**
- HTTP/1.1 Keep-Alive: RFC 7230
- TCP socket listen queue: POSIX.1-2001

**2. Operating System**
- Socket API implementation (BSD sockets)
- Kernel parameters (`somaxconn`, TCP settings)

**3. ASGI Specification**
- Defines interface between web servers and Python web apps
- Doesn't mandate specific timeout/concurrency settings

**4. Uvicorn (ASGI Server)**
- Implements ASGI specification
- Adds server management features
- Provides these configuration parameters

**5. Your Application (FastAPI)**
- Runs on top of uvicorn
- Can indirectly affect these through middleware
- Doesn't directly control socket settings

### Code References

**Uvicorn source code:**

```python
# uvicorn/config.py
class Config:
    def __init__(
        self,
        app: ASGIApplication | Callable | str,
        host: str = "127.0.0.1",
        port: int = 8000,
        # ...
        timeout_keep_alive: int = 5,
        limit_concurrency: int | None = None,
        backlog: int = 2048,
        # ...
    ):
        self.host = host
        self.port = port
        self.timeout_keep_alive = timeout_keep_alive
        self.limit_concurrency = limit_concurrency
        self.backlog = backlog
```

**Socket creation:**

```python
# uvicorn/protocols/http/h11_impl.py
def create_server_socket(host, port, backlog):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(backlog)  # <- backlog parameter used here
    return sock
```

**Keep-alive timeout:**

```python
# uvicorn/protocols/http/h11_impl.py
class H11Protocol:
    def __init__(self, config, ...):
        self.timeout_keep_alive = config.timeout_keep_alive

    async def handle_events(self):
        # After response sent:
        await asyncio.wait_for(
            self.receive_next_request(),
            timeout=self.timeout_keep_alive  # <- timeout used here
        )
```

**Concurrency limit:**

```python
# uvicorn/main.py
class Server:
    def __init__(self, config):
        if config.limit_concurrency:
            self.semaphore = asyncio.Semaphore(config.limit_concurrency)

    async def handle_connection(self):
        if self.semaphore:
            async with self.semaphore:  # <- limit enforced here
                await self.process_request()
```

---

## Default Values and Why They Matter

### Summary Table

| Setting | Uvicorn Default | Recommended for iOS | Why Different |
|---------|----------------|---------------------|---------------|
| `timeout-keep-alive` | 5 seconds | 75 seconds | iOS expects 75s keep-alive |
| `limit-concurrency` | None (unlimited) | 1000 | Protect from overload |
| `backlog` | 2048 | 2048 | Already good default |

### Why Uvicorn's Defaults Are Conservative

**Design philosophy:**
1. **Safe defaults** - won't exhaust server resources
2. **Web browser focused** - optimized for traditional web apps
3. **Low resource usage** - work on minimal hardware

**Why they don't match mobile clients:**
- Uvicorn predates widespread mobile API usage
- Mobile clients have different connection patterns
- iOS/Android SDKs use longer timeouts by default

### Impact of Using Defaults

**With uvicorn defaults:**

```text
Mobile client makes 5 rapid requests:
  Request 1: Success (new connection)
  Request 2: Success (reuses connection)
  ... 6 seconds pass (user thinks) ...
  Request 3: Fail - "Connection closed before full header was received"
  Request 4: Success (new connection)
  Request 5: Success (reuses new connection)
```

**With recommended values:**

```text
Mobile client makes 5 rapid requests:
  Request 1: Success (new connection)
  Request 2: Success (reuses connection)
  ... 60 seconds pass (user scrolls through photos) ...
  Request 3: Success (still reusing connection)
  Request 4: Success (still reusing connection)
  Request 5: Success (still reusing connection)
```

### When Defaults Are Sufficient

**You can use defaults if:**
- Web browsers only (not mobile apps)
- Low traffic (< 10 requests/second)
- No rapid successive requests
- No keep-alive requirements

**You need custom values if:**
- iOS/Android mobile apps
- High traffic (100+ requests/second)
- Rapid successive requests (API calls)
- Long-polling or streaming

---

## How These Settings Work Together

### Request Lifecycle

```text
1. Client initiates TCP connection
   |
2. OS checks: accept queue full?
   +-- Yes -> Connection refused (backlog exceeded)
   +-- No -> Add to accept queue
   |
3. Uvicorn accepts from queue
   |
4. Uvicorn checks: limit_concurrency reached?
   +-- Yes -> Return HTTP 503
   +-- No -> Process request
   |
5. FastAPI handles request
   |
6. Response sent
   |
7. Keep connection open
   |
8. Wait for next request (timeout_keep_alive)
   +-- Request arrives -> Go to step 4
   +-- Timeout -> Close connection
```

### Example Scenario: 1000 Simultaneous Clients

**Configuration:**

```text
timeout_keep_alive = 75
limit_concurrency = 1000
backlog = 2048
```

**What happens:**

```text
T=0s: 1000 clients connect simultaneously
+-- OS: Accept queue receives 1000 connections (< 2048 backlog)
+-- Uvicorn: Accepts all 1000 (< 1000 limit)
+-- All process successfully

T=5s: Clients make second request on existing connections
+-- No new connections needed (keep-alive still active)
+-- All 1000 requests process on existing connections
+-- Much faster (no TCP handshake)

T=60s: Clients make third request
+-- Still within 75s keep-alive window
+-- All 1000 reuse existing connections
+-- Optimal performance

T=80s: Connection timeout
+-- Keep-alive timeout (75s) exceeded
+-- Uvicorn closes all idle connections
+-- limit_concurrency drops to 0 (ready for new connections)
```

### Interaction Matrix

| Scenario | backlog | limit_concurrency | timeout_keep_alive | Result |
|----------|---------|-------------------|-------------------|--------|
| Burst of connections | Critical | Critical | Not relevant | Backlog queues, limit rejects excess |
| Sustained high traffic | Less important | Critical | Important | Limit protects server, timeout manages resources |
| Mobile clients | Important | Important | Critical | Timeout prevents connection errors |
| Slow clients | Less important | Critical | Important | Limit prevents resource exhaustion |

### Tuning Strategy

**Step 1: Start with recommended values**

```text
timeout_keep_alive = 75
limit_concurrency = 1000
backlog = 2048
```

**Step 2: Monitor under load**
- Connection refused errors -> increase `backlog`
- 503 errors -> increase `limit_concurrency`
- "Connection closed" errors -> increase `timeout_keep_alive`

**Step 3: Adjust based on patterns**

```text
# Low traffic, mobile apps
timeout_keep_alive = 120  # Even longer for slow networks
limit_concurrency = 500   # Lower is fine
backlog = 2048            # Keep default

# High traffic, web browsers
timeout_keep_alive = 30   # Shorter is okay
limit_concurrency = 5000  # Much higher needed
backlog = 4096            # Increase for bursts

# API with rapid successive calls
timeout_keep_alive = 75   # Match client expectations
limit_concurrency = 2000  # Moderate-high
backlog = 2048            # Keep default
```

---

## Platform-Specific Behavior

### Linux

**Characteristics:**
- Generous OS limits
- Efficient connection handling
- Good async I/O performance

**OS maximums:**

```bash
# Check limits
cat /proc/sys/net/core/somaxconn          # Usually 4096
cat /proc/sys/net/ipv4/tcp_max_syn_backlog # Usually 2048
cat /proc/sys/fs/file-max                  # Usually 100000+
```

**Recommended settings:**

```text
timeout_keep_alive = 75
limit_concurrency = 2000
backlog = 4096  # Can go higher due to OS limit
```

### macOS

**Characteristics:**
- Lower OS limits by default
- May need sysctl adjustments
- Development-focused

**OS maximums:**

```bash
# Check limits
sysctl kern.ipc.somaxconn    # Usually 128 (very low!)
sysctl kern.maxfiles         # Usually 12288
```

**Important: Increase OS limit for development**

```bash
sudo sysctl -w kern.ipc.somaxconn=2048
```

**Recommended settings:**

```text
timeout_keep_alive = 75
limit_concurrency = 1000
backlog = 2048  # After increasing OS limit
```

### Windows

**Characteristics:**
- Different socket implementation
- May have different defaults
- Production use less common for Python servers

**Recommended settings:**

```text
timeout_keep_alive = 75
limit_concurrency = 1000
backlog = 2048
```

### Docker Containers

**Special considerations:**
- Inherits host OS limits
- May have container-specific limits
- Kubernetes/orchestration may impose limits

**Check limits in container:**

```bash
docker run --rm -it your-image cat /proc/sys/net/core/somaxconn
```

**Override if needed:**

```bash
docker run --sysctl net.core.somaxconn=4096 your-image
```

### Cloud Platforms

**Render:**
- Linux-based containers
- Generous OS limits
- Recommended settings work well as-is

**AWS ECS/Fargate:**
- May need to configure task definition
- Network mode affects limits

**Google Cloud Run:**
- Concurrency per instance limited (default: 80)
- May need to adjust limit_concurrency to match

**Heroku:**
- Router has 30-second timeout
- Keep timeout_keep_alive < 30s on Heroku

---

## Monitoring and Tuning

### Metrics to Track

**Connection metrics:**

```python
# Prometheus example
from prometheus_client import Counter, Gauge

connection_count = Gauge('uvicorn_connections_active', 'Active connections')
connection_rejected = Counter('uvicorn_connections_rejected', 'Rejected connections')
connection_timeout = Counter('uvicorn_connections_timeout', 'Timed out connections')
```

**Key indicators:**
1. **Active connections** (should be < limit_concurrency)
2. **Connection refused rate** (should be near 0)
3. **503 response rate** (should be < 1%)
4. **Average connection duration** (should match timeout_keep_alive usage)
5. **Connection reuse rate** (higher = better)

### Load Testing

**Apache Bench (ab):**

```bash
# Test concurrent connections
ab -n 1000 -c 100 http://localhost:8080/api/server/ping

# Test with keep-alive
ab -n 1000 -c 100 -k http://localhost:8080/api/server/ping
```

**wrk (modern alternative):**

```bash
# Test sustained load
wrk -t4 -c100 -d30s http://localhost:8080/api/server/ping

# Test with custom script
wrk -t4 -c100 -d30s -s rapid-requests.lua http://localhost:8080/
```

### Signs You Need to Adjust

**Increase timeout_keep_alive if:**
- Seeing "Connection closed before full header was received"
- High rate of new connections (should reuse)
- Mobile clients reporting errors

**Increase limit_concurrency if:**
- Seeing HTTP 503 responses
- Connection counter always at max
- Legitimate traffic being rejected

**Increase backlog if:**
- Seeing "Connection refused" in client logs
- Traffic spikes cause failures
- Listen queue shown as full in `ss` output

**Decrease timeout_keep_alive if:**
- Too many idle connections
- Memory usage creeping up
- Connection count always high

**Decrease limit_concurrency if:**
- Server running out of memory
- CPU usage at 100%
- Database connection pool exhausted

### Example Tuning Session

**Starting point:**

```text
timeout_keep_alive = 5    # Default
limit_concurrency = None  # No limit
backlog = 2048           # Default
```

**Observation:**

```text
Mobile client errors: "Connection closed before full header was received"
Error rate: 15% of requests
```

**Action 1: Increase timeout_keep_alive**

```text
timeout_keep_alive = 75
```

**Result:**

```text
Mobile client errors dropped to 2%
Connection reuse rate increased from 20% to 80%
```

**New observation:**

```text
Under load test: 503 errors appearing
Server CPU at 95%
```

**Action 2: Add limit_concurrency**

```text
limit_concurrency = 1000
```

**Result:**

```text
503 errors: 5% during peak (acceptable)
Server CPU stable at 70%
No server crashes
```

**Final configuration:**

```text
timeout_keep_alive = 75
limit_concurrency = 1000
backlog = 2048  # Default was fine
```

---

## Summary

### Quick Reference

```python
# Recommended for iOS/mobile apps
uvicorn.run(
    "main:app",
    host="0.0.0.0",
    port=8080,
    timeout_keep_alive=75,      # Match mobile client expectations
    limit_concurrency=1000,     # Protect from overload
    backlog=2048,               # Handle connection bursts
)
```

### Key Takeaways

1. **timeout_keep_alive (75s)**: Prevents connection reuse errors with mobile clients
2. **limit_concurrency (1000)**: Protects server from resource exhaustion
3. **backlog (2048)**: Handles bursts of simultaneous connections
4. **These settings are layered**: OS -> Uvicorn -> Your app
5. **Monitor and adjust**: Start with recommendations, tune based on metrics
6. **Platform matters**: Check OS limits (especially macOS)

### Further Reading

- [Uvicorn Settings Documentation](https://www.uvicorn.org/settings/)
- [RFC 7230 - HTTP/1.1: Message Syntax and Routing](https://tools.ietf.org/html/rfc7230)
- [TCP Socket Programming Guide](https://beej.us/guide/bgnet/)
- [Linux Socket Programming](https://man7.org/linux/man-pages/man2/listen.2.html)
- [iOS URLSession Keep-Alive](https://developer.apple.com/documentation/foundation/urlsession)
