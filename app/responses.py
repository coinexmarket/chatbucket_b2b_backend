"""The response envelope shared by the blog endpoints.

The frontend's ``useFetch``/``sendGetRequest`` helpers read this exact shape::

    {
      "status": bool,
      "status_code": int,
      "message": str,
      "data": <payload>,
      "response_code": int
    }

``useFetch`` also supports a ``dataKey`` fallback: when ``data`` is absent it
reads ``body[dataKey]`` (e.g. ``categories`` or ``blogs``). We therefore mirror
the payload under that key too, so both access paths work.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def success(
    data: Any,
    *,
    message: str = "Success",
    status_code: int = 200,
    data_key: str | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "status": True,
        "status_code": status_code,
        "message": message,
        "data": data,
        "response_code": status_code,
    }
    if data_key:
        body[data_key] = data
    return JSONResponse(status_code=status_code, content=body)


def failure(
    message: str = "Something went wrong",
    *,
    status_code: int = 500,
) -> JSONResponse:
    body = {
        "status": False,
        "status_code": status_code,
        "message": message,
        "data": None,
        "response_code": status_code,
    }
    return JSONResponse(status_code=status_code, content=body)
