"""User-input payloads for turns (text, images, local images)."""

from __future__ import annotations

from typing import Any, Literal, Sequence, Union

from pydantic import BaseModel


class Text(BaseModel):
    type: Literal["text"] = "text"
    text: str


class Image(BaseModel):
    """An image by URL (``https://...`` or ``data:image/...;base64,...``)."""

    type: Literal["image"] = "image"
    url: str


class LocalImage(BaseModel):
    type: Literal["localImage"] = "localImage"
    path: str


InputItem = Union[Text, Image, LocalImage]
TurnInput = str | InputItem | dict[str, Any] | Sequence[InputItem | dict[str, Any] | str]


def normalize_input(input: TurnInput) -> list[dict[str, Any]]:
    """Coerce the accepted input shapes into the wire ``input`` array."""
    if isinstance(input, str):
        return [{"type": "text", "text": input}]
    if isinstance(input, BaseModel):
        return [input.model_dump()]
    if isinstance(input, dict):
        return [input]
    items: list[dict[str, Any]] = []
    for item in input:
        items.extend(normalize_input(item))
    return items
