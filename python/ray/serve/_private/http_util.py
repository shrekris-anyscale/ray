import asyncio
import socket
from dataclasses import dataclass
import inspect
import json
import logging
from typing import Any, Dict, List, Optional, Type

from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.types import Send, ASGIApp
from uvicorn.config import Config
from uvicorn.lifespan.on import LifespanOn

from ray.serve.exceptions import RayServeException
from ray.serve._private.constants import SERVE_LOGGER_NAME


logger = logging.getLogger(SERVE_LOGGER_NAME)


@dataclass
class HTTPRequestWrapper:
    scope: Dict[Any, Any]
    body: bytes


def build_starlette_request(scope, serialized_body: bytes):
    """Build and return a Starlette Request from ASGI payload.

    This function is intended to be used immediately before task invocation
    happens.
    """

    # Simulates receiving HTTP body from TCP socket.  In reality, the body has
    # already been streamed in chunks and stored in serialized_body.
    received = False

    async def mock_receive():
        nonlocal received

        # If the request has already been received, starlette will keep polling
        # for HTTP disconnect. We will pause forever. The coroutine should be
        # cancelled by starlette after the response has been sent.
        if received:
            block_forever = asyncio.Event()
            await block_forever.wait()

        received = True
        return {"body": serialized_body, "type": "http.request", "more_body": False}

    return Request(scope, mock_receive)


