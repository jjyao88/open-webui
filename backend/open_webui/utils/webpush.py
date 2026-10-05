from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

from pywebpush import WebPushException, webpush_async

from open_webui.models.users import Users

log = logging.getLogger(__name__)

WEB_PUSH_VAPID_PUBLIC_KEY = os.environ.get('WEB_PUSH_VAPID_PUBLIC_KEY', '').strip()
WEB_PUSH_VAPID_PRIVATE_KEY = os.environ.get('WEB_PUSH_VAPID_PRIVATE_KEY', '').strip()
WEB_PUSH_VAPID_SUBJECT = os.environ.get('WEB_PUSH_VAPID_SUBJECT', 'mailto:admin@example.com').strip()

DEFAULT_WEB_PUSH_EVENTS = {
    'chat.finished',
    'chat.failed',
    'channel.message',
    'calendar.alert',
}


def web_push_config() -> dict[str, Any]:
    enabled = bool(WEB_PUSH_VAPID_PUBLIC_KEY and WEB_PUSH_VAPID_PRIVATE_KEY and WEB_PUSH_VAPID_SUBJECT)
    return {
        'enabled': enabled,
        'application_server_key': WEB_PUSH_VAPID_PUBLIC_KEY if enabled else None,
    }


def _subscription_id(endpoint: str) -> str:
    digest = hashlib.sha256(endpoint.encode('utf-8')).hexdigest()[:20]
    return f'webpush-{digest}'


def _validate_subscription(subscription: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(subscription.get('endpoint') or '').strip()
    keys = subscription.get('keys') or {}
    p256dh = str(keys.get('p256dh') or '').strip()
    auth = str(keys.get('auth') or '').strip()

    parsed = urlparse(endpoint)
    if parsed.scheme != 'https' or not parsed.netloc:
        raise ValueError('Invalid Web Push endpoint')
    if not p256dh or not auth:
        raise ValueError('Web Push subscription keys are required')

    return {
        'endpoint': endpoint,
        'expirationTime': subscription.get('expirationTime'),
        'keys': {
            'p256dh': p256dh,
            'auth': auth,
        },
    }


async def _load_notifications(user_id: str) -> dict[str, Any]:
    user = await Users.get_user_by_id(user_id)
    if not user:
        raise ValueError('User not found')

    settings = getattr(user, 'settings', None)
    settings = settings.model_dump(exclude_none=True) if hasattr(settings, 'model_dump') else dict(settings or {})
    return dict(settings.get('notifications') or {})


async def upsert_web_push_subscription(user_id: str, subscription: dict[str, Any]) -> dict[str, Any]:
    if not web_push_config()['enabled']:
        raise ValueError('Web Push is not configured')

    normalized = _validate_subscription(subscription)
    notifications = await _load_notifications(user_id)
    subscriptions = [item for item in notifications.get('web_push_subscriptions') or [] if isinstance(item, dict)]

    now = int(time.time())
    item_id = _subscription_id(normalized['endpoint'])
    item = {
        'id': item_id,
        'subscription': normalized,
        'events': sorted(DEFAULT_WEB_PUSH_EVENTS),
        'delivery': 'away',
        'created_at': now,
        'updated_at': now,
    }

    replaced = False
    for index, existing in enumerate(subscriptions):
        if existing.get('id') == item_id:
            item['created_at'] = int(existing.get('created_at') or now)
            subscriptions[index] = item
            replaced = True
            break

    if not replaced:
        subscriptions.append(item)

    notifications['web_push_subscriptions'] = subscriptions
    await Users.update_user_settings_by_id(user_id, {'notifications': notifications})
    return {'ok': True, 'id': item_id}


async def delete_web_push_subscription(user_id: str, endpoint: str) -> bool:
    endpoint = str(endpoint or '').strip()
    if not endpoint:
        return False

    notifications = await _load_notifications(user_id)
    subscriptions = [item for item in notifications.get('web_push_subscriptions') or [] if isinstance(item, dict)]
    item_id = _subscription_id(endpoint)
    next_subscriptions = [item for item in subscriptions if item.get('id') != item_id]

    if len(next_subscriptions) == len(subscriptions):
        return False

    notifications['web_push_subscriptions'] = next_subscriptions
    await Users.update_user_settings_by_id(user_id, {'notifications': notifications})
    return True


def _event_payload(app_name: str, event: Any) -> dict[str, Any]:
    data = event.data or {}
    event_name = str(event.event or '')

    if event_name == 'chat.finished':
        title = str(data.get('title') or event.message or 'Chat finished')
        body = str(data.get('message') or '')
        url = str(data.get('url') or '')
        chat_id = str(data.get('chat_id') or '')
        if not url and chat_id:
            url = f'/c/{chat_id}'
    elif event_name == 'chat.failed':
        title = str(event.message or 'Chat failed')
        body = str(data.get('message') or '')
        url = str(data.get('url') or '')
    elif event_name == 'channel.message':
        title = str(data.get('title') or event.message or 'Channel message')
        body = str(data.get('content') or data.get('message') or '')
        url = str(data.get('url') or '')
    elif event_name == 'calendar.alert':
        title = str(data.get('title') or event.message or 'Calendar alert')
        starts_in = str(data.get('starts_in') or '')
        body = f'Starting {starts_in}'.strip()
        url = '/calendar'
    else:
        title = str(event.message or event_name or 'Notification')
        body = str(data.get('message') or data.get('preview') or data.get('content_preview') or '')
        url = str(data.get('url') or '')

    return {
        'title': f'{title} / {app_name}',
        'body': body,
        'url': url or '/',
        'icon': '/static/favicon.png',
        'tag': f'open-webui:{event_name}',
        'event': event_name,
    }


async def dispatch_web_push_event(user_id: str, app_name: str, event: Any, is_active: bool) -> None:
    if is_active or event.event not in DEFAULT_WEB_PUSH_EVENTS:
        return

    config = web_push_config()
    if not config['enabled']:
        return

    notifications = await _load_notifications(user_id)
    subscriptions = [item for item in notifications.get('web_push_subscriptions') or [] if isinstance(item, dict)]
    if not subscriptions:
        return

    payload = json.dumps(_event_payload(app_name, event), ensure_ascii=False)
    stale_ids: set[str] = set()

    for item in subscriptions:
        if event.event not in (item.get('events') or DEFAULT_WEB_PUSH_EVENTS):
            continue

        subscription = item.get('subscription') or {}
        try:
            await webpush_async(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=WEB_PUSH_VAPID_PRIVATE_KEY,
                vapid_claims={'sub': WEB_PUSH_VAPID_SUBJECT},
                ttl=300,
            )
        except WebPushException as exc:
            if exc.status_code in (404, 410):
                stale_ids.add(str(item.get('id') or ''))
            else:
                log.warning('Web Push delivery failed for user %s: %s', user_id, exc)
        except Exception:
            log.exception('Web Push delivery failed for user %s', user_id)

    if stale_ids:
        notifications['web_push_subscriptions'] = [
            item for item in subscriptions if str(item.get('id') or '') not in stale_ids
        ]
        await Users.update_user_settings_by_id(user_id, {'notifications': notifications})
