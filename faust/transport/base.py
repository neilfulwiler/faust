import asyncio
import faust
import weakref
from itertools import count
from typing import (
    Awaitable, Callable, Iterable, NamedTuple, Optional, List, cast,
)
from ..event import Event
from ..types import ConsumerCallback, Topic
from ..utils.service import Service

CLIENT_ID = 'faust-{0}'.format(faust.__version__)


class MessageTag(NamedTuple):
    consumer_id: int
    offset: int


class EventRef(weakref.ref):

    def __init__(self, event: Event,
                 callback: Callable = None,
                 tag: MessageTag = None) -> None:
        super().__init__(event, callback)
        self.tag = tag


class Consumer(Service):
    id: int
    topic: Topic
    client_id = CLIENT_ID
    transport: 'Transport'

    commit_interval = 30.0

    #: This counter generates new consumer ids.
    _consumer_ids = count(0)

    _dirty_events = List[EventRef]

    def __init__(self, transport: 'Transport',
                 *,
                 topic: Topic = None,
                 callback: ConsumerCallback = None) -> None:
        assert callback is not None
        self.id = next(self._consumer_ids)
        self.transport = transport
        self.callback = callback
        self.topic = topic
        if self.topic.topics and self.topic.pattern:
            raise TypeError('Topic can specify either topics or pattern')
        self._dirty_events = []
        super().__init__(loop=self.transport.loop)

    async def _commit(self, offset: int) -> None:
        raise NotImplementedError()

    def track_event(self, event: Event, offset: int) -> None:
        self._dirty_events.append(
            EventRef(
                event, self.on_event_ready,
                tag=MessageTag(self.id, offset),
            ),
        )

    def on_event_ready(self, ref: EventRef) -> None:
        print('ACKED MESSAGE %r' % (ref.tag,))

    async def register_timers(self) -> None:
        asyncio.ensure_future(self._commit_handler(), loop=self.loop)

    async def _commit_handler(self) -> None:
        asyncio.sleep(self.commit_interval)
        while 1:
            offsets = list(self._current_offsets())
            if offsets:
                print('COMMITTING %r' % (offsets[-1],))
                await self._commit(offsets[-1])
            await asyncio.sleep(self.commit_interval)

    def _current_offsets(self) -> Iterable[int]:
        for ref in self._dirty_events:
            if ref() is None:
                yield ref.tag.offset
            break


class Producer(Service):
    client_id = CLIENT_ID
    transport: 'Transport'

    def __init__(self, transport: 'Transport') -> None:
        self.transport = transport
        super().__init__(loop=self.transport.loop)

    async def send(
            self,
            topic: str,
            key: Optional[bytes],
            value: bytes) -> Awaitable:
        raise NotImplementedError()

    async def send_and_wait(
            self,
            topic: str,
            key: Optional[bytes],
            value: bytes) -> Awaitable:
        raise NotImplementedError()


# We make aliases here, as mypy is confused by the class variables below.
_ProducerT = Producer
_ConsumerT = Consumer


class Transport:
    Consumer: type
    Producer: type

    url: str
    loop: asyncio.AbstractEventLoop

    def __init__(self,
                 url: str = None,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.url = url
        self.loop = loop

    def create_consumer(self, topic: Topic, callback: ConsumerCallback,
                        **kwargs) -> _ConsumerT:
        return cast(_ConsumerT, self.Consumer(
            self, topic=topic, callback=callback, **kwargs))

    def create_producer(self, **kwargs) -> _ProducerT:
        return cast(_ProducerT, self.Producer(self, **kwargs))