class Response:
    """ASGI compliant response class.

    It is expected to be called in async context and pass along
    `scope, receive, send` as in ASGI spec.

    >>> from ray.serve.http_util import Response  # doctest: +SKIP
    >>> scope, receive = ... # doctest: +SKIP
    >>> await Response({"k": "v"}).send(scope, receive, send) # doctest: +SKIP
    """

    def __init__(self, content=None, status_code=200):
        """Construct a HTTP Response based on input type.

        Args:
            content: Any JSON serializable object.
            status_code (int, optional): Default status code is 200.
        """
        self.status_code = status_code
        self.raw_headers = []

        if content is None:
            self.body = b""
            self.set_content_type("text")
        elif isinstance(content, bytes):
            self.body = content
            self.set_content_type("text")
        elif isinstance(content, str):
            self.body = content.encode("utf-8")
            self.set_content_type("text-utf8")
        else:
            # Delayed import since utils depends on http_util
            from ray.serve._private.utils import serve_encoders

            self.body = json.dumps(
                jsonable_encoder(content, custom_encoder=serve_encoders)
            ).encode()
            self.set_content_type("json")

    def set_content_type(self, content_type):
        if content_type == "text":
            self.raw_headers.append([b"content-type", b"text/plain"])
        elif content_type == "text-utf8":
            self.raw_headers.append([b"content-type", b"text/plain; charset=utf-8"])
        elif content_type == "json":
            self.raw_headers.append([b"content-type", b"application/json"])
        else:
            raise ValueError("Invalid content type {}".format(content_type))

    async def send(self, scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        await send({"type": "http.response.body", "body": self.body})


async def receive_http_body(scope, receive, send):
    body_buffer = []
    more_body = True
    while more_body:
        message = await receive()
        assert message["type"] == "http.request"

        more_body = message["more_body"]
        body_buffer.append(message["body"])

    return b"".join(body_buffer)


class RawASGIResponse(ASGIApp):
    """Implement a raw ASGI response interface.

    We have to build this because starlette's base response class is
    still too smart and perform header inference.
    """

    def __init__(self, messages):
        self.messages = messages

    async def __call__(self, scope, receive, send):
        for message in self.messages:
            await send(message)

    @property
    def status_code(self):
        return self.messages[0]["status"]


class BufferedASGISender(Send):
    """Implement the interface for ASGI sender to save data from varisous
    asgi response type (fastapi, starlette, etc.)
    """

    def __init__(self) -> None:
        self.messages = []

    async def __call__(self, message):
        assert message["type"] in ("http.response.start", "http.response.body")
        self.messages.append(message)

    def build_asgi_response(self) -> RawASGIResponse:
        return RawASGIResponse(self.messages)


class ASGIHTTPQueueSender(Send):
    """ASGI sender that enables polling for the sent messages off a queue.

    This class assumes a single consumer of the queue (concurrent calls to
    `get_messages_nowait` and `wait_for_message` may result in undefined behavior).
    """

    def __init__(self):
        self._message_queue = asyncio.Queue()
        self._new_message_event = asyncio.Event()

    async def __call__(self, message: Dict[str, Any]):
        assert message["type"] in ("http.response.start", "http.response.body")
        await self._message_queue.put(message)
        self._new_message_event.set()

    def get_messages_nowait(self) -> List[Dict[str, Any]]:
        """Returns all messages that are currently available (non-blocking).

        At least one message will be present if `wait_for_message` had previously
        returned and a subsequent call to `wait_for_message` blocks until at
        least one new message is available.
        """
        messages = []
        while not self._message_queue.empty():
            messages.append(self._message_queue.get_nowait())

        self._new_message_event.clear()
        return messages

    async def wait_for_message(self):
        """Wait until at least one new message is available.

        If a message is available, this method will return immediately on each call
        until `get_messages_nowait` is called.
        """
        await self._new_message_event.wait()


def make_fastapi_class_based_view(fastapi_app, cls: Type) -> None:
    """Transform the `cls`'s methods and class annotations to FastAPI routes.

    Modified from
    https://github.com/dmontagu/fastapi-utils/blob/master/fastapi_utils/cbv.py

    Usage:
    >>> from fastapi import FastAPI
    >>> app = FastAPI() # doctest: +SKIP
    >>> class A: # doctest: +SKIP
    ...     @app.route("/{i}") # doctest: +SKIP
    ...     def func(self, i: int) -> str: # doctest: +SKIP
    ...         return self.dep + i # doctest: +SKIP
    >>> # just running the app won't work, here.
    >>> make_fastapi_class_based_view(app, A) # doctest: +SKIP
    >>> # now app can be run properly
    """
    # Delayed import to prevent ciruclar imports in workers.
    from fastapi import Depends, APIRouter
    from fastapi.routing import APIRoute

    def get_current_servable_instance():
        from ray import serve

        return serve.get_replica_context().servable_object

    # Find all the class method routes
    class_method_routes = [
        route
        for route in fastapi_app.routes
        if
        # User defined routes must all be APIRoute.
        isinstance(route, APIRoute)
        # We want to find the route that's bound to the `cls`.
        # NOTE(simon): we can't use `route.endpoint in inspect.getmembers(cls)`
        # because the FastAPI supports different routes for the methods with
        # same name. See #17559.
        and (cls.__qualname__ in route.endpoint.__qualname__)
    ]

    # Modify these routes and mount it to a new APIRouter.
    # We need to to this (instead of modifying in place) because we want to use
    # the laster fastapi_app.include_router to re-run the dependency analysis
    # for each routes.
    new_router = APIRouter()
    for route in class_method_routes:
        fastapi_app.routes.remove(route)

        # This block just adds a default values to the self parameters so that
        # FastAPI knows to inject the object when calling the route.
        # Before: def method(self, i): ...
        # After: def method(self=Depends(...), *, i):...
        old_endpoint = route.endpoint
        old_signature = inspect.signature(old_endpoint)
        old_parameters = list(old_signature.parameters.values())
        if len(old_parameters) == 0:
            # TODO(simon): make it more flexible to support no arguments.
            raise RayServeException(
                "Methods in FastAPI class-based view must have ``self`` as "
                "their first argument."
            )
        old_self_parameter = old_parameters[0]
        new_self_parameter = old_self_parameter.replace(
            default=Depends(get_current_servable_instance)
        )
        new_parameters = [new_self_parameter] + [
            # Make the rest of the parameters keyword only because
            # the first argument is no longer positional.
            parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            for parameter in old_parameters[1:]
        ]
        new_signature = old_signature.replace(parameters=new_parameters)
        setattr(route.endpoint, "__signature__", new_signature)
        setattr(route.endpoint, "_serve_cls", cls)
        new_router.routes.append(route)
    fastapi_app.include_router(new_router)

    routes_to_remove = list()
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute):
            continue

        # If there is a response model, FastAPI creates a copy of the fields.
        # But FastAPI creates the field incorrectly by missing the outer_type_.
        if route.response_model:
            route.secure_cloned_response_field.outer_type_ = (
                route.response_field.outer_type_
            )

        # Remove endpoints that belong to other class based views.
        serve_cls = getattr(route.endpoint, "_serve_cls", None)
        if serve_cls is not None and serve_cls != cls:
            routes_to_remove.append(route)
    fastapi_app.routes[:] = [r for r in fastapi_app.routes if r not in routes_to_remove]


