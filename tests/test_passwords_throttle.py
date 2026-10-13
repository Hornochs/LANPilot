import pytest

from lanpilot.auth import passwords
from lanpilot.auth.throttle import LoginThrottle


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
