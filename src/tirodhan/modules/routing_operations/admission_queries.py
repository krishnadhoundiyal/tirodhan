"""Bound identity sets without asyncpg's expanding-parameter population limit."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import any_, bindparam
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement


def in_ids(
    column: ColumnElement[Any] | InstrumentedAttribute[Any], identities: Iterable[Any]
) -> ColumnElement[bool]:
    values = tuple(identities)
    if len(values) <= 512:
        return column.in_(values)
    return column == any_(bindparam(None, list(values), type_=ARRAY(column.type)))
