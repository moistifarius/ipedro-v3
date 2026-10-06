"""Outgoing-message filter for bots that aren't Dale.

Installed on a plain-flavor bot's Telegram session (see bot.build_runtime):
every message and caption it sends passes through identity.plainify, so a
canned line written in Dale's voice never reaches a chat in another bot's
name. One chokepoint instead of a flavor switch at every site, which is how
it stayed broken: each new canned line was a new chance to forget one.
"""

from __future__ import annotations

from aiogram.client.session.middlewares.base import BaseRequestMiddleware

from ipedro.identity import plainify

_TEXT_FIELDS = ("text", "caption")


class PlainFlavorMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        update = {}
        for field in _TEXT_FIELDS:
            value = getattr(method, field, None)
            if isinstance(value, str):
                cleaned = plainify(value)
                if cleaned != value:
                    update[field] = cleaned
        if update:
            method = method.model_copy(update=update)
        return await make_request(bot, method)
