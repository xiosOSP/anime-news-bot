"""Track the first Telegram publication call, excluding media preparation."""
import asyncio
import inspect


class PublicationBot:
    METHODS = frozenset({
        'send_message', 'send_photo', 'send_video', 'send_media_group',
        'send_document', 'send_animation', 'copy_message', 'forward_message',
    })

    def __init__(self, bot, before_send=None):
        self._bot = bot
        self._before_send = before_send
        self._lock = asyncio.Lock()
        self.started = False

    def __getattr__(self, name):
        method = getattr(self._bot, name)
        if name not in self.METHODS:
            return method

        async def send(*args, **kwargs):
            async with self._lock:
                if not self.started:
                    if self._before_send is not None:
                        accepted = self._before_send()
                        if inspect.isawaitable(accepted):
                            accepted = await accepted
                        if not accepted:
                            raise RuntimeError('Publication reservation could not be persisted')
                    self.started = True
            return await method(*args, **kwargs)

        return send
