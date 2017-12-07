"""Worker.

A "worker" starts a single instance of a Faust application.

The Worker is the terminal interface to App, and is the third
entry point in this list:

1) :program:`faust worker`
2) -> :class:`faust.cli.worker.worker`
3) -> :class:`faust.Worker`
4) -> :class:`faust.App`

You can call ``await app.start()`` directly to get a side-effect free
instance that can be embedded in any environment. It won't even emit logs
to the console unless you have configured :mod:`logging` manually.

The worker only concerns itself with the terminal, process
signal handlers, logging, debugging mechanisms, etc.

.. admonition:: Web server

    The Worker also starts the web server, the app will not start it.

.. admonition:: Multiple apps

    If you want your worker to start multiple apps, you would have
    to pass them in with the ``*services`` starargs::

        worker = Worker(app1, app2, app3, app4)

    This way the extra apps will be started together with the main app,
    and the main app of the worker (``worker.app``) will end up being
    the first positional argument (``app1``).

    Note that the web server will only concern itself with the
    main app, so if you want web access to the other apps you have to
    include web servers for them (also passed in as ``*services`` starargs).
"""
import asyncio
import atexit
import logging
import os
import socket
import sys

from collections import defaultdict
from itertools import chain
from pathlib import Path
from typing import (
    Any, Dict, IO, Iterable, Mapping, Sequence, Set, Tuple, Type, Union,
)

from kafka.structs import TopicPartition as _TopicPartition
import mode
from mode import ServiceT, get_logger
from mode.utils.logging import formatter

from .cli._env import BLOCKING_TIMEOUT, DEBUG
from .types import AppT, SensorT, TP, TopicT
from .utils import text
from .utils.imports import SymbolArg, symbol_by_name
from .utils.objects import cached_property
from .web.site import Website as _Website

try:
    # if installed we use this to set ps.name (argv[0])
    from setproctitle import setproctitle
except ImportError:
    def setproctitle(title: str) -> None: ...  # noqa

__all__ = ['Worker']

#: Path to default Web site class.
WEBSITE_CLS = 'faust.web.site:Website'

#: Name prefix of process in ps/top listings.
PSIDENT = '[Faust:Worker]'

TP_TYPES: Tuple[Type, ...] = (TP, _TopicPartition)

logger = get_logger(__name__)


@formatter
def format_log_arguments(arg: Any) -> Any:
    if arg and isinstance(arg, Mapping):
        first_k, first_v = next(iter(arg.items()))
        if (isinstance(first_k, str) and
                isinstance(first_v, set) and
                isinstance(next(iter(first_v), None), TopicT)):
            return '\n' + text.logtable(
                [(k, v) for k, v in arg.items()],
                title='Subscription',
                headers=['Topic', 'Descriptions'],
            )
        elif isinstance(first_v, TP_TYPES):
            return '\n' + text.logtable(
                [(k.topic, k.partition, v) for k, v in arg.items()],
                title='Topic Partition Map',
                headers=['topic', 'partition', 'offset'],
            )
    elif arg and isinstance(arg, (set, list)):
        if isinstance(next(iter(arg)), TP_TYPES):
            topics: Dict[str, Set[int]] = defaultdict(set)
            for tp in arg:
                topics[tp.topic].add(tp.partition)

            return '\n' + text.logtable(
                [(k, repr(v)) for k, v in topics.items()],
                title='Topic Partition Set',
                headers=['topic', 'partitions'],
            )


class Spinner:
    bell = '\b'
    phases: Sequence[str] = ('-', '\\', '|', '/')
    cursor_hide: str = '\x1b[?25l'
    cursor_show: str = '\x1b[?25h'
    hide_cursor: bool = True

    def __init__(self, file: IO = sys.stderr) -> None:
        self.file: IO = file
        self.width: int = 0
        self.count = 0

    def update(self) -> None:
        if not self.count:
            self.begin()
        i = self.count % len(self.phases)
        self.count += 1
        self.write(self.phases[i])

    def write(self, s: str) -> None:
        if self.file.isatty():
            self._print(f'{self.bell * self.width}{s.ljust(self.width)}')
            self.width = max(self.width, len(s))

    def _print(self, s: str) -> None:
        print(s, end='', file=self.file)
        self.file.flush()

    def begin(self) -> None:
        atexit.register(type(self)._finish, self.file, at_exit=True)
        self._print(self.cursor_hide)

    def finish(self) -> None:
        self._finish(self.file)

    @classmethod
    def _finish(cls, file: IO, *, at_exit: bool = False) -> None:
        print(cls.cursor_show, end='', file=file)
        file.flush()


class SpinnerHandler(logging.Handler):
    """A logger handler that iterates our progress spinner for each log."""

    # For every logging call we advance the terminal spinner (-\/-)

    def __init__(self, worker: 'Worker', **kwargs: Any) -> None:
        self.worker = worker
        super().__init__(**kwargs)

    def emit(self, _record: logging.LogRecord) -> None:
        # the spinner is only in effect with WARN level and below.
        if self.worker.spinner:
            self.worker.spinner.update()


