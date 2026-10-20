"""Turn an adapter's connection schema into form fields and stored values.

The "add server" form is generated from the schema, so adapters need no user interface
code. Secret fields are separated from the rest: they are stored encrypted and never
rendered back into a page.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

import annotated_types
from pydantic import BaseModel, SecretStr, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from lanpilot import i18n
from lanpilot.i18n import M

FieldKind = Literal["text", "number", "checkbox", "select", "password"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: FieldKind
    required: bool
    help: str = ""
    default: object = None
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    max_length: int | None = None

    @property
    def secret(self) -> bool:
        return self.kind == "password"


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    if get_origin(annotation) in (Union, types.UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(members) == 1:
            return members[0], True
    return annotation, False


def _spec(name: str, info: FieldInfo) -> FieldSpec:
    annotation, optional = _unwrap_optional(info.annotation)
    default = None if info.default is PydanticUndefined else info.default
    common: dict[str, Any] = {
        "name": name,
        "label": info.title or name.replace("_", " ").capitalize(),
        "help": info.description or "",
        "required": info.is_required() and not optional,
    }

    if annotation is SecretStr:
        # A default for a secret would end up in the page source; never pass it on.
        return FieldSpec(kind="password", **common)
    if annotation is bool:
        return FieldSpec(kind="checkbox", default=bool(default), **{**common, "required": False})
    if annotation is int:
        minimum = maximum = None
        for item in info.metadata:
            if isinstance(item, annotated_types.Ge):
                minimum = int(item.ge)  # type: ignore[call-overload]
            elif isinstance(item, annotated_types.Le):
                maximum = int(item.le)  # type: ignore[call-overload]
        return FieldSpec(kind="number", default=default, minimum=minimum, maximum=maximum, **common)
    if get_origin(annotation) is Literal:
        choices = get_args(annotation)
        if not all(isinstance(choice, str) for choice in choices):
            raise TypeError(f"field {name!r}: Literal choices must be strings")
        return FieldSpec(kind="select", default=default, choices=tuple(choices), **common)
    if annotation is str:
        max_length = next(
            (item.max_length for item in info.metadata if isinstance(item, annotated_types.MaxLen)),
            None,
        )
        return FieldSpec(kind="text", default=default, max_length=max_length, **common)
    raise TypeError(
        f"field {name!r} has unsupported type {annotation!r}; "
        "use str, int, bool, Literal[...] or SecretStr"
    )


def describe_fields(schema: type[BaseModel]) -> list[FieldSpec]:
    """Form fields for a connection schema, in declaration order."""
    return [_spec(name, info) for name, info in schema.model_fields.items()]


def split_values(connection: BaseModel) -> tuple[dict[str, Any], dict[str, str]]:
    """Separate a validated connection into public values and secret values."""
    public: dict[str, Any] = {}
    secret: dict[str, str] = {}
    for name in type(connection).model_fields:
        value = getattr(connection, name)
        if not _is_secret(type(connection), name):
            public[name] = value
        elif value is not None:
            secret[name] = value.get_secret_value()
    return public, secret


def _is_secret(schema: type[BaseModel], name: str) -> bool:
    annotation, _ = _unwrap_optional(schema.model_fields[name].annotation)
    return annotation is SecretStr


def secret_field_names(schema: type[BaseModel]) -> list[str]:
    return [name for name in schema.model_fields if _is_secret(schema, name)]


def _message_of_pydantic(item: Mapping[str, Any]) -> str:
    """One validation error as a message that can be translated; the English texts are
    pydantic's own. Errors that are not known here are shown as pydantic words them."""
    kind = item["type"]
    context = item.get("ctx") or {}
    if kind == "value_error" and "error" in context:
        return i18n.message_of(context["error"])
    if kind == "missing":
        return M("Field required")
    if kind == "greater_than_equal" and "ge" in context:
        return M("Input should be greater than or equal to {limit}", limit=context["ge"])
    if kind == "less_than_equal" and "le" in context:
        return M("Input should be less than or equal to {limit}", limit=context["le"])
    if kind == "string_too_long" and context.get("max_length", 1) != 1:
        return M("String should have at most {limit} characters", limit=context["max_length"])
    if kind == "int_parsing":
        return M("Input should be a valid integer, unable to parse string as an integer")
    if kind == "literal_error" and "expected" in context:
        return M("Input should be {expected}", expected=context["expected"])
    return str(item["msg"]).removeprefix("Value error, ")


def describe_errors(error: ValidationError) -> dict[str, str]:
    """Field name -> message, without echoing the input (it may be a password)."""
    messages: dict[str, str] = {}
    for item in error.errors(include_input=False, include_url=False):
        name = str(item["loc"][0]) if item["loc"] else ""
        messages.setdefault(name, _message_of_pydantic(item))
    return messages
