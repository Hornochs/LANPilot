import pytest

from lanpilot.auth import passwords
from lanpilot.auth.throttle import LoginThrottle, client_key


def test_hash_and_verify() -> None:
    stored = passwords.hash_password("correct horse battery")

    assert stored.startswith("$argon2id$")
    assert "correct horse" not in stored
    assert passwords.verify_password(stored, "correct horse battery")
    assert not passwords.verify_password(stored, "wrong horse battery")


def test_verify_handles_garbage_hashes() -> None:
    assert not passwords.verify_password("not-a-hash", "whatever")
    assert not passwords.verify_password("", "whatever")


def test_overlong_password_is_rejected_without_hashing() -> None:
    stored = passwords.hash_password("x" * passwords.MAX_LENGTH)

    assert passwords.verify_password(stored, "x" * passwords.MAX_LENGTH)
    assert not passwords.verify_password(stored, "x" * (passwords.MAX_LENGTH + 1))


@pytest.mark.parametrize(
    ("password", "username", "fragment"),
    [
        ("short", None, "at least"),
        ("x" * 257, None, "at most"),
        ("AdminAccount", "adminaccount", "differ from the user name"),
    ],
)
def test_policy_rejections(password: str, username: str | None, fragment: str) -> None:
    with pytest.raises(passwords.PasswordPolicyError, match=fragment):
        passwords.check_policy(password, username)


def test_policy_accepts_a_long_passphrase() -> None:
    passwords.check_policy("correct horse battery", "admin")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pair_is_blocked_after_too_many_failures() -> None:
    clock = Clock()
    throttle = LoginThrottle(max_failures=3, window_seconds=60, clock=clock)

    for _ in range(3):
        assert throttle.retry_after("admin", "10.0.0.1") == 0
        throttle.record_failure("admin", "10.0.0.1")

    assert throttle.retry_after("admin", "10.0.0.1") == 61
    # Another client is not locked out of the same account, nor this client out of another.
    assert throttle.retry_after("admin", "10.0.0.2") == 0
    assert throttle.retry_after("other", "10.0.0.1") == 0


def test_block_ends_when_the_window_has_passed() -> None:
    clock = Clock()
    throttle = LoginThrottle(max_failures=2, window_seconds=60, clock=clock)
    throttle.record_failure("admin", "10.0.0.1")
    clock.now += 30
    throttle.record_failure("admin", "10.0.0.1")
    assert throttle.retry_after("admin", "10.0.0.1") == 31

    clock.now += 31

    assert throttle.retry_after("admin", "10.0.0.1") == 0


def test_success_clears_the_pair_counter() -> None:
    throttle = LoginThrottle(max_failures=2, clock=Clock())
    throttle.record_failure("admin", "10.0.0.1")
    throttle.record_success("admin", "10.0.0.1")
    throttle.record_failure("admin", "10.0.0.1")

    assert throttle.retry_after("admin", "10.0.0.1") == 0


def test_address_is_blocked_across_user_names() -> None:
    throttle = LoginThrottle(max_failures=5, max_failures_per_address=4, clock=Clock())
    for index in range(4):
        throttle.record_failure(f"user{index}", "10.0.0.1")

    assert throttle.retry_after("someone-new", "10.0.0.1") > 0
    assert throttle.retry_after("someone-new", "10.0.0.2") == 0


def test_memory_stays_bounded() -> None:
    clock = Clock()
    throttle = LoginThrottle(max_entries=50, window_seconds=60, clock=clock)
    for index in range(500):
        throttle.record_failure(f"user{index}", f"10.0.{index // 250}.{index % 250}")

    assert len(throttle._failures) <= 52


def test_attempts_are_counted_before_the_password_is_checked() -> None:
    clock = Clock()
    throttle = LoginThrottle(max_failures=3, max_failures_per_address=10, clock=clock)
    # Parallel requests: each one is counted at once, before any of them is verified.
    outcomes = [throttle.attempt("admin", "10.0.0.1") for _ in range(5)]
    assert all(wait == 0 for wait, _ in outcomes[:3])
    assert all(wait > 0 and mark is None for wait, mark in outcomes[3:])


def test_a_right_password_takes_back_its_attempt() -> None:
    clock = Clock()
    throttle = LoginThrottle(max_failures=3, max_failures_per_address=2, clock=clock)
    for _ in range(5):
        _, mark = throttle.attempt("admin", "10.0.0.1")
        clock.now += 1
        throttle.record_success("admin", "10.0.0.1", mark)
    # Five successful sign-ins in a row never reach the per-address limit.
    assert throttle.retry_after("admin", "10.0.0.1") == 0
    assert throttle.retry_after("someone", "10.0.0.1") == 0


def test_refusals_are_reported_once_per_window() -> None:
    clock = Clock()
    throttle = LoginThrottle(window_seconds=60, clock=clock)
    assert throttle.report_refusal("10.0.0.1")
    assert not throttle.report_refusal("10.0.0.1")
    assert throttle.report_refusal("10.0.0.2")
    clock.now += 61
    assert throttle.report_refusal("10.0.0.1")


async def test_hashing_runs_only_a_few_at_a_time() -> None:
    import asyncio
    import threading
    import time

    running = 0
    peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1

    await asyncio.gather(*(passwords.in_worker(work) for _ in range(20)))
    assert peak <= passwords.MAX_PARALLEL


def test_ipv6_clients_are_throttled_by_their_64() -> None:
    throttle = LoginThrottle(max_failures=3, max_failures_per_address=100, clock=Clock())
    for index in range(3):  # a new address from the same /64 for every attempt
        throttle.record_failure("ada", f"2001:db8:1:2::{index + 1:x}")

    assert throttle.retry_after("ada", "2001:db8:1:2:ffff::9") > 0
    assert throttle.retry_after("ada", "2001:db8:1:3::1") == 0  # another /64
    assert client_key("::ffff:10.0.0.1") == client_key("10.0.0.1") == "10.0.0.1"


def test_many_clients_guessing_one_account_slow_down_but_do_not_lock_it() -> None:
    clock = Clock()
    throttle = LoginThrottle(
        max_failures=5, max_failures_per_account=10, account_cooldown_seconds=30, clock=clock
    )
    for index in range(10):
        throttle.record_failure("ada", f"10.0.1.{index}")

    # Every client now waits a short cooldown after the last failure, not the whole window.
    wait = throttle.retry_after("ada", "10.0.2.1")
    assert 0 < wait <= 31
    assert throttle.retry_after("grace", "10.0.2.1") == 0  # other accounts are not affected
    clock.now += 31
    assert throttle.retry_after("ada", "10.0.2.1") == 0
    throttle.record_failure("ada", "10.0.1.99")
    assert throttle.retry_after("ada", "10.0.2.1") > 0  # and again after each failure