def set_socket_reuse_port(sock: socket.socket) -> bool:
    """Mutate a socket object to allow multiple process listening on the same port.

    Returns:
        success: whether the setting was successful.
    """
    try:
        # These two socket options will allow multiple process to bind the the
        # same port. Kernel will evenly load balance among the port listeners.
        # Note: this will only work on Linux.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        # In some Python binary distribution (e.g., conda py3.6), this flag
        # was not present at build time but available in runtime. But
        # Python relies on compiler flag to include this in binary.
        # Therefore, in the absence of socket.SO_REUSEPORT, we try
        # to use `15` which is value in linux kernel.
        # https://github.com/torvalds/linux/blob/master/tools/include/uapi/asm-generic/socket.h#L27
        else:
            sock.setsockopt(socket.SOL_SOCKET, 15, 1)
        return True
    except Exception as e:
        logger.debug(
            f"Setting SO_REUSEPORT failed because of {e}. SO_REUSEPORT is disabled."
        )
        return False


class ASGIAppReplicaWrapper:
    """Provides a common wrapper for replicas running an ASGI app."""

    def __init__(self, app: ASGIApp):
        self._asgi_app = app

        # Use uvicorn's lifespan handling code to properly deal with
        # startup and shutdown event.
        self._serve_asgi_lifespan = LifespanOn(Config(self._asgi_app, lifespan="on"))

        # Replace uvicorn logger with our own.
        self._serve_asgi_lifespan.logger = logger

    async def _run_asgi_lifespan_startup(self):
        # LifespanOn's logger logs in INFO level thus becomes spammy
        # Within this block we temporarily uplevel for cleaner logging
        from ray.serve._private.logging_utils import LoggingContext

        with LoggingContext(self._serve_asgi_lifespan.logger, level=logging.WARNING):
            await self._serve_asgi_lifespan.startup()

    async def __call__(
        self, request: Request, asgi_sender: Optional[Send] = None
    ) -> Optional[ASGIApp]:
        """Calls into the wrapped ASGI app.

        If asgi_sender is provided, it's passed into the app and nothing is
        returned.

        If no asgi_sender is provided, an ASGI response is built and returned.
        """
        build_and_return_response = False
        if asgi_sender is None:
            asgi_sender = BufferedASGISender()
            build_and_return_response = True

        await self._asgi_app(
            request.scope,
            request.receive,
            asgi_sender,
        )

        if build_and_return_response:
            return asgi_sender.build_asgi_response()

    # NOTE: __del__ must be async so that we can run ASGI shutdown
    # in the same event loop.
    async def __del__(self):
        # LifespanOn's logger logs in INFO level thus becomes spammy.
        # Within this block we temporarily uplevel for cleaner logging.
        from ray.serve._private.logging_utils import LoggingContext

        with LoggingContext(self._serve_asgi_lifespan.logger, level=logging.WARNING):
            await self._serve_asgi_lifespan.shutdown()
