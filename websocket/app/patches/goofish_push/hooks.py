from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

ORDER_STATUS_BY_MESSAGE = {
    "[我已拍下，待付款]": "pending_payment",
    "[我已修改价格，等待你付款]": "price_modified_pending_payment",
    "[我已付款，等待你发货]": "paid",
    "[买家已付款]": "paid",
    "[付款完成]": "paid",
    "[已付款，待发货]": "paid",
}


@dataclass
class PushSettings:
    enabled: bool
    chat_reply_url: str
    chat_notify_url: str
    api_key: str
    timeout_seconds: float
    push_order_enabled: bool
    push_image_enabled: bool


_dedup_cache: dict[str, float] = {}
_dedup_ttl_seconds = 30 * 60


async def handle_parsed_message(xianyu_instance: Any, parsed_message: dict[str, Any], source: str) -> None:
    """Push image and order events to Java without blocking core message flow."""
    try:
        settings = await _load_settings()
        if not settings.enabled:
            return
        if settings.push_image_enabled:
            await _push_image_message(settings, xianyu_instance, parsed_message, source)
        if settings.push_order_enabled:
            await _push_order_message(settings, xianyu_instance, parsed_message, source)
    except Exception as exc:
        cookie_id = getattr(xianyu_instance, "cookie_id", "")
        logger.warning(f"【{cookie_id}】goofish push skipped by exception: {exc}")


async def _load_settings() -> PushSettings:
    enabled = _is_truthy(await _get_setting("goofish.chat_push.enabled", "false"))
    base_url = _normalize_base_url(await _get_setting("goofish.chat_push.url", ""))
    chat_reply_url = _build_push_url(
        base_url,
        await _get_setting("goofish.chat_push.chat_reply_url", ""),
        "/xianyu/chatReply",
    )
    chat_notify_url = _build_push_url(
        base_url,
        await _get_setting("goofish.chat_push.chat_notify_url", ""),
        "/xianyu/chatNodify",
    )
    api_key = (await _get_setting("goofish.chat_push.key", "")).strip()
    if not api_key:
        api_key = (await _get_setting("goofish.chat.api.key", "")).strip()
    timeout_raw = await _get_setting("goofish.chat_push.timeout_seconds", "10")
    try:
        timeout_seconds = max(1.0, float(timeout_raw))
    except (TypeError, ValueError):
        timeout_seconds = 10.0
    return PushSettings(
        enabled=enabled,
        chat_reply_url=chat_reply_url,
        chat_notify_url=chat_notify_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        push_order_enabled=_is_truthy(await _get_setting("goofish.chat_push.push_order_enabled", "true")),
        push_image_enabled=_is_truthy(await _get_setting("goofish.chat_push.push_image_enabled", "true")),
    )


async def _get_setting(key: str, default: str = "") -> str:
    from common.db.compat import db_manager

    value = db_manager.get_system_setting(key, default)
    return str(value) if value is not None else default


def _is_truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _normalize_base_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}".rstrip("/")
    if "://" not in url and not url.startswith("/"):
        return f"https://{url}".rstrip("/")
    return url


def _build_push_url(base_url: str, configured_url: Any, default_path: str) -> str:
    url = str(configured_url or "").strip()
    if not url:
        return f"{base_url}{default_path}" if base_url else ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{base_url}{url}" if base_url else url
    if "://" not in url:
        return f"https://{url}"
    return url


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def _push_image_message(
    settings: PushSettings,
    xianyu_instance: Any,
    parsed_message: dict[str, Any],
    source: str,
) -> None:
    if not settings.chat_reply_url:
        return
    if not _is_valid_http_url(settings.chat_reply_url):
        logger.warning("goofish image push skipped, invalid chat_reply_url={}", settings.chat_reply_url)
        return
    image_urls = _extract_image_urls(parsed_message)
    if not image_urls:
        return

    cookie_id = getattr(xianyu_instance, "cookie_id", "")
    myid = str(getattr(xianyu_instance, "myid", "") or "").split("@", 1)[0].strip()
    send_user_id = _clean_unknown(parsed_message.get("send_user_id", ""))
    normalized_send_user_id = str(send_user_id or "").split("@", 1)[0].strip()
    if not normalized_send_user_id:
        logger.info(
            "【{}】goofish image push skipped, send_user_id empty source={} chat_id={} image_count={}",
            cookie_id, source, parsed_message.get("chat_id", ""), len(image_urls),
        )
        return
    if myid and normalized_send_user_id == myid:
        logger.info(
            "【{}】goofish image push skipped, self message source={} chat_id={} send_user_id={} image_count={}",
            cookie_id, source, parsed_message.get("chat_id", ""), normalized_send_user_id, len(image_urls),
        )
        return

    item_id = str(parsed_message.get("item_id", "") or "").strip()
    if not item_id:
        logger.info(
            "【{}】goofish image push skipped, item_id empty source={} chat_id={} image_count={}",
            cookie_id, source, parsed_message.get("chat_id", ""), len(image_urls),
        )
        return

    message_id = _extract_message_id(xianyu_instance, parsed_message)
    dedup_key = f"image:{cookie_id}:{message_id or '|'.join(image_urls)}"
    if _is_duplicate(dedup_key):
        return

    payload = {
        "cookie_id": cookie_id,
        "msg_time": parsed_message.get("msg_time", ""),
        "send_user_id": send_user_id,
        "send_user_name": parsed_message.get("send_user_name", ""),
        "item_id": item_id,
        "send_message": parsed_message.get("send_message", ""),
        "chat_id": parsed_message.get("chat_id", ""),
        "image_urls": image_urls,
    }
    headers = {"X-Goofish-Chat-Key": settings.api_key} if settings.api_key else {}
    await _post_json(settings.chat_reply_url, payload, settings.timeout_seconds, headers=headers)
    logger.info(
        "【{}】goofish image pushed source={} chat_id={} image_count={}",
        cookie_id, source, payload["chat_id"], len(image_urls),
    )


