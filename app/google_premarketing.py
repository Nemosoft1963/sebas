"""Google Forms publication and idempotent response ingestion."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.premarketing import capture_and_queue


FORM_FIELDS = (
    ("name", "お名前", True, False),
    ("email", "メールアドレス", True, False),
    ("company", "会社・組織", False, False),
    ("role", "役割", False, False),
    ("problem", "現在の課題", True, True),
    ("timeline", "検討時期", False, False),
    ("budget", "予算感", False, False),
)
CONSENT_TITLE = "個人情報の利用への同意"
CONSENT_VALUE = "相談対応のための利用に同意する"


class GooglePremarketingError(RuntimeError):
    pass


class GoogleAuthenticationRequired(GooglePremarketingError):
    pass


@dataclass(frozen=True)
class GooglePremarketingConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    account_email: str
    forms_base_url: str = "https://forms.googleapis.com/v1"
    token_url: str = "https://oauth2.googleapis.com/token"
    timeout_seconds: float = 30.0
    max_attempts: int = 3

    @classmethod
    def from_env(cls) -> "GooglePremarketingConfig":
        return cls(
            client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
            client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
            refresh_token=os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip(),
            account_email=os.getenv("GOOGLE_MARKETING_ACCOUNT", "").strip(),
            forms_base_url=os.getenv(
                "GOOGLE_FORMS_BASE_URL", "https://forms.googleapis.com/v1"
            ).rstrip("/"),
            token_url=os.getenv(
                "GOOGLE_OAUTH_TOKEN_URL", "https://oauth2.googleapis.com/token"
            ),
            timeout_seconds=max(5.0, float(os.getenv("GOOGLE_API_TIMEOUT", "30"))),
            max_attempts=max(1, min(int(os.getenv("GOOGLE_API_MAX_ATTEMPTS", "3")), 5)),
        )

    def missing(self) -> list[str]:
        values = {
            "GOOGLE_OAUTH_CLIENT_ID": self.client_id,
            "GOOGLE_OAUTH_CLIENT_SECRET": self.client_secret,
            "GOOGLE_OAUTH_REFRESH_TOKEN": self.refresh_token,
            "GOOGLE_MARKETING_ACCOUNT": self.account_email,
        }
        return [key for key, value in values.items() if not value]

    @property
    def ready(self) -> bool:
        return not self.missing()


class GoogleFormsPublisher:
    def __init__(self, config: GooglePremarketingConfig,
                 client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._owns_client = client is None
        self._access_token = ""
        self._token_expires_at = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _token(self) -> str:
        if not self.config.ready:
            raise GoogleAuthenticationRequired(
                "Google認証設定が不足しています: " + ", ".join(self.config.missing())
            )
        if self._access_token and time.monotonic() < self._token_expires_at - 60:
            return self._access_token
        response = await self.client.post(self.config.token_url, data={
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "refresh_token": self.config.refresh_token,
            "grant_type": "refresh_token",
        })
        if response.status_code >= 400:
            raise GoogleAuthenticationRequired(
                f"Google OAuth更新に失敗しました ({response.status_code})"
            )
        payload = response.json()
        self._access_token = str(payload.get("access_token", ""))
        if not self._access_token:
            raise GoogleAuthenticationRequired("Google OAuth応答にaccess_tokenがありません")
        self._token_expires_at = time.monotonic() + int(payload.get("expires_in", 3600))
        return self._access_token

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.config.max_attempts):
            token = await self._token()
            try:
                response = await self.client.request(
                    method, f"{self.config.forms_base_url}/{path.lstrip('/')}",
                    headers={"Authorization": f"Bearer {token}"}, **kwargs,
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt + 1 < self.config.max_attempts:
                    await asyncio.sleep(0.25 * (2 ** attempt))
                    continue
                raise GooglePremarketingError(f"Google APIへ接続できません: {exc}") from exc
            if response.status_code in {401, 403}:
                self._access_token = ""
                raise GoogleAuthenticationRequired(
                    f"Google認証または権限の再確認が必要です ({response.status_code})"
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = GooglePremarketingError(
                    f"Google API一時エラー ({response.status_code})"
                )
                if attempt + 1 < self.config.max_attempts:
                    await asyncio.sleep(0.25 * (2 ** attempt))
                    continue
            if response.status_code >= 400:
                detail = response.text[:1000]
                raise GooglePremarketingError(
                    f"Google APIエラー ({response.status_code}): {detail}"
                )
            return response.json() if response.content else {}
        raise last_error or GooglePremarketingError("Google API処理に失敗しました")

    @staticmethod
    def _create_item(title: str, required: bool, paragraph: bool, index: int) -> dict:
        return {
            "createItem": {
                "item": {
                    "title": title,
                    "questionItem": {"question": {
                        "required": required,
                        "textQuestion": {"paragraph": paragraph},
                    }},
                },
                "location": {"index": index},
            }
        }

    async def create_and_publish(self, campaign: dict, on_created=None) -> dict[str, Any]:
        created = await self._request("POST", "forms?unpublished=true", json={
            "info": {"title": campaign["title"]},
        })
        form_id = str(created.get("formId", ""))
        if not form_id:
            raise GooglePremarketingError("GoogleフォームIDを取得できませんでした")
        if on_created is not None:
            on_created(form_id, str(created.get("responderUri") or ""))
        return await self.resume_existing({**campaign, "google_form_id": form_id})

    async def resume_existing(self, campaign: dict) -> dict[str, Any]:
        """Finish a known form ID without issuing forms.create again."""
        form_id = str(campaign.get("google_form_id") or "")
        if not form_id:
            raise GooglePremarketingError("再開するフォームIDがありません")
        form = await self._request("GET", f"forms/{form_id}")
        if str(form.get("formId") or "") != form_id:
            raise GooglePremarketingError("保存済みフォームIDとGoogleの回答が一致しません")
        if str((form.get("info") or {}).get("title") or "") != str(campaign.get("title") or ""):
            raise GooglePremarketingError("Googleフォームのタイトルが案件と一致しません")
        items = form.get("items") or []
        by_title = {str(item.get("title") or ""): item for item in items if isinstance(item, dict)}
        expected = [(key, title, required, paragraph) for key, title, required, paragraph in FORM_FIELDS]
        titles = {title for _, title, _, _ in expected} | {CONSENT_TITLE}
        for title in titles & set(by_title):
            if not (by_title[title].get("questionItem") or {}).get("question"):
                raise GooglePremarketingError("既存フォームの質問が不完全です。人間が確認してください")
        requests = [{
            "updateFormInfo": {
                "info": {"description": (
                    f"対象: {campaign['audience']}\n\n提供内容: {campaign['offer']}\n\n"
                    "回答は相談対応とサービス改善のために利用します。"
                )},
                "updateMask": "description",
            }
        }]
        index = len(items)
        for _, title, required, paragraph in expected:
            if title not in by_title:
                requests.append(self._create_item(title, required, paragraph, index))
                index += 1
        if CONSENT_TITLE not in by_title:
            requests.append({
                "createItem": {
                    "item": {
                        "title": CONSENT_TITLE,
                        "questionItem": {"question": {
                            "required": True,
                            "choiceQuestion": {
                                "type": "CHECKBOX",
                                "options": [{"value": CONSENT_VALUE}],
                                "shuffle": False,
                            },
                        }},
                    },
                    "location": {"index": index},
                }
            })
        await self._request("POST", f"forms/{form_id}:batchUpdate", json={"requests": requests})
        state = (form.get("publishSettings") or {}).get("publishState") or {}
        if not (state.get("isPublished") and state.get("isAcceptingResponses")):
            await self._request("POST", f"forms/{form_id}:setPublishSettings", json={
                "publishSettings": {"publishState": {
                    "isPublished": True, "isAcceptingResponses": True,
                }},
            })
        verified = await self._request("GET", f"forms/{form_id}")
        if str(verified.get("formId") or "") != form_id:
            raise GooglePremarketingError("再開後のフォームIDを検証できません")
        state = (verified.get("publishSettings") or {}).get("publishState") or {}
        if not (state.get("isPublished") and state.get("isAcceptingResponses")):
            raise GooglePremarketingError("フォームの公開・回答受付を検証できません")
        title_to_key = {title: key for key, title, _, _ in FORM_FIELDS}
        question_map = {}
        for item in verified.get("items") or []:
            title = str(item.get("title") or "")
            question_id = (item.get("questionItem") or {}).get("question", {}).get("questionId")
            if question_id and title in title_to_key:
                question_map[question_id] = title_to_key[title]
            elif question_id and title == CONSENT_TITLE:
                question_map[question_id] = "consent"
        expected_keys = {key for key, *_ in FORM_FIELDS} | {"consent"}
        if set(question_map.values()) != expected_keys:
            raise GooglePremarketingError("再開後の質問項目が不足しています")
        uri = str(verified.get("responderUri") or "")
        if not uri.startswith("https://"):
            raise GooglePremarketingError("回答URLを検証できません")
        return {"form_id": form_id, "responder_uri": uri, "question_map": question_map}

    async def list_responses(self, form_id: str,
                             since: str = "") -> list[dict[str, Any]]:
        params = {"pageSize": 5000}
        if since:
            params["filter"] = f"timestamp > {since}"
        responses: list[dict[str, Any]] = []
        page_token = ""
        while True:
            if page_token:
                params["pageToken"] = page_token
            payload = await self._request(
                "GET", f"forms/{form_id}/responses", params=params,
            )
            responses.extend(payload.get("responses", []))
            page_token = str(payload.get("nextPageToken", ""))
            if not page_token:
                return responses


def response_values(response: dict, question_map: dict[str, str]) -> dict[str, str]:
    values = {key: "" for key, *_ in FORM_FIELDS}
    values["consent"] = ""
    for question_id, answer in response.get("answers", {}).items():
        key = question_map.get(question_id)
        if not key:
            continue
        entries = answer.get("textAnswers", {}).get("answers", [])
        values[key] = "\n".join(str(item.get("value", "")) for item in entries).strip()
    if not values["email"]:
        values["email"] = str(response.get("respondentEmail", "")).strip()
    return values


async def sync_campaign_responses(memory, campaign: dict,
                                  publisher: GoogleFormsPublisher) -> dict[str, Any]:
    if not campaign.get("google_form_id"):
        raise GooglePremarketingError("Googleフォームが未作成です")
    try:
        question_map = json.loads(campaign.get("google_question_map") or "{}")
    except json.JSONDecodeError as exc:
        raise GooglePremarketingError("質問マッピングが壊れています") from exc
    responses = await publisher.list_responses(
        campaign["google_form_id"], campaign.get("last_synced_at") or "",
    )
    imported = duplicates = rejected = 0
    errors: list[str] = []
    latest = campaign.get("last_synced_at") or ""
    for response in sorted(responses, key=lambda item: item.get("lastSubmittedTime", "")):
        response_id = str(response.get("responseId", ""))
        submitted = str(response.get("lastSubmittedTime", ""))
        if submitted > latest:
            latest = submitted
        if not response_id:
            rejected += 1
            errors.append("responseIdのない回答を除外しました")
            continue
        if memory.get_lead_by_external_ref(campaign["id"], "google_forms", response_id):
            duplicates += 1
            continue
        values = response_values(response, question_map)
        consent = CONSENT_VALUE in values["consent"]
        try:
            capture_and_queue(
                memory, campaign, name=values["name"], email=values["email"],
                company=values["company"], role=values["role"],
                problem=values["problem"], timeline=values["timeline"],
                budget=values["budget"], consent=consent,
                source="google_forms", external_ref=response_id,
            )
            imported += 1
        except ValueError as exc:
            rejected += 1
            errors.append(f"{response_id}: {exc}")
    return {
        "fetched": len(responses), "imported": imported, "duplicates": duplicates,
        "rejected": rejected, "errors": errors[:20],
        "last_synced_at": latest or datetime.now(timezone.utc).isoformat(),
    }
