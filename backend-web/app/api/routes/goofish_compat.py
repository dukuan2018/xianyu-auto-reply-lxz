from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import get_settings
from common.db.session import async_session_maker
from common.models.system_setting import SystemSetting
from common.models.xy_account import XYAccount
from common.services.order_service import OrderStatusChecker
from common.utils.internal_auth import build_internal_auth_headers

router = APIRouter(tags=["Goofish兼容接口"])


class CompatSendMessageRequest(BaseModel):
    api_key: str = ""
    cookie_id: str
    chat_id: str
    to_user_id: str = ""
    message: str


class CompatSendImageRequest(BaseModel):
    api_key: str = ""
    cookie_id: str
    chat_id: str
    to_user_id: str = ""
    image_url: str | None = ""
    image_base64: str | None = ""
    filename: str | None = "image.jpg"


class CompatOrderFullInfoRequest(BaseModel):
    api_key: str = ""
    cookie_id: str
    order_id: str = Field(default="", alias="order_id")


async def _get_system_setting(key: str, default: str = "") -> str:
    async with async_session_maker() as session:
        result = await session.execute(
            select(SystemSetting.value).where(SystemSetting.key == key)
        )
        value = result.scalar_one_or_none()
        return str(value) if value is not None else default


async def _expected_api_key() -> str:
    key = (await _get_system_setting("goofish.chat.api.key", "")).strip()
    if key:
        return key
    return (await _get_system_setting("goofish.chat_push.key", "")).strip()


async def _verify_api_key(api_key: str) -> tuple[bool, str]:
    expected = await _expected_api_key()
    if not expected:
        return False, "Python端未配置goofish.chat.api.key"
    if not api_key or api_key != expected:
        return False, "api_key校验失败"
    return True, ""


def _plain_response(success: bool, message: str, data: Any | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"success": success, "message": message}
    if data is not None:
        result["data"] = data
    return result


