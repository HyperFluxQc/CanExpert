"""Waiting until a moment to a fraction of a millisecond, and cyclic frames sent on time by a thread of their own
(timing.py, cyclic.CyclicSender). The bounds are loose: a busy CI machine is not a quiet bench PC."""
import sys
import threading
import time
import unittest

from canexpert.cyclic import CycleStats, CyclicSender
from canexpert.timing import SWITCH_INTERVAL, Waiter, precise_switching, raise_priority


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class WaiterTest(unittest.TestCase):
    def setUp(self):
        self.waiter = Waiter()
        self.addCleanup(self.waiter.close)

    def test_it_wakes_at_the_moment_never_before(self):
        lateness = []
        for _ in range(20):
            deadline = time.perf_counter() + 0.005
            self.assertFalse(self.waiter.wait_until(deadline))
            lateness.append(time.perf_counter() - deadline)
        self.assertGreaterEqual(min(lateness), 0, "never early")
        self.assertLess(sorted(lateness)[len(lateness) // 2], 0.003, "a millisecond or so late at most, mostly")

    def test_another_thread_can_wake_it(self):
        timer = threading.Timer(0.05, self.waiter.wake)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.perf_counter()
        self.assertTrue(self.waiter.wait_until(started + 10), "woken")
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertFalse(self.waiter.wait_until(time.perf_counter() + 0.01), "and it waits again after")

    def test_a_moment_passed_returns_at_once(self):
        started = time.perf_counter()
        self.assertFalse(self.waiter.wait_until(started - 1))
        self.assertLess(time.perf_counter() - started, 0.01)

    @unittest.skipUnless(sys.platform == "win32", "Windows' high-resolution waitable timer")
    def test_a_high_resolution_timer_on_windows(self):
        self.assertTrue(self.waiter.high_resolution, "Windows 10 1803 or later")

    def test_the_interpreter_changes_hands_quickly(self):
        before = sys.getswitchinterval()
        self.addCleanup(sys.setswitchinterval, before)
        precise_switching()
        self.assertLessEqual(sys.getswitchinterval(), SWITCH_INTERVAL)
        raise_priority()                                   # harmless for the test's own thread


class CycleStatsTest(unittest.TestCase):
    def test_the_measured_cycle(self):
        stats = CycleStats()
        self.assertEqual(stats.text(), "")
        for moment in (1.000, 1.010, 1.021, 1.030):
            stats.add(moment)
        self.assertEqual(stats.text(), "10.0 (9.0-11.0)")
        self.assertEqual((stats.sent, stats.take_new(), stats.take_new()), (4, 4, 0))


class CyclicSenderTest(unittest.TestCase):
    def sender(self, send, failed=None):
        sender = CyclicSender(send, failed)
        self.addCleanup(sender.close)
        return sender

    def test_frames_go_at_their_cycle(self):
        times = []
        sender = self.sender(lambda can_id, data, extended: times.append(time.perf_counter()))
        sender.set("a", 0.020, lambda: (0x100, b"\x01", False))
        time.sleep(0.5)
        sender.remove("a")
        self.assertTrue(18 <= len(times) <= 28, f"{len(times)} frames in 0.5 s at 20 ms")
        gaps = [later - earlier for earlier, later in zip(times[1:], times[2:])]
        self.assertAlmostEqual(sum(gaps) / len(gaps), 0.020, delta=0.004)
        new, measured = sender.take("a")
        self.assertEqual((new, measured), (0, ""), "a key removed has nothing left to report")

    def test_each_send_asks_for_the_frame_as_it_is(self):
        sent, count = [], iter(range(1000))
        sender = self.sender(lambda can_id, data, extended: sent.append((can_id, data, extended)))
        sender.set("counter", 0.005, lambda: (0x18DA10F1, bytes([next(count)]), True))
        self.assertTrue(wait_for(lambda: len(sent) >= 5))
        sender.close()
        self.assertEqual([frame[1][0] for frame in sent[:5]], [0, 1, 2, 3, 4])
        self.assertEqual({(frame[0], frame[2]) for frame in sent}, {(0x18DA10F1, True)})

    def test_none_skips_a_send(self):
        sent, asked = [], []
        sender = self.sender(lambda can_id, data, extended: sent.append(data))
        sender.set("odd", 0.005, lambda: (asked.append(1), (0x1, bytes([len(asked)]), False) if len(asked) % 2 else None)[1])
        self.assertTrue(wait_for(lambda: len(asked) >= 6))
        sender.close()
        self.assertTrue(all(data[0] % 2 for data in sent), "only every other ask sent")

    def test_a_send_that_fails_stops_that_key_only(self):
        sent, failures = [], []

        def send(can_id, data, extended):
            if can_id == 0x666:
                raise RuntimeError("Connect before sending CAN messages")
            sent.append(can_id)
        sender = self.sender(send, lambda key, error: failures.append((key, str(error))))
        sender.set("bad", 0.005, lambda: (0x666, b"", False))
        sender.set("good", 0.005, lambda: (0x100, b"", False))
        self.assertTrue(wait_for(lambda: failures and len(sent) >= 5))
        self.assertEqual(failures, [("bad", "Connect before sending CAN messages")])
        self.assertEqual(sorted(sender.keys()), ["good"])

    def test_a_late_send_is_not_caught_up_in_a_burst(self):
        times, stalled = [], []

        def send(can_id, data, extended):
            times.append(time.perf_counter())
            if len(times) == 3 and not stalled:
                stalled.append(True)
                time.sleep(0.15)                            # the adapter hung for fifteen cycles
        sender = self.sender(send)
        sender.set("a", 0.010, lambda: (0x100, b"", False))
        self.assertTrue(wait_for(lambda: len(times) >= 12))
        sender.close()
        bursts = [later - earlier for earlier, later in zip(times[3:], times[4:]) if later - earlier < 0.004]
        self.assertLessEqual(len(bursts), 1, "missed sends are not all made at once afterwards")

    def test_keys_come_and_go(self):
        sent = []
        sender = self.sender(lambda can_id, data, extended: sent.append(can_id))
        sender.set(1, 0.01, lambda: (1, b"", False))
        sender.set(2, 0.01, lambda: (2, b"", False))
        self.assertTrue(wait_for(lambda: {1, 2} <= set(sent)))
        sender.set(1, 0.05, lambda: (1, b"", False))        # a new cycle, the same key: it keeps its place
        self.assertEqual(sorted(sender.keys()), [1, 2])
        sender.clear()
        self.assertEqual(sender.keys(), [])
        sender.close()
        sender.set(3, 0.01, lambda: (3, b"", False))
        self.assertEqual(sender.keys(), [], "closed for good")


if __name__ == "__main__":
    unittest.main()
