"""HTTP plumbing: sign-in, session renewal, timeouts and the site's parameter conventions.

Every request the client makes goes through `Session.request`, which

  * signs in on first use, and again — once — when the site says the session expired.
    Expiry shows up differently per endpoint: search.aspx serves the sign-in page with a
    200, the AJAX endpoints answer 403 or send an X-Redirect-XHR-Caller header. All of
    them are treated alike.
  * is safe to call from several threads: renewal is serialized, and a generation
    counter stops a burst of workers that all saw the same expiry from each signing in.
  * applies a timeout to every request (requests' default is none, which can hang a
    thread pool forever on a stalled server).
  * prefixes the identity/mode parameters each endpoint family expects (see `Params`).
"""
from __future__ import annotations

import threading
from typing import Any, Literal

import requests
from bs4 import BeautifulSoup, Tag

from ._parsers import is_login_page
from .constants import BROWSE_URL, LOGIN_URL
from .errors import LoginError, NetworkError, ServerError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
SEARCH_URL = BROWSE_URL + "search.aspx"

# The "mode" fieldset of the site's form. Popup endpoints are sent all of these, empty
# unless set — `host` matters most: the structured-hit endpoints only fill in their JSON
# for a host application (see HOST_FOR_HIT_DATA).
MODE_FIELDS = (
    "host", "SymbolPref", "forcedomain", "debug", "selectDatatypes", "multiSelect",
    "isGrouped", "isShortdesc", "isLongname", "TSDatatypesOnly", "dt", "nova", "l",
    "outboundChannel", "version", "pattern", "exportToExcel", "explorerMode", "dforic",
)

# hitdata.aspx and resolveLegacySelections.aspx return bare {} objects to the standalone
# site (which never reads them) and fill them in only for a host app. "unity" is the
# host that gets the most identifiers back (RIC, ISIN, SEDOL, local code, LDB, ...).
HOST_FOR_HIT_DATA = "unity"

ParamStyle = Literal["page", "mode", "progress", "none"]


