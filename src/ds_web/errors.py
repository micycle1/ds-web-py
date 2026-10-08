"""Exception and warning types raised by ds_web."""
from __future__ import annotations


class DatastreamWebError(Exception):
    """Base class for every error this library raises."""


class LoginError(DatastreamWebError):
    """Sign-in failed, or an expired session could not be renewed."""


class NetworkError(DatastreamWebError):
    """The site couldn't be reached, or didn't answer in time."""


class ServerError(DatastreamWebError):
    """The site answered with an error status.

    `detail` contains the site's X-Error-Detail header, if present (for example
    "Unexpected logged as 145989980"). The site's support desk asks for this reference.
    """

    def __init__(self, message: str, status_code: int | None = None, detail: str | None = None):
        super().__init__(message if not detail else f"{message} ({detail})")
        self.status_code = status_code
        self.detail = detail


class ResultSetTooLargeError(DatastreamWebError):
    """A complete result set was asked for, but the site can't deliver one that big.

    Every bulk endpoint the site has stops at a fixed row count, and paging past it is
    not a workaround: pages are not a stable partition of the results (tied hits reorder
    between requests), so a page walk both repeats and drops rows. Narrow the query
    with filters, or pass an explicit `limit` to take the top of the ranking.
    """

    def __init__(self, total_hits: int, cap: int):
        super().__init__(
            f"the query matches {total_hits:,} series but the site returns at most "
            f"{cap:,} in one result set; narrow it with filters or pass limit="
        )
        self.total_hits = total_hits
        self.cap = cap


class ParseError(DatastreamWebError):
    """A response didn't have the shape this library expects — usually a site change."""


class TruncatedResultsWarning(UserWarning):
    """Fewer rows came back than the query has, because of a site-side cap."""
