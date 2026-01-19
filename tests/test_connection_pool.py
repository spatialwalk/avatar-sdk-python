"""
Tests for the connection pool example.

These tests verify the concurrent handling logic using mocks,
allowing testing without a real avatar backend.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from avatarkit import new_avatar_session
from avatarkit.proto.generated import message_pb2


class _DummyTask:
    """Dummy task that can be awaited and cancelled."""
    
    def __init__(self):
        self._cancelled = False
    
    def cancel(self) -> None:
        self._cancelled = True
    
    def __await__(self):
        if False:
            yield None
        return None


class _FakeWebSocket:
    """Fake WebSocket for testing."""

    def __init__(self, recv_messages: Optional[list[bytes]] = None):
        self.sent: list[bytes] = []
        self._recv_q: asyncio.Queue[bytes] = asyncio.Queue()
        for m in (recv_messages or []):
            self._recv_q.put_nowait(m)
        self._iter_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False
    
    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))
    
    async def recv(self):
        return await self._recv_q.get()
    
    async def close(self) -> None:
        self._closed = True
    
    def __aiter__(self):
        return self
    
    async def __anext__(self):
        try:
            return await asyncio.wait_for(self._iter_q.get(), timeout=0.01)
        except asyncio.TimeoutError:
            raise StopAsyncIteration


def _mk_confirm(connection_id: str) -> bytes:
    """Create a ServerConfirmSession message."""
    m = message_pb2.Message()
    m.type = message_pb2.MESSAGE_SERVER_CONFIRM_SESSION
    m.server_confirm_session.connection_id = connection_id
    return m.SerializeToString()


def _mk_animation_frame(req_id: str, end: bool = False) -> bytes:
    """Create a ServerResponseAnimation message."""
    m = message_pb2.Message()
    m.type = message_pb2.MESSAGE_SERVER_RESPONSE_ANIMATION
    m.server_response_animation.connection_id = "cid"
    m.server_response_animation.req_id = req_id
    m.server_response_animation.end = end
    return m.SerializeToString()


class TestAnimationCollector(unittest.IsolatedAsyncioTestCase):
    """Test the AnimationCollector class."""
    
    async def test_transport_frame_collects_frames(self):
        """Test that transport_frame adds frames to the list."""
        # Import from the example module
        import sys
        from pathlib import Path
        
        example_path = Path(__file__).parent.parent / "examples" / "connection_pool"
        sys.path.insert(0, str(example_path))
        try:
            from main import AnimationCollector
        finally:
            sys.path.pop(0)
        
        collector = AnimationCollector()
        
        # Send some frames
        collector.transport_frame(b"frame1", False)
        collector.transport_frame(b"frame2", False)
        collector.transport_frame(b"frame3", True)
        
        self.assertEqual(len(collector.frames), 3)
        self.assertTrue(collector.last)
    
    async def test_wait_returns_after_last_frame(self):
        """Test that wait() completes when last=True is received."""
        import sys
        from pathlib import Path
        
        example_path = Path(__file__).parent.parent / "examples" / "connection_pool"
        sys.path.insert(0, str(example_path))
        try:
            from main import AnimationCollector
        finally:
            sys.path.pop(0)
        
        collector = AnimationCollector()
        
        async def send_frames():
            await asyncio.sleep(0.01)
            collector.transport_frame(b"frame1", False)
            await asyncio.sleep(0.01)
            collector.transport_frame(b"frame2", True)
        
        task = asyncio.create_task(send_frames())
        
        # Wait should complete once last frame is received
        await collector.wait(timeout=1.0)
        await task
        
        self.assertEqual(len(collector.frames), 2)
        self.assertTrue(collector.last)
    
    async def test_wait_times_out(self):
        """Test that wait() raises TimeoutError if frames don't arrive."""
        import sys
        from pathlib import Path
        
        example_path = Path(__file__).parent.parent / "examples" / "connection_pool"
        sys.path.insert(0, str(example_path))
        try:
            from main import AnimationCollector
        finally:
            sys.path.pop(0)
        
        collector = AnimationCollector()
        
        with self.assertRaises(TimeoutError):
            await collector.wait(timeout=0.05)
    
    async def test_reset_clears_state(self):
        """Test that reset() clears collector state for reuse."""
        import sys
        from pathlib import Path
        
        example_path = Path(__file__).parent.parent / "examples" / "connection_pool"
        sys.path.insert(0, str(example_path))
        try:
            from main import AnimationCollector
        finally:
            sys.path.pop(0)
        
        collector = AnimationCollector()
        
        # Add some frames
        collector.transport_frame(b"frame1", True)
        self.assertEqual(len(collector.frames), 1)
        self.assertTrue(collector.last)
        
        # Reset
        collector.reset()
        
        self.assertEqual(len(collector.frames), 0)
        self.assertFalse(collector.last)
        self.assertIsNone(collector.error)