async def _post_websocket_internal(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    url = f"{settings.websocket_service_url.rstrip('/')}{path}"
    headers = build_internal_auth_headers(settings.internal_api_token)
    async with httpx.AsyncClient(timeout=35.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    return data if isinstance(data, dict) else {"success": False, "message": "WebSocket服务返回格式异常"}


async def _get_account_cookie(cookie_id: str) -> str:
    async with async_session_maker() as session:
        result = await session.execute(
            select(XYAccount.cookie).where(XYAccount.account_id == cookie_id)
        )
        return result.scalar_one_or_none() or ""


def _write_base64_image(image_base64: str, filename: str) -> tuple[str, str]:
    raw = image_base64
    if "," in raw and raw.split(",", 1)[0].lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    suffix = os.path.splitext(filename or "")[1] or ".jpg"
    static_root = Path(get_settings().static_dir)
    if not static_root.is_absolute():
        static_root = Path.cwd() / static_root
    upload_dir = static_root / "uploads" / "goofish_compat"
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / f"goofish_{os.urandom(8).hex()}{suffix}"
    with open(file_path, "wb") as file:
        file.write(base64.b64decode(raw))
    return str(file_path), f"/static/uploads/goofish_compat/{file_path.name}"


@router.post("/send-message")
async def send_message(request: CompatSendMessageRequest):
    ok, error = await _verify_api_key(request.api_key)
    if not ok:
        return _plain_response(False, error)
    if not request.cookie_id or not request.chat_id or not request.message:
        return _plain_response(False, "cookie_id/chat_id/message不能为空")
    if not request.to_user_id.strip():
        return _plain_response(False, "to_user_id不能为空")

    try:
        result = await _post_websocket_internal(
            f"/internal/accounts/{request.cookie_id}/send-message",
            {
                "chat_id": request.chat_id,
                "to_user_id": request.to_user_id,
                "message": request.message,
                "wait_result": True,
                "wait_timeout": 15,
            },
        )
        success = bool(result.get("success"))
        return _plain_response(success, result.get("message") or ("发送成功" if success else "发送失败"), result)
    except Exception as exc:
        logger.error(
            "goofish compat send-message exception cookie_id={} chat_id={}: {}",
            request.cookie_id, request.chat_id, exc,
        )
        return _plain_response(False, str(exc))


@router.post("/send-image")
async def send_image(request: CompatSendImageRequest):
    ok, error = await _verify_api_key(request.api_key)
    if not ok:
        return _plain_response(False, error)
    if not request.cookie_id or not request.chat_id:
        return _plain_response(False, "cookie_id/chat_id不能为空")
    if not request.to_user_id.strip():
        return _plain_response(False, "to_user_id不能为空")

    image_url = request.image_url
    temp_path = ""
    if not image_url and request.image_base64:
        try:
            temp_path, image_url = _write_base64_image(request.image_base64, request.filename or "image.jpg")
        except Exception as exc:
            return _plain_response(False, f"image_base64解析失败: {exc}")
    if not image_url:
        return _plain_response(False, "image_url/image_base64不能同时为空")

    try:
        result = await _post_websocket_internal(
            f"/internal/accounts/{request.cookie_id}/send-image",
            {
                "chat_id": request.chat_id,
                "to_user_id": request.to_user_id,
                "image_url": image_url,
            },
        )
        success = bool(result.get("success"))
        return _plain_response(success, result.get("message") or ("发送成功" if success else "发送失败"), result)
    except Exception as exc:
        logger.error(
            "goofish compat send-image exception cookie_id={} chat_id={}: {}",
            request.cookie_id, request.chat_id, exc,
        )
        return _plain_response(False, str(exc))
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


@router.post("/order-full-info")
async def order_full_info(request: CompatOrderFullInfoRequest):
    ok, error = await _verify_api_key(request.api_key)
    if not ok:
        return _plain_response(False, error)
    if not request.cookie_id or not request.order_id:
        return _plain_response(False, "cookie_id/order_id不能为空")

    cookies = await _get_account_cookie(request.cookie_id)
    if not cookies:
        return _plain_response(False, f"未找到账号Cookie: {request.cookie_id}")

    try:
        checker = OrderStatusChecker(cookies, request.cookie_id)
        raw = await checker._fetch_raw_order_detail(request.order_id)
        if not raw:
            return {
                "success": False,
                "message": "订单详情获取失败",
                "cookie_id": request.cookie_id,
                "order_id": request.order_id,
                "data": {},
            }
        parsed = _extract_order_summary(request.order_id, raw)
        return {
            "success": True,
            "message": "request finished",
            "cookie_id": request.cookie_id,
            "order_id": request.order_id,
            **parsed,
            "raw": raw,
            "data": parsed,
        }
    except Exception as exc:
        logger.error(
            "goofish compat order-full-info exception cookie_id={} order_id={}: {}",
            request.cookie_id, request.order_id, exc,
        )
        return _plain_response(False, str(exc))


def _extract_order_summary(order_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data") if isinstance(raw, dict) else {}
    if not isinstance(data, dict):
        data = {}
    components = data.get("components") or []
    result: dict[str, Any] = {
        "order_id": order_id,
        "orderId": order_id,
        "buyer_id": str(data.get("peerUserId") or ""),
        "buyerId": str(data.get("peerUserId") or ""),
        "item_id": str(data.get("itemId") or ""),
        "itemId": str(data.get("itemId") or ""),
        "order_status": "",
        "orderStatus": "",
        "order_amount": "",
        "orderAmount": "",
        "merchant_price": {},
        "merchantPriceVO": {},
        "price_info": {},
        "priceInfo": {},
        "pay_success_time": "",
    }
    if not isinstance(components, list):
        return result

    for component in components:
        if not isinstance(component, dict):
            continue
        render = component.get("render")
        comp_data = component.get("data") if isinstance(component.get("data"), dict) else {}
        if render == "orderInfoVO":
            item_info = comp_data.get("itemInfo") if isinstance(comp_data.get("itemInfo"), dict) else {}
            result["item_id"] = str(item_info.get("itemId") or comp_data.get("itemId") or result["item_id"])
            result["itemId"] = result["item_id"]
            result["buyer_id"] = str(comp_data.get("buyerUserId") or comp_data.get("buyerId") or result["buyer_id"])
            result["buyerId"] = result["buyer_id"]
            if item_info.get("price"):
                result["order_amount"] = str(item_info.get("price"))
                result["orderAmount"] = result["order_amount"]
        elif render == "orderStatusVO":
            info = comp_data.get("orderStatusInfo") if isinstance(comp_data.get("orderStatusInfo"), dict) else {}
            result["order_status"] = str(info.get("title") or "")
            result["orderStatus"] = result["order_status"]
        elif render in ("merchantPriceVO", "priceInfoVO"):
            result["merchant_price"] = comp_data
            result["merchantPriceVO"] = comp_data
            result["price_info"] = comp_data
            result["priceInfo"] = comp_data
            total = comp_data.get("totalPrice") or comp_data.get("auctionPrice")
            if total:
                result["order_amount"] = str(total)
                result["orderAmount"] = result["order_amount"]

    return result
