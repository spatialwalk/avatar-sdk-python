"""
Example: Connection Pool with Concurrent Audio Processing

This example demonstrates how to:
1. Maintain a connection pool of avatar sessions for efficient reuse
2. Handle multiple concurrent audio inputs simultaneously
3. Properly manage connection lifecycle and error handling
4. Use async context managers for safe resource cleanup

The pool pre-initializes a configurable number of connections and provides
a way to borrow/return them for concurrent request processing.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Callable

from avatar_sdk_python import AvatarSession, new_avatar_session, SessionTokenError


# Configuration
POOL_SIZE = 3  # Number of connections to maintain
CONCURRENT_REQUESTS = 5  # Number of concurrent audio requests per round
NUM_ROUNDS = 10  # Number of rounds to run
ROUND_INTERVAL = 30.0  # Seconds between rounds (total ~5 minutes with 10 rounds)
AUDIO_FILE_PATH = "../../audio.pcm"
REQUEST_TIMEOUT = 45  # seconds
SESSION_TTL = 10  # minutes (longer for pool reuse over multiple rounds)


@dataclass
class RequestResult:
    """Result of a single audio request."""
    request_id: str
    connection_id: str
    frame_count: int
    duration_ms: float
    success: bool
    error: str | None = None


@dataclass
class AnimationCollector:
    """Collects animation frames from an avatar session."""
    
    frames: list[bytes] = field(default_factory=list)
    last: bool = False
    error: Exception | None = None
    _done: asyncio.Event = field(default_factory=asyncio.Event)
    
    def transport_frame(self, data: bytes, last: bool) -> None:
        """Callback for receiving animation frames."""
        self.frames.append(bytes(data))
        if last:
            self.last = True
            self._done.set()
    
    def on_error(self, error: Exception) -> None:
        """Callback for handling errors."""
        if error and not self.error:
            self.error = error
        self._done.set()
    
    def on_close(self) -> None:
        """Callback for handling session close."""
        if not self.last and not self.error:
            self.error = Exception("Session closed before final animation frame")
        self._done.set()
    
    def reset(self) -> None:
        """Reset collector for reuse."""
        self.frames = []
        self.last = False
        self.error = None
        self._done = asyncio.Event()
    
    async def wait(self, timeout: float | None = None) -> None:
        """Wait for collection to complete."""
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError("Timed out waiting for animation frames") from e
        
        if self.error:
            raise self.error


@dataclass
class PooledConnection:
    """A pooled avatar session with its collector."""
    session: AvatarSession
    collector: AnimationCollector
    connection_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    request_count: int = 0


class AvatarConnectionPool:
    """
    Manages a pool of avatar session connections.
    
    Features:
    - Pre-initializes connections for faster request handling
    - Provides async context manager for safe borrowing/returning
    - Handles connection health and automatic reconnection
    - Thread-safe connection management using asyncio primitives
    """
    
    def __init__(
        self,
        pool_size: int,
        config_factory: Callable[[AnimationCollector], dict],
        session_ttl_minutes: int = 5,
    ):
        """
        Initialize the connection pool.
        
        Args:
            pool_size: Number of connections to maintain.
            config_factory: Factory function that returns session config dict.
                           Takes AnimationCollector as argument to wire callbacks.
            session_ttl_minutes: Session TTL in minutes.
        """
        self._pool_size = pool_size
        self._config_factory = config_factory
        self._session_ttl = session_ttl_minutes
        
        self._available: asyncio.Queue[PooledConnection] = asyncio.Queue()
        self._all_connections: list[PooledConnection] = []
        self._lock = asyncio.Lock()
        self._initialized = False
        self._closing = False
    
    async def initialize(self) -> None:
        """
        Initialize all pool connections.
        
        This creates and connects all sessions in the pool.
        Call this before borrowing connections.
        """
        async with self._lock:
            if self._initialized:
                return
            
            print(f"Initializing connection pool with {self._pool_size} connections...")
            
            # Create all connections concurrently
            tasks = [self._create_connection(i) for i in range(self._pool_size)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success_count = 0
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    print(f"  Connection {i}: FAILED - {result}")
                else:
                    self._all_connections.append(result)
                    await self._available.put(result)
                    success_count += 1
                    print(f"  Connection {i}: OK (connection_id={result.connection_id})")
            
            if success_count == 0:
                raise RuntimeError("Failed to create any connections")
            
            print(f"Pool initialized with {success_count}/{self._pool_size} connections")
            self._initialized = True
    
    async def _create_connection(self, index: int) -> PooledConnection:
        """Create and initialize a single connection."""
        collector = AnimationCollector()
        config = self._config_factory(collector)
        
        # Override expire_at with our pool TTL
        config["expire_at"] = datetime.now(timezone.utc) + timedelta(minutes=self._session_ttl)
        
        session = new_avatar_session(**config)
        
        # Initialize and start the session
        await session.init()
        connection_id = await session.start()
        
        return PooledConnection(
            session=session,
            collector=collector,
            connection_id=connection_id,
        )
    
    @asynccontextmanager
    async def borrow(self, timeout: float = 30.0) -> AsyncIterator[PooledConnection]:
        """
        Borrow a connection from the pool.
        
        Usage:
            async with pool.borrow() as conn:
                await conn.session.send_audio(audio, end=True)
                await conn.collector.wait()
        
        The connection is automatically returned when the context exits.
        """
        if not self._initialized:
            raise RuntimeError("Pool not initialized. Call initialize() first.")
        
        if self._closing:
            raise RuntimeError("Pool is closing, cannot borrow connections")
        
        try:
            conn = await asyncio.wait_for(self._available.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timed out waiting for available connection (waited {timeout}s)")
        
        try:
            # Reset collector for new request
            conn.collector.reset()
            yield conn
        finally:
            if not self._closing:
                conn.request_count += 1
                await self._available.put(conn)
    
    async def close(self) -> None:
        """Close all connections in the pool."""
        async with self._lock:
            self._closing = True
            print("Closing connection pool...")
            
            # Close all connections
            for conn in self._all_connections:
                try:
                    await conn.session.close()
                except Exception as e:
                    print(f"  Error closing connection {conn.connection_id}: {e}")
            
            self._all_connections.clear()
            
            # Drain the queue
            while not self._available.empty():
                try:
                    self._available.get_nowait()
                except asyncio.QueueEmpty:
                    break
            
            self._initialized = False
            print("Connection pool closed")
    
    @property
    def available_count(self) -> int:
        """Number of currently available connections."""
        return self._available.qsize()
    
    @property
    def total_count(self) -> int:
        """Total number of connections in the pool."""
        return len(self._all_connections)
    
    def get_stats(self) -> dict:
        """Get pool statistics."""
        return {
            "total_connections": self.total_count,
            "available_connections": self.available_count,
            "total_requests_served": sum(c.request_count for c in self._all_connections),
            "connections": [
                {
                    "connection_id": c.connection_id,
                    "request_count": c.request_count,
                    "age_seconds": (datetime.now(timezone.utc) - c.created_at).total_seconds(),
                }
                for c in self._all_connections
            ],
        }


async def process_audio_request(
    pool: AvatarConnectionPool,
    audio: bytes,
    request_num: int,
) -> RequestResult:
    """
    Process a single audio request using a pooled connection.
    
    Args:
        pool: The connection pool.
        audio: Audio data to send.
        request_num: Request number for logging.
    
    Returns:
        RequestResult with timing and frame count.
    """
    start_time = time.monotonic()
    
    try:
        async with pool.borrow() as conn:
            # Send audio
            request_id = await conn.session.send_audio(audio, end=True)
            
            # Wait for response
            await conn.collector.wait(timeout=REQUEST_TIMEOUT)
            
            duration_ms = (time.monotonic() - start_time) * 1000
            
            return RequestResult(
                request_id=request_id,
                connection_id=conn.connection_id,
                frame_count=len(conn.collector.frames),
                duration_ms=duration_ms,
                success=True,
            )
    
    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        return RequestResult(
            request_id="",
            connection_id="",
            frame_count=0,
            duration_ms=duration_ms,
            success=False,
            error=str(e),
        )


async def run_concurrent_test(
    pool: AvatarConnectionPool,
    audio: bytes,
    num_requests: int,
) -> list[RequestResult]:
    """
    Run multiple concurrent audio requests.
    
    Args:
        pool: The connection pool.
        audio: Audio data to send.
        num_requests: Number of concurrent requests.
    
    Returns:
        List of results for all requests.
    """
    print(f"\nStarting {num_requests} concurrent audio requests...")
    print(f"Pool has {pool.available_count}/{pool.total_count} connections available")
    
    start_time = time.monotonic()
    
    # Create tasks for all requests
    tasks = [
        process_audio_request(pool, audio, i)
        for i in range(num_requests)
    ]
    
    # Run all concurrently
    results = await asyncio.gather(*tasks)
    
    total_duration = (time.monotonic() - start_time) * 1000
    
    print(f"\nCompleted {num_requests} requests in {total_duration:.2f}ms")
    
    return list(results)


@dataclass
class RoundResult:
    """Result of a single round of concurrent requests."""
    round_num: int
    start_time: float
    duration_ms: float
    successful: int
    failed: int
    results: list[RequestResult]


async def run_multiple_rounds(
    pool: AvatarConnectionPool,
    audio: bytes,
    num_rounds: int,
    requests_per_round: int,
    interval_seconds: float,
) -> list[RoundResult]:
    """
    Run multiple rounds of concurrent audio requests over time.
    
    This simulates sustained usage of the connection pool over an extended
    period (e.g., 5 minutes), testing that connections remain stable and
    performant across many requests.
    
    Args:
        pool: The connection pool.
        audio: Audio data to send.
        num_rounds: Number of rounds to run.
        requests_per_round: Number of concurrent requests per round.
        interval_seconds: Seconds to wait between rounds.
    
    Returns:
        List of RoundResult for each round.
    """
    total_expected_duration = (num_rounds - 1) * interval_seconds
    print(f"\n{'='*60}")
    print(f"STARTING MULTI-ROUND TEST")
    print(f"{'='*60}")
    print(f"Rounds: {num_rounds}")
    print(f"Requests per round: {requests_per_round}")
    print(f"Interval between rounds: {interval_seconds}s")
    print(f"Expected total duration: ~{total_expected_duration/60:.1f} minutes")
    print(f"Pool size: {pool.total_count} connections")
    print(f"{'='*60}")
    
    overall_start = time.monotonic()
    round_results: list[RoundResult] = []
    
    for round_num in range(num_rounds):
        round_start = time.monotonic()
        elapsed_total = round_start - overall_start
        
        print(f"\n[Round {round_num + 1}/{num_rounds}] "
              f"(elapsed: {elapsed_total:.1f}s, "
              f"pool: {pool.available_count}/{pool.total_count} available)")
        
        # Run concurrent requests for this round
        tasks = [
            process_audio_request(pool, audio, i)
            for i in range(requests_per_round)
        ]
        results = await asyncio.gather(*tasks)
        results = list(results)
        
        round_duration = (time.monotonic() - round_start) * 1000
        successful = sum(1 for r in results if r.success)
        failed = sum(1 for r in results if not r.success)
        
        round_result = RoundResult(
            round_num=round_num + 1,
            start_time=elapsed_total,
            duration_ms=round_duration,
            successful=successful,
            failed=failed,
            results=results,
        )
        round_results.append(round_result)
        
        print(f"  Completed: {successful} OK, {failed} FAILED in {round_duration:.1f}ms")
        
        # Show any errors
        for r in results:
            if not r.success:
                print(f"    ERROR: {r.error}")
        
        # Wait before next round (except for the last round)
        if round_num < num_rounds - 1:
            print(f"  Waiting {interval_seconds}s until next round...")
            await asyncio.sleep(interval_seconds)
    
    overall_duration = time.monotonic() - overall_start
    print(f"\n{'='*60}")
    print(f"MULTI-ROUND TEST COMPLETE")
    print(f"Total duration: {overall_duration:.1f}s ({overall_duration/60:.1f} minutes)")
    print(f"{'='*60}")
    
    return round_results


def print_multi_round_summary(round_results: list[RoundResult]) -> None:
    """Print summary of multi-round test results."""
    total_requests = sum(r.successful + r.failed for r in round_results)
    total_successful = sum(r.successful for r in round_results)
    total_failed = sum(r.failed for r in round_results)
    
    all_results = [r for rr in round_results for r in rr.results]
    successful_results = [r for r in all_results if r.success]
    
    print(f"\n{'='*60}")
    print("MULTI-ROUND SUMMARY")
    print(f"{'='*60}")
    
    print(f"\nOverall Statistics:")
    print(f"  Total rounds: {len(round_results)}")
    print(f"  Total requests: {total_requests}")
    print(f"  Successful: {total_successful} ({100*total_successful/total_requests:.1f}%)")
    print(f"  Failed: {total_failed} ({100*total_failed/total_requests:.1f}%)")
    
    if successful_results:
        durations = [r.duration_ms for r in successful_results]
        frames = [r.frame_count for r in successful_results]
        
        print(f"\nRequest Performance:")
        print(f"  Avg duration: {sum(durations)/len(durations):.2f}ms")
        print(f"  Min duration: {min(durations):.2f}ms")
        print(f"  Max duration: {max(durations):.2f}ms")
        print(f"  Avg frames: {sum(frames)/len(frames):.1f}")
    
    # Per-round breakdown
    print(f"\nPer-Round Breakdown:")
    print(f"  {'Round':<6} {'Time(s)':<10} {'Duration(ms)':<14} {'OK':<6} {'FAIL':<6}")
    print(f"  {'-'*6} {'-'*10} {'-'*14} {'-'*6} {'-'*6}")
    for rr in round_results:
        print(f"  {rr.round_num:<6} {rr.start_time:<10.1f} {rr.duration_ms:<14.1f} "
              f"{rr.successful:<6} {rr.failed:<6}")
    
    # Connection usage distribution
    conn_usage: dict[str, int] = {}
    for r in successful_results:
        conn_usage[r.connection_id] = conn_usage.get(r.connection_id, 0) + 1
    
    if conn_usage:
        print(f"\nConnection Usage Distribution:")
        for conn_id, count in sorted(conn_usage.items()):
            pct = 100 * count / len(successful_results)
            print(f"  {conn_id[:20]}...: {count} requests ({pct:.1f}%)")


def print_results(results: list[RequestResult]) -> None:
    """Print summary of request results."""
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    
    print(f"\nSuccessful: {len(successful)}/{len(results)}")
    print(f"Failed: {len(failed)}/{len(results)}")
    
    if successful:
        avg_duration = sum(r.duration_ms for r in successful) / len(successful)
        avg_frames = sum(r.frame_count for r in successful) / len(successful)
        
        print(f"\nSuccessful Request Stats:")
        print(f"  Average duration: {avg_duration:.2f}ms")
        print(f"  Average frames: {avg_frames:.1f}")
        print(f"  Min duration: {min(r.duration_ms for r in successful):.2f}ms")
        print(f"  Max duration: {max(r.duration_ms for r in successful):.2f}ms")
    
    if failed:
        print(f"\nFailed Requests:")
        for r in failed:
            print(f"  - {r.error}")
    
    # Show connection distribution
    conn_usage = {}
    for r in successful:
        conn_usage[r.connection_id] = conn_usage.get(r.connection_id, 0) + 1
    
    if conn_usage:
        print(f"\nConnection Usage Distribution:")
        for conn_id, count in sorted(conn_usage.items()):
            print(f"  {conn_id[:16]}...: {count} requests")


async def main() -> int:
    """Main entry point for the example."""
    # Load configuration
    config = load_config()
    
    # Load audio file
    audio = load_audio(AUDIO_FILE_PATH)
    print(f"Loaded audio file: {len(audio)} bytes")
    
    def config_factory(collector: AnimationCollector) -> dict:
        """Factory to create session config with collector callbacks."""
        return {
            "api_key": config["api_key"],
            "app_id": config["app_id"],
            "use_query_auth": config["use_query_auth"],
            "console_endpoint_url": config["console_url"],
            "ingress_endpoint_url": config["ingress_url"],
            "avatar_id": config["avatar_id"],
            "transport_frames": collector.transport_frame,
            "on_error": collector.on_error,
            "on_close": collector.on_close,
        }
    
    # Create connection pool
    pool = AvatarConnectionPool(
        pool_size=POOL_SIZE,
        config_factory=config_factory,
        session_ttl_minutes=SESSION_TTL,
    )
    
    try:
        # Initialize the pool
        await pool.initialize()
        
        # Run multiple rounds of concurrent requests over time
        # This tests sustained connection usage (simulating ~5 minutes of activity)
        round_results = await run_multiple_rounds(
            pool=pool,
            audio=audio,
            num_rounds=NUM_ROUNDS,
            requests_per_round=CONCURRENT_REQUESTS,
            interval_seconds=ROUND_INTERVAL,
        )
        
        # Print multi-round summary
        print_multi_round_summary(round_results)
        
        # Print pool stats
        stats = pool.get_stats()
        print(f"\nFinal Pool Statistics:")
        print(f"  Total requests served: {stats['total_requests_served']}")
        print(f"  Connections in pool: {stats['total_connections']}")
        for conn in stats["connections"]:
            print(
                f"  Connection {conn['connection_id'][:20]}...: "
                f"{conn['request_count']} requests, "
                f"age: {conn['age_seconds']:.1f}s ({conn['age_seconds']/60:.1f} min)"
            )
        
        return 0
    
    except SessionTokenError as e:
        print(f"Session token error: {e}")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await pool.close()


def load_config() -> dict:
    """Load configuration from environment variables."""
    api_key = os.getenv("AVATAR_API_KEY", "").strip()
    app_id = os.getenv("AVATAR_APP_ID", "").strip()
    use_query_auth = os.getenv("AVATAR_USE_QUERY_AUTH", "").strip().lower() in (
        "1", "true", "yes", "y", "on"
    )
    console_url = os.getenv("AVATAR_CONSOLE_ENDPOINT", "").strip()
    ingress_url = os.getenv("AVATAR_INGRESS_ENDPOINT", "").strip()
    avatar_id = os.getenv("AVATAR_SESSION_AVATAR_ID", "").strip()
    
    missing = []
    if not api_key:
        missing.append("AVATAR_API_KEY")
    if not app_id:
        missing.append("AVATAR_APP_ID")
    if not console_url:
        missing.append("AVATAR_CONSOLE_ENDPOINT")
    if not ingress_url:
        missing.append("AVATAR_INGRESS_ENDPOINT")
    if not avatar_id:
        missing.append("AVATAR_SESSION_AVATAR_ID")
    
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    
    return {
        "api_key": api_key,
        "app_id": app_id,
        "use_query_auth": use_query_auth,
        "console_url": console_url,
        "ingress_url": ingress_url,
        "avatar_id": avatar_id,
    }


def load_audio(path: str) -> bytes:
    """Load audio file from disk."""
    audio_path = Path(__file__).parent / path
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    return audio_path.read_bytes()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)