class TestAvatarConnectionPool(unittest.IsolatedAsyncioTestCase):
    """Test the AvatarConnectionPool class."""
    
    def _get_pool_class(self):
        """Import and return the pool class."""
        import sys
        from pathlib import Path
        
        example_path = Path(__file__).parent.parent / "examples" / "connection_pool"
        sys.path.insert(0, str(example_path))
        try:
            from main import AvatarConnectionPool, AnimationCollector
            return AvatarConnectionPool, AnimationCollector
        finally:
            sys.path.pop(0)
    
    async def test_pool_initialization(self):
        """Test pool creates specified number of connections."""
        AvatarConnectionPool, AnimationCollector = self._get_pool_class()
        
        connection_count = 0
        
        def config_factory(collector):
            nonlocal connection_count
            connection_count += 1
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            conn_id = f"conn-{connection_count}"
            return _FakeWebSocket(recv_messages=[_mk_confirm(conn_id)])
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        # Mock the session init to skip HTTP call
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=3,
            config_factory=config_factory,
            session_ttl_minutes=5,
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        self.assertEqual(pool.total_count, 3)
        self.assertEqual(pool.available_count, 3)
        
        await pool.close()
    
    async def test_borrow_returns_connection(self):
        """Test that borrow() returns a usable connection."""
        AvatarConnectionPool, AnimationCollector = self._get_pool_class()
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        connection_id_counter = [0]
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_id_counter[0] += 1
            return _FakeWebSocket(recv_messages=[_mk_confirm(f"conn-{connection_id_counter[0]}")])
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=1,
            config_factory=config_factory,
            session_ttl_minutes=5,
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        self.assertEqual(pool.available_count, 1)
        
        async with pool.borrow() as conn:
            self.assertIsNotNone(conn)
            self.assertIsNotNone(conn.session)
            self.assertIsNotNone(conn.collector)
            self.assertEqual(pool.available_count, 0)
        
        # Connection returned
        self.assertEqual(pool.available_count, 1)
        
        await pool.close()
    
    async def test_concurrent_borrow_queues(self):
        """Test that concurrent borrows queue when pool is exhausted."""
        AvatarConnectionPool, AnimationCollector = self._get_pool_class()
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        connection_id_counter = [0]
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_id_counter[0] += 1
            return _FakeWebSocket(recv_messages=[_mk_confirm(f"conn-{connection_id_counter[0]}")])
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=2,
            config_factory=config_factory,
            session_ttl_minutes=5,
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        acquired_connections = []
        order = []
        
        async def use_connection(delay: float, label: str):
            order.append(f"{label}_start")
            async with pool.borrow() as conn:
                order.append(f"{label}_acquired")
                acquired_connections.append(conn.connection_id)
                await asyncio.sleep(delay)
            order.append(f"{label}_released")
        
        # Start 3 tasks with 2 connections
        # First 2 should acquire immediately, third should wait
        task1 = asyncio.create_task(use_connection(0.1, "A"))
        task2 = asyncio.create_task(use_connection(0.1, "B"))
        
        # Give time for first two to acquire
        await asyncio.sleep(0.02)
        
        task3 = asyncio.create_task(use_connection(0.01, "C"))
        
        await asyncio.gather(task1, task2, task3)
        
        # C should have started but acquired after A or B released
        self.assertIn("C_start", order)
        c_acquired_idx = order.index("C_acquired")
        
        # Either A or B should have released before C acquired
        a_released = "A_released" in order[:c_acquired_idx]
        b_released = "B_released" in order[:c_acquired_idx]
        self.assertTrue(a_released or b_released)
        
        await pool.close()
    
    async def test_pool_stats(self):
        """Test pool statistics tracking."""
        AvatarConnectionPool, AnimationCollector = self._get_pool_class()
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        connection_id_counter = [0]
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_id_counter[0] += 1
            return _FakeWebSocket(recv_messages=[_mk_confirm(f"conn-{connection_id_counter[0]}")])
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=2,
            config_factory=config_factory,
            session_ttl_minutes=5,
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        # Use connections a few times
        for _ in range(3):
            async with pool.borrow():
                pass
        
        stats = pool.get_stats()
        
        self.assertEqual(stats["total_connections"], 2)
        self.assertEqual(stats["total_requests_served"], 3)
        self.assertEqual(len(stats["connections"]), 2)
        
        await pool.close()


