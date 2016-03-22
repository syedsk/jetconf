import asyncio
import ssl
from collections import OrderedDict
from colorlog import error, warning as warn, info, debug
from typing import List, Tuple, Dict, Any, Callable

from h2.connection import H2Connection
from h2.events import DataReceived, RequestReceived, RemoteSettingsChanged

import jetconf.http_handlers as handlers
from .config import CONFIG_HTTP, NACM_API_ROOT_data, API_ROOT_data, load_config, print_config
from .nacm import NacmConfig
from .data import JsonDatastore


# Function(method, path) -> bool
HandlerConditionT = Callable[[str, str], bool]


class HandlerList:
    def __init__(self):
        self.handlers = []              # type: List[Tuple[HandlerConditionT, Callable]]
        self.default_handler = None     # type: Callable

    def register_handler(self, condition: HandlerConditionT, handler: Callable):
        self.handlers.append((condition, handler))

    def register_default_handler(self, handler: Callable):
        self.default_handler = handler

    def get_handler(self, method: str, path: str) -> Callable:
        for h in self.handlers:
            if h[0](method, path):
                return h[1]

        return self.default_handler


class H2Protocol(asyncio.Protocol):
    def __init__(self):
        self.conn = H2Connection(client_side=False)
        self.transport = None
        self.reqs_waiting_upload = dict()
        self.client_cert = None     # type: Dict[str, Any]

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        self.conn.initiate_connection()
        self.transport.write(self.conn.data_to_send())
        self.client_cert = self.transport.get_extra_info('peercert')

    def data_received(self, data: bytes):
        events = self.conn.receive_data(data)
        self.transport.write(self.conn.data_to_send())
        for event in events:
            if isinstance(event, RequestReceived):
                # Handle request
                headers = OrderedDict(event.headers)

                http_method = headers[":method"]
                if http_method in ("GET", "DELETE"):
                    # Handle immediately, no need to wait for incoming data
                    self.handle_get_delete(headers, event.stream_id)
                elif http_method in ("PUT", "POST"):
                    # Store headers and wait for data upload
                    self.reqs_waiting_upload[event.stream_id] = headers
                else:
                    warn("Unknown http method \"{}\"".format(headers[":method"]))
            elif isinstance(event, DataReceived):
                self.http_handle_upload(event.data, event.stream_id)
            elif isinstance(event, RemoteSettingsChanged):
                self.conn.acknowledge_settings(event)

    def http_handle_upload(self, data: bytes, stream_id: int):
        try:
            headers = self.reqs_waiting_upload.pop(stream_id)
        except KeyError:
            return

        # Handle PUT, POST
        url_split = headers[":path"].split("?")
        url_path = url_split[0]

        h = h2_handlers.get_handler(headers[":method"], url_path)
        if h:
            h(self, headers, data, stream_id)

    def handle_get_delete(self, headers: OrderedDict, stream_id: int):
        # Handle GET, DELETE
        url_split = headers[":path"].split("?")
        url_path = url_split[0]

        h = h2_handlers.get_handler(headers[":method"], url_path)
        if h:
            h(self, headers, stream_id)


def run():
    global h2_handlers

    # Load configuration
    load_config("jetconf/config.yaml")
    print_config()

    # NACM init
    nacm_data = JsonDatastore("./data", "./data/yang-library-data.json", "NACM data")
    nacm_data.load("jetconf/example-data-nacm.json")

    nacmc = NacmConfig(nacm_data)

    # Datastore init
    ex_datastore = JsonDatastore("./data", "./data/yang-library-data.json", "DNS data")
    ex_datastore.load("jetconf/example-data.json")
    ex_datastore.register_nacm(nacmc)

    # Register HTTP handlers
    api_get_root = handlers.api_root_handler
    api_get = handlers.create_get_api(ex_datastore)
    api_post = handlers.create_post_api(ex_datastore)
    api_put = handlers.create_put_api(ex_datastore)
    api_delete = handlers.create_api_delete(ex_datastore)
    nacm_api_get = handlers.create_get_nacm_api(ex_datastore)
    nacm_api_post = handlers.create_post_nacm_api(ex_datastore)
    nacm_api_put = handlers.create_put_nacm_api(ex_datastore)
    nacm_api_delete = handlers.create_nacm_api_delete(ex_datastore)

    h2_handlers = HandlerList()
    h2_handlers.register_handler(lambda m, p: (m == "GET") and (p == CONFIG_HTTP["API_ROOT"]), api_get_root)
    h2_handlers.register_handler(lambda m, p: (m == "GET") and (p.startswith(API_ROOT_data)), api_get)
    h2_handlers.register_handler(lambda m, p: (m == "POST") and (p.startswith(API_ROOT_data)), api_post)
    h2_handlers.register_handler(lambda m, p: (m == "PUT") and (p.startswith(API_ROOT_data)), api_put)
    h2_handlers.register_handler(lambda m, p: (m == "DELETE") and (p.startswith(API_ROOT_data)), api_delete)

    h2_handlers.register_handler(lambda m, p: (m == "GET") and (p == CONFIG_HTTP["NACM_API_ROOT"]), api_get_root)
    h2_handlers.register_handler(lambda m, p: (m == "GET") and (p.startswith(NACM_API_ROOT_data)), nacm_api_get)
    h2_handlers.register_handler(lambda m, p: (m == "POST") and (p.startswith(NACM_API_ROOT_data)), nacm_api_post)
    h2_handlers.register_handler(lambda m, p: (m == "PUT") and (p.startswith(NACM_API_ROOT_data)), nacm_api_put)
    h2_handlers.register_handler(lambda m, p: (m == "DELETE") and (p.startswith(NACM_API_ROOT_data)), nacm_api_delete)

    h2_handlers.register_handler(lambda m, p: m == "GET", handlers.get_file)

    # HTTP server init
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.options |= (ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_COMPRESSION)
    ssl_context.load_cert_chain(certfile=CONFIG_HTTP["SERVER_SSL_CERT"], keyfile=CONFIG_HTTP["SERVER_SSL_PRIVKEY"])
    try:
        ssl_context.set_alpn_protocols(["h2"])
    except AttributeError:
        info("Python not compiled with ALPN support, using NPN instead.")
        ssl_context.set_npn_protocols(["h2"])
    ssl_context.verify_mode = ssl.CERT_REQUIRED
    ssl_context.load_verify_locations(cafile=CONFIG_HTTP["CA_CERT"])

    loop = asyncio.get_event_loop()

    # Each client connection will create a new protocol instance
    listener = loop.create_server(H2Protocol, "127.0.0.1", CONFIG_HTTP["PORT"], ssl=ssl_context)
    server = loop.run_until_complete(listener)

    info("Server started on {}".format(server.sockets[0].getsockname()))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    server.close()
    loop.run_until_complete(server.wait_closed())
    loop.close()