class Session:
    def __init__(
        self,
        username: str,
        password: str,
        *,
        timeout: float | tuple[float, float] | None,
        pool_size: int,
        app_group: str = "DSAddin",
    ):
        self.username = username
        self._password = password
        self.timeout = timeout
        self.app_group = app_group
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT})
        adapter = requests.adapters.HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        self.http.mount("https://", adapter)
        self._lock = threading.Lock()
        self._generation = 0  # bumped on every successful sign-in
        self._logged_in = False

    def __repr__(self) -> str:
        return f"Session(username={self.username!r}, logged_in={self._logged_in})"

    # --- sign-in -----------------------------------------------------------------

    def login(self) -> None:
        with self._lock:
            self._login_locked()

    def ensure_login(self) -> None:
        if not self._logged_in:
            with self._lock:
                if not self._logged_in:
                    self._login_locked()

    def _renew(self, seen_generation: int) -> None:
        with self._lock:
            # another thread may already have renewed the session this request saw expire
            if self._generation == seen_generation:
                self._logged_in = False
                self._login_locked()

    def _login_locked(self) -> None:
        try:
            self._sign_in()
        except requests.RequestException as exc:
            if isinstance(exc, requests.HTTPError):
                raise LoginError(f"sign-in failed: {exc}") from exc
            raise NetworkError(f"sign-in failed: {exc}") from exc
        self._logged_in = True
        self._generation += 1

    def _sign_in(self) -> None:
        """The sign-in itself: the WebForms logon page, posted back with credentials."""
        params = {
            "AppGroup": self.app_group,
            "env": "PROD",
            "persisttoken": "true",
            "redirect": f"{SEARCH_URL}?AppGroup={self.app_group}",
            "srcapp": "Navigator",
        }
        form_page = self.http.get(LOGIN_URL, params=params, timeout=self.timeout)
        form_page.raise_for_status()
        soup = BeautifulSoup(form_page.text, "html.parser")

        def hidden(name: str) -> str:
            tag = soup.find("input", {"name": name})
            value = tag.get("value") if isinstance(tag, Tag) else None
            return value if isinstance(value, str) else ""

        response = self.http.post(
            LOGIN_URL,
            params=params,
            data={
                "__LASTFOCUS": "",
                "__VIEWSTATE": hidden("__VIEWSTATE"),
                "__VIEWSTATEGENERATOR": hidden("__VIEWSTATEGENERATOR"),
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "__EVENTVALIDATION": hidden("__EVENTVALIDATION"),
                "usernameTextBox": self.username,
                "passwordTextBox": self._password,
                "signonbutton": "Sign In",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        if "search.aspx" not in response.url:
            raise LoginError(
                f"sign-in as {self.username!r} failed (the site did not redirect to search.aspx; "
                f"check the credentials)"
            )

    # --- requests ----------------------------------------------------------------

    def params(self, style: ParamStyle, **mode: str) -> dict[str, str]:
        """The identity (and mode) parameters an endpoint family expects:

        page:     search.aspx, excelexport.aspx — dsid and AppGroup
        mode:     popups and AJAX lookups — dsid, AppGroup and every mode field
                  (empty unless given), the way the site serializes its form
        progress: endpoints that change user data — dsid, AppGroup, host
        none:     explorer endpoints, which rely on the session cookie alone
        """
        if style == "none":
            return {}
        base = {"dsid": self.username, "AppGroup": self.app_group}
        if style == "page":
            return {**base, **mode}
        if style == "progress":
            return {**base, "host": mode.get("host", "")}
        return {**base, **{name: mode.get(name, "") for name in MODE_FIELDS}}

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        style: ParamStyle,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        mode: dict[str, str] | None = None,
        xhr: bool = True,
    ) -> requests.Response:
        """Send a request to a /browse/ endpoint, signing in or renewing as needed."""
        self.ensure_login()
        query = {**self.params(style, **(mode or {})), **(params or {})}
        headers = {"Referer": SEARCH_URL}
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"

        for attempt in (1, 2):
            generation = self._generation
            try:
                response = self.http.request(
                    method, BROWSE_URL + endpoint, params=query, data=data, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                raise NetworkError(f"{method} {endpoint}: {exc}") from exc
            if not _expired(response):
                break
            if attempt == 1:
                self._renew(generation)
        else:
            if response.status_code in (401, 403) and not _shows_sign_in(response):
                # still refused with a fresh session: a real permission error
                raise ServerError(
                    f"{method} {endpoint} was refused (HTTP {response.status_code})",
                    status_code=response.status_code,
                    detail=response.headers.get("X-Error-Detail"),
                )
            raise LoginError(f"{endpoint}: the session expired and could not be renewed")

        if response.status_code >= 400:
            raise ServerError(
                f"{method} {endpoint} failed with HTTP {response.status_code}",
                status_code=response.status_code,
                detail=response.headers.get("X-Error-Detail"),
            )
        return response

    def json(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        response = self.request(method, endpoint, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ServerError(
                f"{endpoint} returned {response.headers.get('Content-Type')!r}, not JSON",
                status_code=response.status_code,
                detail=response.headers.get("X-Error-Detail"),
            ) from exc

    def close(self) -> None:
        self.http.close()


def _shows_sign_in(response: requests.Response) -> bool:
    """The response is (or redirects to) the sign-in page."""
    if "X-Redirect-XHR-Caller" in response.headers or "DSLogon.aspx" in response.url:
        return True
    content_type = response.headers.get("Content-Type", "")
    return "html" in content_type and is_login_page(response.text)


def _expired(response: requests.Response) -> bool:
    """Every way an endpoint says the session has gone: the sign-in page (search.aspx),
    a 401/403 (the AJAX endpoints), or a redirect header for XHR callers."""
    return response.status_code in (401, 403) or _shows_sign_in(response)