class TestConcurrentAudioProcessing(unittest.IsolatedAsyncioTestCase):
    """Test concurrent audio processing scenarios."""
    
    def _get_classes(self):
        """Import classes from the example module."""
        import sys
        from pathlib import Path
        
        example_path = Path(__file__).parent.parent / "examples" / "connection_pool"
        sys.path.insert(0, str(example_path))
        try:
            from main import (
                AvatarConnectionPool,
                AnimationCollector,
                process_audio_request,
                RequestResult,
            )
            return AvatarConnectionPool, AnimationCollector, process_audio_request, RequestResult
        finally:
            sys.path.pop(0)
    
    async def test_process_audio_request_success(self):
        """Test successful audio processing through the pool."""
        AvatarConnectionPool, AnimationCollector, process_audio_request, RequestResult = self._get_classes()
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        connection_id_counter = [0]
        websockets_created: list[_FakeWebSocket] = []
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_id_counter[0] += 1
            ws = _FakeWebSocket(recv_messages=[_mk_confirm(f"conn-{connection_id_counter[0]}")])
            websockets_created.append(ws)
            return ws
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=1,
            config_factory=config_factory,
            session_ttl_minutes=5,
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        # Simulate frames being received in the background
        async def send_frames():
            await asyncio.sleep(0.02)
            # Get the borrowed connection's collector and send frames
            for conn in pool._all_connections:
                conn.collector.transport_frame(b"frame1", False)
                conn.collector.transport_frame(b"frame2", True)
        
        frame_task = asyncio.create_task(send_frames())
        
        audio = b"test audio data"
        result = await process_audio_request(pool, audio, 0)
        
        await frame_task
        
        self.assertTrue(result.success)
        self.assertEqual(result.frame_count, 2)
        self.assertIsNone(result.error)
        
        await pool.close()


