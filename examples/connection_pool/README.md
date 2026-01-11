# Connection Pool Example

This example demonstrates how to maintain a connection pool of avatar sessions and handle multiple concurrent audio inputs efficiently over an extended period (~5 minutes).

## Features

- **Connection Pooling**: Pre-initializes a configurable number of WebSocket connections for faster request handling
- **Concurrent Processing**: Handles multiple audio requests simultaneously using async/await
- **Multi-Round Testing**: Runs multiple rounds of concurrent requests over time (simulating ~5 minutes of sustained usage)
- **Safe Resource Management**: Uses async context managers for automatic connection borrowing/returning
- **Connection Reuse**: Connections are reused across rounds, reducing overhead
- **Long-Lived Connections**: Tests connection stability over extended periods
- **Statistics Tracking**: Tracks request counts, timing, and per-round performance

## Configuration

Set the following environment variables:

```bash
export AVATAR_API_KEY="your-api-key"
export AVATAR_APP_ID="your-app-id"
export AVATAR_CONSOLE_ENDPOINT="https://console.example.com"
export AVATAR_INGRESS_ENDPOINT="https://ingress.example.com"
export AVATAR_SESSION_AVATAR_ID="your-avatar-id"

# Optional
export AVATAR_USE_QUERY_AUTH="false"  # Set to "true" for web-style auth
```

## Running the Example

```bash
# From the repository root
cd examples/connection_pool
python main.py
```

## How It Works

### Pool Initialization

The `AvatarConnectionPool` class manages a set of pre-initialized connections:

```python
pool = AvatarConnectionPool(
    pool_size=3,           # Number of connections to maintain
    config_factory=...,    # Factory function for session config
    session_ttl_minutes=5, # Session TTL
)

await pool.initialize()  # Creates and connects all sessions
```

### Borrowing Connections

Use the async context manager to safely borrow and return connections:

```python
async with pool.borrow() as conn:
    # Send audio using the borrowed connection
    request_id = await conn.session.send_audio(audio, end=True)
    
    # Wait for animation frames
    await conn.collector.wait(timeout=45)
    
    # Access results
    frames = conn.collector.frames
# Connection is automatically returned to the pool
```

### Concurrent Requests

When you have more requests than connections, they queue up:

```python
# With pool_size=3 and 5 concurrent requests:
# - 3 requests run immediately
# - 2 requests wait for connections to become available
tasks = [process_audio(pool, audio) for _ in range(5)]
results = await asyncio.gather(*tasks)
```

## Pool Configuration Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `POOL_SIZE` | 3 | Number of WebSocket connections to maintain |
| `CONCURRENT_REQUESTS` | 5 | Number of concurrent requests per round |
| `NUM_ROUNDS` | 10 | Number of rounds to run |
| `ROUND_INTERVAL` | 30.0 | Seconds between rounds (~5 min total with 10 rounds) |
| `SESSION_TTL` | 10 | Session time-to-live in minutes |
| `REQUEST_TIMEOUT` | 45 | Timeout for each audio request in seconds |

Modify these constants at the top of `main.py` to adjust behavior.

### Multi-Round Testing

The example runs multiple rounds of concurrent requests:

```python
# Run 10 rounds of 5 concurrent requests, with 30s between rounds
# Total: 50 requests over ~5 minutes
round_results = await run_multiple_rounds(
    pool=pool,
    audio=audio,
    num_rounds=10,
    requests_per_round=5,
    interval_seconds=30.0,
)
```

This tests:
- Connection stability over extended periods
- Connection reuse across many requests
- Pool behavior under sustained load

## Expected Output

```
Loaded audio file: 12345 bytes
Initializing connection pool with 3 connections...
  Connection 0: OK (connection_id=abc123...)
  Connection 1: OK (connection_id=def456...)
  Connection 2: OK (connection_id=ghi789...)
Pool initialized with 3/3 connections

============================================================
STARTING MULTI-ROUND TEST
============================================================
Rounds: 10
Requests per round: 5
Interval between rounds: 30.0s
Expected total duration: ~4.5 minutes
Pool size: 3 connections
============================================================

[Round 1/10] (elapsed: 0.0s, pool: 3/3 available)
  Completed: 5 OK, 0 FAILED in 2500.0ms
  Waiting 30.0s until next round...

[Round 2/10] (elapsed: 32.5s, pool: 3/3 available)
  Completed: 5 OK, 0 FAILED in 2400.0ms
  Waiting 30.0s until next round...

... (rounds 3-9) ...

[Round 10/10] (elapsed: 272.5s, pool: 3/3 available)
  Completed: 5 OK, 0 FAILED in 2300.0ms

============================================================
MULTI-ROUND TEST COMPLETE
Total duration: 275.0s (4.6 minutes)
============================================================

============================================================
MULTI-ROUND SUMMARY
============================================================

Overall Statistics:
  Total rounds: 10
  Total requests: 50
  Successful: 50 (100.0%)
  Failed: 0 (0.0%)

Request Performance:
  Avg duration: 2450.00ms
  Min duration: 2200.00ms
  Max duration: 2700.00ms
  Avg frames: 10.0

Per-Round Breakdown:
  Round  Time(s)    Duration(ms)   OK     FAIL  
  ------ ---------- -------------- ------ ------
  1      0.0        2500.0         5      0     
  2      32.5       2400.0         5      0     
  ...
  10     272.5      2300.0         5      0     

Connection Usage Distribution:
  abc123...: 17 requests (34.0%)
  def456...: 17 requests (34.0%)
  ghi789...: 16 requests (32.0%)

Final Pool Statistics:
  Total requests served: 50
  Connections in pool: 3
  Connection abc123...: 17 requests, age: 275.0s (4.6 min)
  Connection def456...: 17 requests, age: 275.0s (4.6 min)
  Connection ghi789...: 16 requests, age: 275.0s (4.6 min)

Closing connection pool...
Connection pool closed
```

## Integration with Your Application

To use this pattern in your own application:

1. Create the pool during application startup
2. Share the pool instance across request handlers
3. Use `pool.borrow()` in each request handler
4. Close the pool during application shutdown

Example with aiohttp:

```python
async def on_startup(app):
    pool = AvatarConnectionPool(...)
    await pool.initialize()
    app['avatar_pool'] = pool

async def on_shutdown(app):
    await app['avatar_pool'].close()

async def handle_request(request):
    pool = request.app['avatar_pool']
    async with pool.borrow() as conn:
        await conn.session.send_audio(audio, end=True)
        await conn.collector.wait()
        return web.json_response({"frames": len(conn.collector.frames)})
```