async def _push_order_message(
    settings: PushSettings,
    xianyu_instance: Any,
    parsed_message: dict[str, Any],
    source: str,
) -> None:
    if not settings.chat_notify_url:
        return
    if not _is_valid_http_url(settings.chat_notify_url):
        logger.warning("goofish order push skipped, invalid chat_notify_url={}", settings.chat_notify_url)
        return
    send_message = str(parsed_message.get("send_message", "") or "")
    order_status = ORDER_STATUS_BY_MESSAGE.get(send_message)
    if not order_status:
        return

    raw_message = parsed_message.get("raw_message") or {}
    order_id = ""
    try:
        order_id = xianyu_instance._extract_order_id(raw_message)
    except Exception as exc:
        logger.warning("【{}】goofish order id extract failed: {}", getattr(xianyu_instance, "cookie_id", ""), exc)
    if not order_id:
        logger.warning(
            "【{}】goofish order push skipped, order_id empty status={} message={}",
            getattr(xianyu_instance, "cookie_id", ""), order_status, send_message,
        )
        return

    cookie_id = getattr(xianyu_instance, "cookie_id", "")
    message_id = _extract_message_id(xianyu_instance, parsed_message)
    dedup_key = f"order:{cookie_id}:{order_id}:{order_status}:{message_id or ''}"
    if _is_duplicate(dedup_key):
        return

    send_user_id = _clean_unknown(parsed_message.get("send_user_id", ""))
    buyer_id = _clean_unknown(parsed_message.get("buyer_id", ""))
    payload = {
        "orderId": order_id,
        "sendUserId": send_user_id,
        "buyerId": buyer_id,
        "goofishShopId": cookie_id,
        "goofishChatId": parsed_message.get("chat_id", ""),
        "itemId": parsed_message.get("item_id", ""),
        "orderStatus": order_status,
        "messageId": message_id,
        "eventTime": _event_time_ms(parsed_message.get("msg_time", "")),
    }
    headers = {"X-Goofish-Chat-Key": settings.api_key} if settings.api_key else {}
    await _post_json(settings.chat_notify_url, payload, settings.timeout_seconds, headers=headers)
    logger.info(
        "【{}】goofish order pushed source={} order_id={} status={} chat_id={} buyer_id={}",
        cookie_id, source, order_id, order_status, payload["goofishChatId"], payload["buyerId"],
    )


def _extract_image_urls(parsed_message: dict[str, Any]) -> list[str]:
    raw_message = parsed_message.get("raw_message") or {}
    try:
        from common.utils.xianyu_message_parser import decode_first_content, interpret_content

        msg_1 = raw_message.get("1", {}) if isinstance(raw_message, dict) else {}
        msg_6 = msg_1.get("6", {}) if isinstance(msg_1, dict) else {}
        msg_6_3 = msg_6.get("3", {}) if isinstance(msg_6, dict) else {}
        candidates = [msg_6_3.get("5", ""), msg_6_3.get("1", "")] if isinstance(msg_6_3, dict) else []
        decoded = decode_first_content(candidates)
        if decoded is not None:
            _, images, msg_type = interpret_content(decoded)
            if msg_type == "image" and images:
                return images
    except Exception:
        pass

    send_message = str(parsed_message.get("send_message", "") or "")
    urls = [
        line.strip()
        for line in send_message.splitlines()
        if line.strip().lower().startswith(("http://", "https://"))
    ]
    return urls


def _extract_message_id(xianyu_instance: Any, parsed_message: dict[str, Any]) -> str:
    raw_message = parsed_message.get("raw_message") or {}
    handler = getattr(xianyu_instance, "message_handler", None)
    if handler and hasattr(handler, "extract_message_id"):
        try:
            return str(handler.extract_message_id(raw_message) or "")
        except Exception:
            return ""
    return ""


def _clean_unknown(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "unknown" else text


def _event_time_ms(msg_time: Any) -> int:
    if not msg_time:
        return int(time.time() * 1000)
    text = str(msg_time)
    try:
        return int(time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S")) * 1000)
    except ValueError:
        return int(time.time() * 1000)


def _is_duplicate(key: str) -> bool:
    now = time.time()
    expired = [cache_key for cache_key, ts in _dedup_cache.items() if now - ts > _dedup_ttl_seconds]
    for cache_key in expired:
        _dedup_cache.pop(cache_key, None)
    if key in _dedup_cache:
        return True
    _dedup_cache[key] = now
    return False


async def _post_json(
    url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, json=payload, headers=headers or {})
        text = response.text
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = {"success": False, "message": text}
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning("goofish push target returned failure url={} response={}", url, data)
    return data if isinstance(data, dict) else {"success": True, "data": data}
