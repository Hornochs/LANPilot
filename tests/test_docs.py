"""The code in the adapter guide is real: these tests run it."""

import re
from pathlib import Path

import pytest

from lanpilot.core.adapters import (
    AuthenticationError,
    CommandRejectedError,
    MapRef,
    ServerUnreachableError,
    validate_adapter_class,
)
from lanpilot.core.adapters.schema import describe_fields

GUIDE = Path(__file__).parent.parent / "docs" / "adapters.md"


def example(name: str) -> str:
    match = re.search(rf"<!-- example: {name} -->\n```python\n(.*?)\n```", GUIDE.read_text(), re.S)
    assert match, f"example {name!r} not found in {GUIDE}"
    return match.group(1)


@pytest.fixture
def guide() -> dict[str, object]:
    namespace: dict[str, object] = {}
    exec(compile(example("adapter"), "docs/adapters.md#adapter", "exec"), namespace)  # noqa: S102
    exec(compile(example("test"), "docs/adapters.md#test", "exec"), namespace)  # noqa: S102
    return namespace


async def test_example_adapter_passes_its_own_contract_test(guide: dict[str, object]) -> None:
    validate_adapter_class(guide["LineworldAdapter"])  # type: ignore[arg-type]

    await guide["test_lineworld_fulfils_the_contract"]()  # type: ignore[operator]


def test_example_connection_becomes_a_form(guide: dict[str, object]) -> None:
    fields = {field.name: field for field in describe_fields(guide["LineworldConnection"])}  # type: ignore[arg-type]

    assert list(fields) == ["host", "port", "password"]
    assert fields["password"].kind == "password"
    assert fields["port"].default == 4000


async def test_example_adapter_reports_errors_as_the_guide_says(guide: dict[str, object]) -> None:
    from pydantic import SecretStr

    adapter_class = guide["LineworldAdapter"]
    connection_class = guide["LineworldConnection"]
    server = await guide["lineworld_simulator"]("s3cret")  # type: ignore[operator]
    port = server.sockets[0].getsockname()[1]

    def adapter(password: str, at: int = port) -> object:
        return adapter_class(  # type: ignore[operator]
            connection_class(host="127.0.0.1", port=at, password=SecretStr(password))  # type: ignore[operator]
        )

    try:
        with pytest.raises(AuthenticationError) as wrong:
            await adapter("wrong-password").list_maps()  # type: ignore[attr-defined]
        assert "wrong-password" not in str(wrong.value)

        with pytest.raises(CommandRejectedError):
            await adapter("s3cret").change_map(MapRef("nowhere"))  # type: ignore[attr-defined]
        with pytest.raises(CommandRejectedError, match="cannot be sent"):
            await adapter("s3cret").change_map(MapRef("meadow\nMAP s3cret canyon"))  # type: ignore[attr-defined]
        assert (await adapter("s3cret").query()).map == MapRef("meadow")  # type: ignore[attr-defined]
    finally:
        server.close()
        await server.wait_closed()

    with pytest.raises(ServerUnreachableError):
        await adapter("s3cret").query()  # type: ignore[attr-defined]
