from __future__ import annotations

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel


class PageParams:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
