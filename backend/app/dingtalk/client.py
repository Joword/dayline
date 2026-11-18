"""DingTalk OpenAPI client (internal enterprise app).

Two API generations are used:

* ``api.dingtalk.com/v1.0/...`` - header ``x-acs-dingtalk-access-token``; access token from
  ``/v1.0/oauth2/accessToken`` (also used by the official Stream SDK); robot one-to-one
  messages (``/v1.0/robot/oToMessages/batchSend``) - verified against the official SDK.
* ``oapi.dingtalk.com/topapi/...`` - ``access_token`` query parameter, ``errcode`` in the body:
  log (report) create / template / list and the contact directory. Report create and
  template-by-name are not in the SDKs we could inspect; their shapes follow the public
  documentation and should be checked against a real tenant before go-live
  (``dl dingtalk status --check-template``).

All calls go through ``TargetClient`` (proxy ``auto|none|url``, company CA).
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.integrations.http import TargetClient

API = "https://api.dingtalk.com"
OAPI = "https://oapi.dingtalk.com"
INVALID_TOKEN_CODES = {40001, 40014, 42001, 88}


class DingTalkError(Exception):
    def __init__(self, msg: str, code: int | str | None = None):
        super().__init__(msg)
        self.code = code


class DingTalkClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        robot_code: str | None = None,
        http: TargetClient | None = None,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.robot_code = robot_code or app_key
        self.http = http or TargetClient("auto")
        self._token: tuple[str, float] | None = None

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- auth ---------------------------------------------------------------------------

    async def access_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token[1]:
            return self._token[0]
        resp = await self.http.request(
            "POST",
            f"{API}/v1.0/oauth2/accessToken",
            json={"appKey": self.app_key, "appSecret": self.app_secret},
        )
        body = _json(resp)
        if resp.status_code != 200 or "accessToken" not in body:
            raise DingTalkError(
                f"cannot get access token: {body.get('message') or resp.status_code}", body.get("code")
            )
        self._token = (body["accessToken"], time.time() + int(body.get("expireIn", 7200)) - 300)
        return self._token[0]

    async def _api(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in (1, 2):
            token = await self.access_token(force=attempt == 2)
            resp = await self.http.request(
                method, f"{API}{path}", json=body, headers={"x-acs-dingtalk-access-token": token}
            )
            data = _json(resp)
            if resp.status_code == 401 and attempt == 1:
                continue
            if resp.status_code >= 400:
                raise DingTalkError(f"{path}: {data.get('message') or resp.status_code}", data.get("code"))
            return data
        raise DingTalkError(f"{path}: unauthorized")

    async def _oapi(self, path: str, body: dict[str, Any]) -> Any:
        for attempt in (1, 2):
            token = await self.access_token(force=attempt == 2)
            resp = await self.http.request("POST", f"{OAPI}{path}", params={"access_token": token}, json=body)
            data = _json(resp)
            code = data.get("errcode", 0 if resp.status_code == 200 else resp.status_code)
            if code in INVALID_TOKEN_CODES and attempt == 1:
                continue
            if code != 0:
                raise DingTalkError(f"{path}: {data.get('errmsg') or code}", code)
            return data.get("result")
        raise DingTalkError(f"{path}: invalid access token")

    # --- messages -----------------------------------------------------------------------

    async def send_markdown(self, user_ids: list[str], title: str, text: str) -> dict[str, Any]:
        """Robot one-to-one message to DingTalk users (staff ids)."""
        return await self._api(
            "POST",
            "/v1.0/robot/oToMessages/batchSend",
            {
                "robotCode": self.robot_code,
                "userIds": user_ids,
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
            },
        )

    async def send_action_card(
        self, user_ids: list[str], title: str, text: str, buttons: list[tuple[str, str]]
    ) -> dict[str, Any]:
        """One-to-one action card with up to two buttons (sampleActionCard / sampleActionCard2)."""
        if len(buttons) == 1:
            key, param = (
                "sampleActionCard",
                {"title": title, "text": text, "singleTitle": buttons[0][0], "singleURL": buttons[0][1]},
            )
        else:
            (t1, u1), (t2, u2) = buttons[:2]
            key, param = (
                "sampleActionCard2",
                {
                    "title": title,
                    "text": text,
                    "actionTitle1": t1,
                    "actionURL1": u1,
                    "actionTitle2": t2,
                    "actionURL2": u2,
                },
            )
        return await self._api(
            "POST",
            "/v1.0/robot/oToMessages/batchSend",
            {
                "robotCode": self.robot_code,
                "userIds": user_ids,
                "msgKey": key,
                "msgParam": json.dumps(param, ensure_ascii=False),
            },
        )

    async def reply(self, session_webhook: str, payload: dict[str, Any]) -> None:
        """Reply in the conversation a bot message came from (no token needed)."""
        resp = await self.http.request("POST", session_webhook, json=payload)
        data = _json(resp)
        if resp.status_code >= 400 or data.get("errcode", 0) != 0:
            raise DingTalkError(
                f"session webhook: {data.get('errmsg') or resp.status_code}", data.get("errcode")
            )

    # --- logs (日志) ----------------------------------------------------------------------

    async def get_template(self, name: str, userid: str) -> dict[str, Any]:
        """{"id", "name", "fields": [{"field_name", "sort", "type"}], ...}"""
        return (
            await self._oapi("/topapi/report/template/getbyname", {"template_name": name, "userid": userid})
            or {}
        )

    async def create_report(
        self,
        *,
        userid: str,
        template_id: str,
        contents: list[dict[str, Any]],
        to_userids: list[str] | None = None,
    ) -> str:
        param: dict[str, Any] = {
            "userid": userid,
            "template_id": template_id,
            "contents": contents,
            "dd_from": "dayline",
            "to_chat": False,
        }
        if to_userids:
            param["to_userids"] = to_userids
        return str(await self._oapi("/topapi/report/create", {"create_report_param": param}))

    async def list_reports(
        self,
        *,
        userid: str,
        start_ms: int,
        end_ms: int,
        template_name: str = "",
        cursor: int = 0,
        size: int = 20,
    ) -> dict[str, Any]:
        return (
            await self._oapi(
                "/topapi/report/list",
                {
                    "start_time": start_ms,
                    "end_time": end_ms,
                    "cursor": cursor,
                    "size": size,
                    "template_name": template_name,
                    "userid": userid,
                },
            )
            or {}
        )

    # --- contacts -------------------------------------------------------------------------

    async def sub_department_ids(self, dept_id: int) -> list[int]:
        return (await self._oapi("/topapi/v2/department/listsubid", {"dept_id": dept_id}) or {}).get(
            "dept_id_list", []
        )

    async def department(self, dept_id: int) -> dict[str, Any]:
        return await self._oapi("/topapi/v2/department/get", {"dept_id": dept_id}) or {}

    async def department_user_ids(self, dept_id: int) -> list[str]:
        return (await self._oapi("/topapi/user/listid", {"dept_id": dept_id}) or {}).get("userid_list", [])

    async def user(self, userid: str) -> dict[str, Any]:
        return await self._oapi("/topapi/v2/user/get", {"userid": userid, "language": "zh_CN"}) or {}


def _json(resp) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"result": data}
    except ValueError:
        return {}


def client_from_settings(settings) -> DingTalkClient | None:
    if not settings.dingtalk_enabled:
        return None
    return DingTalkClient(
        settings.dingtalk_app_key,
        settings.dingtalk_app_secret.get_secret_value(),
        robot_code=settings.dingtalk_robot_code,
        http=TargetClient("auto", fallback_proxy=settings.integration_proxy),
    )
