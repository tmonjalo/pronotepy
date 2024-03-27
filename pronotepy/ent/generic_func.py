from logging import getLogger, DEBUG
import typing

import requests
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin, urlparse, urlunparse

from ..exceptions import *

log = getLogger(__name__)
log.setLevel(DEBUG)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:73.0) Gecko/20100101 Firefox/73.0"
}


class AuthSession(requests.Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hooks = {"response": lambda r, *args, **kwargs: r.raise_for_status()}

    def request(self, *args, **kwargs):
        kwargs.setdefault("allow_redirects", True)
        self.response = super().request(*args, **kwargs)
        self.response.html = BeautifulSoup(self.response.text, "html.parser")
        return self.response

    def find_form_field(self, key, value, attr):
        tag = None if value == "submit" else "input"
        field = self.response.html.find(tag, {key: value})
        return field.get(attr, None) if field else None

    @property
    def form_field_user(self):
        return self.find_form_field("type", "text", "name")

    @property
    def form_field_password(self):
        return self.find_form_field("type", "password", "name")

    @property
    def form_field_submit(self):
        return self.find_form_field("type", "submit", "name")

    @property
    def response_dump(self):
        if not self.response:
            return None
        r = self.response
        return f"status: {r.status_code}\nlocation: {r.url}\ncontent: \n{r.text}"

    @property
    def saml(self):
        for saml_type in ("SAMLRequest", "SAMLResponse"):
            saml = self.find_form_field("name", saml_type, "value")
            if saml:
                return {saml_type: saml}
        return None

    def post_form(self, payload=None):
        if payload is None:
            payload = {}
        for field in self.response.html.find_all(["input", "button"]):
            name = field.get("name", None)
            if name and name not in payload:
                payload[name] = field.get("value", "")
        url = self.response.html.find("form")["action"]
        if url.startswith("/"):
            url = urljoin(self.response.url, url)
        return self.post(url, data=payload)


def generic_auth(
    username: str, password: str, pronote_url: str = "", **opts
) -> requests.cookies.RequestsCookieJar:
    with AuthSession() as session:
        session.get(pronote_url)
        has_saml = True if session.saml else False
        if has_saml:
            # received SAML request from the service provider (SP)
            # send SAML request to the identity provider (IdP)
            session.post_form()
        if session.form_field_password:
            # received login form
            field_user = session.form_field_user
            field_pass = session.form_field_password
            if not field_user or not field_pass:
                raise ENTLoginError("Invalid login form")
            payload = {field_user: username, field_pass: password}
        elif has_saml:
            raise ENTLoginError("SAML connection failure")
        else:
            # no login, no cookies
            return None
        # send credentials (to the IdP in SAML case)
        session.post_form(payload)
        if has_saml:
            if not session.saml:
                raise ENTLoginError("SAML login failure")
            # received SAML response from the identity provider (IdP)
            # send SAML response to the service provider (SP)
            session.post_form()
        return session.cookies


def _sso_redirect(
    session: requests.Session,
    response: requests.Response,
    saml_type: str,  # SAMLRequest or SAMLResponse
    request_url: str = "",
    request_payload: dict = {},
) -> typing.Optional[requests.Response]:
    soup = BeautifulSoup(response.text, "html.parser")

    saml = soup.find("input", {"name": saml_type})
    if not saml and response.status_code == 200 and request_url != response.url:
        # manual redirect
        response = session.post(response.url, headers=HEADERS, data=request_payload)
        soup = BeautifulSoup(response.text, "html.parser")
        saml = soup.find("input", {"name": saml_type})

    if not saml:
        return None

    assert isinstance(saml, Tag)

    payload = {saml_type: saml.get("value")}

    relay_state = soup.find("input", {"name": "RelayState"})
    assert isinstance(relay_state, Tag)
    if relay_state:
        payload["RelayState"] = relay_state["value"]

    url: str = soup.find("form")["action"]  # type: ignore

    return session.post(url, headers=HEADERS, data=payload)


@typing.no_type_check
def _educonnect(
    session: requests.Session,
    username: str,
    password: str,
    url: str,
    exceptions: bool = True,
    **opts: str,
) -> typing.Optional[requests.Response]:
    """
    Generic function for EduConnect

    Parameters
    ----------
    username : str
        username
    password : str
        password
    url: str
        url of the ent login page

    Returns
    -------
    response: requests.Response
        the response returned by EduConnect login
    """
    if not url:
        raise ENTLoginError("Missing url attribute")

    log.debug(f"[EduConnect {url}] Logging in with {username}")

    payload = {"j_username": username, "j_password": password, "_eventId_proceed": ""}
    response = session.post(url, headers=HEADERS, data=payload)
    response = _sso_redirect(session, response, "SAMLResponse", url, payload)
    if not response:
        if exceptions:
            raise ENTLoginError(
                "Fail to connect with EduConnect : probably wrong login information"
            )
        else:
            return None
    return response


@typing.no_type_check
def _cas_edu(
    username: str,
    password: str,
    url: str = "",
    redirect_form: bool = True,
    **opts: str,
) -> requests.cookies.RequestsCookieJar:
    """
    Generic function for CAS with Educonnect

    Parameters
    ----------
    username : str
        username
    password : str
        password
    url: str
        url of the ent login page
    redirect_form : bool
        True if the site use JS redirection

    Returns
    -------
    cookies : cookies
        returns the ent session cookies
    """
    if not url:
        raise ENTLoginError("Missing url attribute")

    log.debug(f"[ENT {url}] Logging in with {username}")

    # ENT Connection
    with requests.Session() as session:
        response = session.get(url, headers=HEADERS)

        if redirect_form:
            response = _sso_redirect(session, response, "SAMLResponse", url)
        if not response:
            raise ENTLoginError("Connection failure")

        _educonnect(session, username, password, response.url)

        return session.cookies


@typing.no_type_check
def _cas(
    username: str, password: str, url: str = "", **opts: str
) -> requests.cookies.RequestsCookieJar:
    """
    Generic function for CAS

    Parameters
    ----------
    username : str
        username
    password : str
        password
    url: str
        url of the ent login page

    Returns
    -------
    cookies : cookies
        returns the ent session cookies
    """
    if not url:
        raise ENTLoginError("Missing url attribute")

    log.debug(f"[ENT {url}] Logging in with {username}")

    # ENT Connection
    with requests.Session() as session:
        response = session.get(url, headers=HEADERS)

        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", {"class": "cas__login-form"})
        payload = {}
        for input_ in form.findAll("input"):
            payload[input_["name"]] = input_.get("value")
        payload["username"] = username
        payload["password"] = password

        r = session.post(response.url, data=payload, headers=HEADERS)
        soup = BeautifulSoup(r.text, "html.parser")

        if soup.find("form", {"class": "cas__login-form"}):
            raise ENTLoginError(
                f"Fail to connect with CAS {url} : probably wrong login information"
            )

        return session.cookies


def _open_ent_ng(
    username: str, password: str, url: str = "", **opts: str
) -> requests.cookies.RequestsCookieJar:
    """
    ENT which has an authentication like https://ent.iledefrance.fr/auth/login

    Parameters
    ----------
    username : str
        username
    password : str
        password
    url : str
        url of the ENT

    Returns
    -------
    cookies : cookies
        returns the ent session cookies
    """
    if not url:
        raise ENTLoginError("Missing url attribute")

    log.debug(f"[ENT {url}] Logging in with {username}")

    # ENT Connection
    with requests.Session() as session:
        payload = {"email": username, "password": password}
        r = session.post(url, headers=HEADERS, data=payload)

        if "login" in r.url:
            raise ENTLoginError(
                f"Fail to connect with Open NG {url} : probably wrong login information"
            )

        return session.cookies


def _open_ent_ng_edu(
    username: str,
    password: str,
    domain: str = "",
    providerId: str = "",
    **opts: str,
) -> requests.cookies.RequestsCookieJar:
    """
    ENT which has an authentication like https://connexion.l-educdenormandie.fr/

    Parameters
    ----------
    username : str
        username
    password : str
        password
    domain : str
        domain of the ENT

    Returns
    -------
    cookies : cookies
        returns the ent session cookies
    """
    if not domain:
        raise ENTLoginError("Missing domain attribute")
    if not providerId:
        providerId = f"{domain}/auth/saml/metadata/idp.xml"

    log.debug(f"[ENT {domain}] Logging in with {username}")

    # URL required
    ent_login_page = (
        "https://educonnect.education.gouv.fr/idp/profile/SAML2/Unsolicited/SSO"
    )

    with requests.Session() as session:
        params = {"providerId": providerId}

        response = session.get(ent_login_page, params=params, headers=HEADERS)
        response = _educonnect(
            session, username, password, response.url, exceptions=False
        )

        if not response:
            log.debug(f"Fail to connect with EduConnect, trying with Open NG")
            return _open_ent_ng(username, password, f"{domain}/auth/login")

        elif "login" in response.url:
            log.debug(f"Fail to connect with EduConnect, trying with Open NG")
            return _open_ent_ng(username, password, response.url)

        return session.cookies


@typing.no_type_check
def _wayf(
    username: str,
    password: str,
    domain: str = "",
    entityID: str = "",
    returnX: str = "",
    redirect_form: bool = True,
    **opts: str,
) -> requests.cookies.RequestsCookieJar:
    """
    Generic function for WAYF

    Parameters
    ----------
    username : str
        username
    password : str
        password
    domain : str
        domain of the ENT
    entityID : str
        request param entityID
    returnX : str
        request param returnX
    redirect_form : bool
        True if the site use JS redirection

    Returns
    -------
    cookies : cookies
        returns the ent session cookies
    """
    if not domain:
        raise ENTLoginError("Missing domain attribute")
    if not entityID:
        entityID = f"{domain}/shibboleth"
    if not returnX:
        returnX = f"{domain}/Shibboleth.sso/Login"

    log.debug(f"[ENT {domain}] Logging in with {username}")

    ent_login_page = f"{domain}/discovery/WAYF"

    # ENT Connection
    with requests.Session() as session:
        params = {
            "entityID": entityID,
            "returnX": returnX,
            "returnIDParam": "entityID",
            "action": "selection",
            "origin": "https://educonnect.education.gouv.fr/idp",
        }

        response = session.get(ent_login_page, params=params, headers=HEADERS)

        if redirect_form:
            response = _sso_redirect(session, response, "SAMLRequest", ent_login_page)
        if not response:
            raise ENTLoginError("Connection failure")

        _educonnect(session, username, password, response.url)

        return session.cookies


@typing.no_type_check
def _oze_ent(
    username: str, password: str, url: str = "", **opts: str
) -> requests.cookies.RequestsCookieJar:
    """
    Generic function for Oze ENT

    Parameters
    ----------
    username : str
        username
    password : str
        password
    url : str
        url of the ENT

    Returns
    -------
    cookies : cookies
        returns the ent session cookies
    """
    if not url:
        raise ENTLoginError("Missing url attribute")

    log.debug(f"[ENT {url}] Logging in with {username}")

    # ENT Connection
    with requests.Session() as session:
        response = session.get(url, headers=HEADERS)

        domain = urlparse(url).netloc

        if domain not in username:
            username = f"{username}@{domain}"

        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", {"id": "kc-form-login"})
        payload = {}
        for input_ in form.findAll("input"):
            payload[input_["name"]] = input_.get("value")
        payload["username"] = username
        payload["password"] = password

        r = session.post(response.url, data=payload, headers=HEADERS)

        if "auth_form" in r.text:
            raise ENTLoginError(
                f"Fail to connect with Oze ENT {url} : probably wrong login information"
            )

        # Compute the Oze API url
        api_url = urlunparse(
            urlparse(url)._replace(netloc="api-" + urlparse(url).netloc)
        )

        # Get mandatory user info for next call
        info_url = urljoin(api_url, "/v1/users/me")
        r = session.get(info_url, headers=HEADERS)
        info = r.json()
        ctx_profil = info["currentProfil"]["codeProfil"]
        ctx_etab = info["currentProfil"]["uai"]

        # Get info about Oze apps
        ozeapps_url = urljoin(api_url, "/v1/ozapps")
        payload = {
            "ctx_profil": ctx_profil,
            "ctx_etab": ctx_etab,
        }
        r = session.get(ozeapps_url, params=payload, headers=HEADERS)

        # Find proxySSO url for Pronote app and call it
        ozeapps = r.json()
        proxysso_url = None
        for app in ozeapps:
            if app["code"] == "pronote":
                proxysso_url = urljoin(url, app["externalRoute"])

        # If we still haven't got the url, try something else
        if not proxysso_url:
            pronoteConfig_url = urljoin(api_url, "/v1/config/Pronote")
            payload = {}
            payload["ctx_profil"] = ctx_profil
            payload["ctx_etab"] = ctx_etab
            r = session.get(pronoteConfig_url, params=payload, headers=HEADERS)
            pronoteConfig = r.json()
            if pronoteConfig["autorisationId"] and pronoteConfig["projet"]:
                proxysso_url = f"{url}cas/proxySSO/{pronoteConfig['autorisationId']}?uai={ctx_etab}&projet={pronoteConfig['projet']}&fonction=ELV"

        r = session.get(proxysso_url, headers=HEADERS)
        return session.cookies


@typing.no_type_check
def _simple_auth(
    username: str,
    password: str,
    url: str = "",
    form_attr: dict = {},
    **opts: str,
) -> requests.cookies.RequestsCookieJar:
    """
    Generic function for ENT with simple login form

    Parameters
    ----------
    username : str
        username
    password : str
        password
    url: str
        url of the ent login page
    form_attr: dict
        attr to locate form

    Returns
    -------
    cookies : cookies
        returns the ent session cookies
    """
    if not url:
        raise ENTLoginError("Missing url attribute")

    log.debug(f"[ENT {url}] Logging in with {username}")

    # ENT Connection
    with requests.Session() as session:
        response = session.get(url, headers=HEADERS)

        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", form_attr)
        payload = {}
        for input_ in form.findAll("input"):
            payload[input_["name"]] = input_.get("value")
        payload["username"] = username
        payload["password"] = password

        r = session.post(response.url, data=payload, headers=HEADERS)
        soup = BeautifulSoup(r.text, "html.parser")

        if soup.find("form", form_attr):
            raise ENTLoginError(
                f"Fail to connect with {url} : probably wrong login information"
            )

        return session.cookies


@typing.no_type_check
def _hubeduconnect(
    username: str, password: str, pronote_url: str = "", **opts: str
) -> requests.cookies.RequestsCookieJar:
    """
    Pronote EduConnect connection (with HubEduConnect.index-education.net)

    Parameters
    ----------
    username : str
        username
    password : str
        password
    pronote_url: str
        URL of Pronote instance

    Returns
    -------
    cookies : cookies
        returns the ent session cookies
    """
    hubeduconnect_url = "https://hubeduconnect.index-education.net/EduConnect/cas/login"
    url = f"{hubeduconnect_url}?service={pronote_url}"

    with requests.Session() as session:
        response = session.get(url, headers=HEADERS)

        response = _sso_redirect(session, response, "SAMLRequest", url)
        if not response:
            raise ENTLoginError("Connection failure")

        if response.content.__contains__(
            b'<label id="zone_msgDetail">L&#x27;url de service est vide</label>'
        ):
            raise ENTLoginError(
                "Fail to connect with HubEduConnect : Service URL not provided."
            )
        elif response.content.__contains__(b"n&#x27;est pas une url de confiance."):
            raise ENTLoginError(
                "Fail to connect with HubEduConnect : Service URL not trusted. Is Pronote instance supported?"
            )

        _educonnect(session, username, password, response.url)

    return session.cookies
