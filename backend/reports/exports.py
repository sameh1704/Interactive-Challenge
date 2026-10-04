"""CSV export.

Deliberately small: the standard library's :mod:`csv` module and a
``HttpResponse``. No reporting engine, no template-driven export, no spreadsheet
dependency - a school that wants the numbers in Excel opens a CSV.

One helper builds the file so that every export shares the same conventions: a
header row, UTF-8 with a byte-order mark so Excel on Windows reads accented
classroom names correctly, and a filename derived from the thing being exported
rather than from the URL.
"""

from __future__ import annotations

import csv
import io

from django.http import HttpResponse

# Excel assumes the system codepage unless a byte-order mark says otherwise, which
# turns "Sciences Physiques" into mojibake on a school laptop. The BOM costs three
# bytes and prevents that.
UTF8_BOM = "﻿"


def csv_response(filename: str, header: list[str], rows) -> HttpResponse:
    """Build a CSV download.

    ``rows`` may be any iterable of sequences. Values are converted with
    :func:`str`, and ``None`` becomes an empty cell rather than the word "None",
    which would otherwise appear in a column where a blank is the honest answer.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else cell for cell in row])

    response = HttpResponse(
        UTF8_BOM + buffer.getvalue(),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def slugify_filename(*parts: str) -> str:
    """A filesystem-safe name from arbitrary text.

    Falls back to "export" so a name made entirely of punctuation still produces a
    usable filename rather than an empty one.
    """
    pieces = []
    for part in parts:
        text = "".join(
            character if character.isalnum() else "-" for character in (part or "")
        )
        text = "-".join(piece for piece in text.split("-") if piece)
        if text:
            pieces.append(text)
    return "-".join(pieces) or "export"


__all__ = ["csv_response", "slugify_filename"]