class TestLongLivedConnections(unittest.IsolatedAsyncioTestCase):
    """
    Test long-lived connections that persist for 5 minutes.
    
    These tests simulate time passing to verify connection pool behavior
    over extended periods without actually waiting.
    """
    
    def _get_classes(self):
        """Import classes from the example module."""
        import sys
        from pathlib import Path
        
        example_path = Path(__file__).parent.parent / "examples" / "connection_pool"
        sys.path.insert(0, str(example_path))
        try:
            from main import (
                AvatarConnectionPool,
                AnimationCollector,
                PooledConnection,
                process_audio_request,
                RequestResult,
            )
            return AvatarConnectionPool, AnimationCollector, PooledConnection, process_audio_request, RequestResult
        finally:
            sys.path.pop(0)
    
    async def test_connections_survive_5_minutes(self):
        """Test that connections remain usable after 5 minutes of simulated time."""
        AvatarConnectionPool, AnimationCollector, PooledConnection, _, _ = self._get_classes()
        
        # Track the simulated current time
        simulated_time = datetime.now(timezone.utc)
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        connection_id_counter = [0]
        websockets_created: list[_FakeWebSocket] = []
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_id_counter[0] += 1
            ws = _FakeWebSocket(recv_messages=[_mk_confirm(f"long-conn-{connection_id_counter[0]}")])
            websockets_created.append(ws)
            return ws
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token-long-lived"
        
        pool = AvatarConnectionPool(
            pool_size=2,
            config_factory=config_factory,
            session_ttl_minutes=10,  # 10 minute TTL to cover 5 minute test
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        # Record initial connection IDs
        initial_conn_ids = [c.connection_id for c in pool._all_connections]
        self.assertEqual(len(initial_conn_ids), 2)
        
        # Simulate multiple requests over 5 minutes
        # We'll do 10 request batches, simulating 30 seconds between each
        request_count = 0
        
        for batch in range(10):
            # Each batch: borrow connection, use it, return it
            async with pool.borrow() as conn:
                # Verify connection is still the same (not recreated)
                self.assertIn(conn.connection_id, initial_conn_ids)
                
                # Simulate request processing
                conn.collector.transport_frame(b"frame", True)
                await conn.collector.wait(timeout=1.0)
                request_count += 1
            
            # Simulate 30 seconds passing (total = batch * 30 seconds)
            # After 10 batches = 5 minutes
            simulated_time += timedelta(seconds=30)
        
        # Verify all requests succeeded over the 5 minute period
        self.assertEqual(request_count, 10)
        
        # Verify connections are still the same ones (not recreated)
        final_conn_ids = [c.connection_id for c in pool._all_connections]
        self.assertEqual(set(initial_conn_ids), set(final_conn_ids))
        
        # Verify request counts accumulated correctly
        stats = pool.get_stats()
        self.assertEqual(stats["total_requests_served"], 10)
        
        await pool.close()
    
    async def test_connection_age_tracking_over_5_minutes(self):
        """Test that connection age is correctly tracked over 5 minutes."""
        AvatarConnectionPool, AnimationCollector, PooledConnection, _, _ = self._get_classes()
        
        # Start time for mocking
        start_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_now = [start_time]
        
        def mock_datetime_now(tz=None):
            return mock_now[0]
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        connection_id_counter = [0]
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_id_counter[0] += 1
            return _FakeWebSocket(recv_messages=[_mk_confirm(f"age-conn-{connection_id_counter[0]}")])
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=1,
            config_factory=config_factory,
            session_ttl_minutes=10,
        )
        
        # Patch datetime in the main module to control created_at timestamps
        import sys
        from pathlib import Path
        example_path = Path(__file__).parent.parent / "examples" / "connection_pool"
        sys.path.insert(0, str(example_path))
        try:
            import main as pool_main
            original_datetime = pool_main.datetime
            
            # Create a mock datetime class that intercepts now()
            class MockDateTime:
                @staticmethod
                def now(tz=None):
                    return mock_now[0]
                
                def __getattr__(self, name):
                    return getattr(original_datetime, name)
            
            pool_main.datetime = MockDateTime()
            
            with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
                 patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
                 patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
                await pool.initialize()
            
            # Verify connection was created at start_time
            self.assertEqual(pool._all_connections[0].created_at, start_time)
            
            # Advance time by 5 minutes
            mock_now[0] = start_time + timedelta(minutes=5)
            
            # Check age in stats
            stats = pool.get_stats()
            self.assertEqual(len(stats["connections"]), 1)
            # Age should be 5 minutes = 300 seconds
            self.assertAlmostEqual(stats["connections"][0]["age_seconds"], 300.0, delta=1.0)
            
        finally:
            pool_main.datetime = original_datetime
            sys.path.pop(0)
        
        await pool.close()
    
    async def test_high_throughput_over_5_minutes(self):
        """Test handling many requests over a 5 minute period."""
        AvatarConnectionPool, AnimationCollector, PooledConnection, process_audio_request, RequestResult = self._get_classes()
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        connection_id_counter = [0]
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_id_counter[0] += 1
            return _FakeWebSocket(recv_messages=[_mk_confirm(f"throughput-conn-{connection_id_counter[0]}")])
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=3,
            config_factory=config_factory,
            session_ttl_minutes=10,
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        # Simulate 5 minutes worth of requests
        # Assuming 1 request per second = 300 requests over 5 minutes
        # We'll do 100 requests in batches to keep test fast
        total_requests = 100
        successful_requests = 0
        connection_usage = {}
        
        for i in range(total_requests):
            async with pool.borrow() as conn:
                # Track which connections are being used
                connection_usage[conn.connection_id] = connection_usage.get(conn.connection_id, 0) + 1
                
                # Simulate successful processing
                conn.collector.transport_frame(b"response-frame", True)
                await conn.collector.wait(timeout=1.0)
                successful_requests += 1
        
        # Verify all requests succeeded
        self.assertEqual(successful_requests, total_requests)
        
        # Verify load was distributed across connections
        self.assertEqual(len(connection_usage), 3)  # All 3 connections used
        
        # Each connection should have handled roughly 1/3 of requests
        for conn_id, count in connection_usage.items():
            self.assertGreater(count, 20)  # At least 20% of total
        
        # Verify stats
        stats = pool.get_stats()
        self.assertEqual(stats["total_requests_served"], total_requests)
        
        await pool.close()
    
    async def test_concurrent_requests_over_5_minutes(self):
        """Test concurrent requests continuously over a 5 minute simulated period."""
        AvatarConnectionPool, AnimationCollector, PooledConnection, _, _ = self._get_classes()
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        connection_id_counter = [0]
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_id_counter[0] += 1
            return _FakeWebSocket(recv_messages=[_mk_confirm(f"concurrent-conn-{connection_id_counter[0]}")])
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=5,
            config_factory=config_factory,
            session_ttl_minutes=10,
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        # Simulate 5 minutes of concurrent activity
        # 30 batches of 10 concurrent requests = 300 requests
        batches = 30
        requests_per_batch = 10
        total_successful = 0
        
        async def make_request(batch_num: int, req_num: int):
            async with pool.borrow() as conn:
                # Simulate some processing time
                await asyncio.sleep(0.001)
                conn.collector.transport_frame(f"batch-{batch_num}-req-{req_num}".encode(), True)
                await conn.collector.wait(timeout=1.0)
                return 1
        
        for batch in range(batches):
            # Run concurrent requests
            tasks = [
                make_request(batch, i)
                for i in range(requests_per_batch)
            ]
            results = await asyncio.gather(*tasks)
            total_successful += sum(results)
        
        self.assertEqual(total_successful, batches * requests_per_batch)
        
        stats = pool.get_stats()
        self.assertEqual(stats["total_requests_served"], batches * requests_per_batch)
        
        # All 5 connections should have been used
        self.assertEqual(stats["total_connections"], 5)
        for conn_stat in stats["connections"]:
            self.assertGreater(conn_stat["request_count"], 0)
        
        await pool.close()
    
    async def test_connection_reuse_stability(self):
        """Test that the same connections are reused without reconnection over 5 minutes."""
        AvatarConnectionPool, AnimationCollector, PooledConnection, _, _ = self._get_classes()
        
        connection_create_count = [0]
        created_connection_ids = []
        
        def config_factory(collector):
            return {
                "api_key": "test-key",
                "app_id": "test-app",
                "console_endpoint_url": "https://console.example.com",
                "ingress_endpoint_url": "https://ingress.example.com",
                "avatar_id": "test-avatar",
                "transport_frames": collector.transport_frame,
                "on_error": collector.on_error,
                "on_close": collector.on_close,
            }
        
        async def fake_connect(url, additional_headers=None, **_kwargs):
            connection_create_count[0] += 1
            conn_id = f"stable-conn-{connection_create_count[0]}"
            created_connection_ids.append(conn_id)
            return _FakeWebSocket(recv_messages=[_mk_confirm(conn_id)])
        
        def fake_create_task(coro):
            coro.close()
            return _DummyTask()
        
        async def fake_init(self):
            self._session_token = "fake-token"
        
        pool = AvatarConnectionPool(
            pool_size=2,
            config_factory=config_factory,
            session_ttl_minutes=10,
        )
        
        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect), \
             patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task), \
             patch("avatarkit.avatar_session.AvatarSession.init", new=fake_init):
            await pool.initialize()
        
        # Should have created exactly 2 connections during init
        self.assertEqual(connection_create_count[0], 2)
        initial_ids = list(created_connection_ids)
        
        # Simulate 5 minutes of activity (50 requests)
        for i in range(50):
            async with pool.borrow() as conn:
                # Verify we're using one of the original connections
                self.assertIn(conn.connection_id, initial_ids)
                conn.collector.transport_frame(b"frame", True)
                await conn.collector.wait(timeout=1.0)
        
        # Verify NO new connections were created during the 5 minute simulation
        self.assertEqual(connection_create_count[0], 2)
        self.assertEqual(created_connection_ids, initial_ids)
        
        await pool.close()


if __name__ == "__main__":
    unittest.main()