class Worker(mode.Worker):
    """Worker.

    Usage:
        You can start a worker using:

            1) the :program:`faust worker` program.

            2) instantiating Worker programmatically and calling
               `execute_from_commandline()`::

                    >>> worker = Worker(app)
                    >>> worker.execute_from_commandline()

            3) or if you already have an event loop, calling ``await start``,
               but in that case *you are responsible for gracefully shutting
               down the event loop*::

                    async def start_worker(worker: Worker) -> None:
                        await worker.start()

                    def manage_loop():
                        loop = asyncio.get_event_loop()
                        worker = Worker(app, loop=loop)
                        try:
                            loop.run_until_complete(start_worker(worker)
                        finally:
                            worker.stop_and_shutdown_loop()

    Arguments:
        app: The Faust app to start.
        *services: Services to start with worker.
            This includes application instances to start.

        sensors (Iterable[SensorT]): List of sensors to include.
        debug (bool): Enables debugging mode [disabled by default].
        quiet (bool): Do not output anything to console [disabled by default].
        loglevel (Union[str, int]): Level to use for logging, can be string
            (one of: CRIT|ERROR|WARN|INFO|DEBUG), or integer.
        logfile (Union[str, IO]): Name of file or a stream to log to.
        logformat (str): Format to use when logging messages.
        stdout (IO): Standard out stream.
        stderr (IO): Standard err stream.
        blocking_timeout (float): When :attr:`debug` is enabled this
            sets the timeout for detecting that the event loop is blocked.
        workdir (Union[str, Path]): Custom working directory for the process
            that the worker will change into when started.
            This working directory change is permanent for the process,
            or until something else changes the working directory again.
        Website: Class used to serve the Faust web site
            (defaults to :class:`faust.web.site.Website`).
        web_port (int): Port for web site to bind to (defaults to 6066).
        web_bind (str): Host to bind web site to (defaults to "0.0.0.0").
        web_host (str): Canonical host name used for this server.
           (defaults to the current host name).
        loop (asyncio.AbstractEventLoop): Custom event loop object.
    """

    logger = logger

    #: The Faust app started by this worker.
    app: AppT

    #: Additional sensors to add to the Faust app.
    sensors: Set[SensorT]

    #: Current working directory.
    #: Note that if passed as an argument to Worker, the worker
    #: will change to this directory when started.
    workdir: Path

    #: Port to run the embedded web server on (defaults to 6066).
    web_port: int

    #: Host to bind web server port to (defaults to '0.0.0.0').
    web_bind: str

    #: Class that starts our web server and serves the Faust website.
    Website: Type[_Website]

    #: Class that displays a terminal progress spinner (see :pypi:`progress`).
    spinner: Spinner

    def __init__(
            self, app: AppT, *services: ServiceT,
            sensors: Iterable[SensorT] = None,
            debug: bool = DEBUG,
            quiet: bool = False,
            loglevel: Union[str, int] = None,
            logfile: Union[str, IO] = None,
            logformat: str = None,
            stdout: IO = sys.stdout,
            stderr: IO = sys.stderr,
            blocking_timeout: float = BLOCKING_TIMEOUT,
            workdir: Union[Path, str] = None,
            Website: SymbolArg[Type[_Website]] = WEBSITE_CLS,
            web_port: int = None,
            web_bind: str = None,
            web_host: str = None,
            loop: asyncio.AbstractEventLoop = None,
            **kwargs: Any) -> None:
        self.app = app
        self.sensors = set(sensors or [])
        self.workdir = Path(workdir or Path.cwd())
        self.Website = symbol_by_name(Website)
        self.web_port = web_port
        self.web_bind = web_bind
        self.web_host = web_host or socket.gethostname()
        super().__init__(
            *services,
            debug=debug,
            quiet=quiet,
            loglevel=loglevel,
            logfile=logfile,
            logformat=logformat,
            stdout=stdout,
            stderr=stderr,
            blocking_timeout=blocking_timeout,
            loop=loop,
            **kwargs)
        self.spinner = Spinner(file=self.stdout)

    async def on_startup_finished(self) -> None:
        # block detection started here after changelog stuff,
        # and blocking RocksDB bulk updates.
        await self.maybe_start_blockdetection()
        if self.spinner:
            self.spinner.finish()
            self.spinner = None
            self.say('ready- ^')
        else:
            self.log.info('Ready')

    def on_init_dependencies(self) -> Iterable[ServiceT]:
        # App service is now a child of worker.
        self.app.beacon.reattach(self.beacon)
        # Transfer sensors to app
        for sensor in self.sensors:
            self.app.sensors.add(sensor)
        # Callback called once the app is running and fully
        # functional, we use it to e.g. print the "ready" message.
        self.app.on_startup_finished = self.on_startup_finished
        return chain([self.website], self.services, [self.app])

    async def on_first_start(self) -> None:
        if self.workdir and Path.cwd().absolute() != self.workdir.absolute():
            os.chdir(Path(self.workdir).absolute())
        if self.app.autodiscover:
            self.app.discover()
        await super().on_first_start()  # <-- sets up logging

    def _setproctitle(self, info: str, *, ident: str = PSIDENT) -> None:
        setproctitle(f'{ident} {info}')

    async def on_execute(self) -> None:
        # This is called as soon as we starts
        self._setproctitle('init')
        self._say('^ ', end='')

    def on_setup_root_logger(self,
                             logger: logging.Logger,
                             level: int) -> None:
        # This is used to set up the terminal progress spinner
        # so that it spins for every log message emitted.
        if level and level < logging.WARN:
            self.spinner = None

        if self.spinner:
            logger.handlers[0].setLevel(level)
            logger.addHandler(
                SpinnerHandler(self, level=logging.DEBUG))
            logger.setLevel(logging.DEBUG)

    @cached_property
    def website(self) -> _Website:
        return self.Website(
            self.app,
            bind=self.web_bind,
            port=self.web_port,
            loop=self.loop,
            beacon=self.beacon,
        )
