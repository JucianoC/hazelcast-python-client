import logging
import sys
import threading
import time
from Queue import Queue

import functools

from hazelcast.exception import create_exception, HazelcastInstanceNotActiveError, is_retryable_error, TimeoutError
from hazelcast.protocol.client_message import LISTENER_FLAG
from hazelcast.protocol.custom_codec import EXCEPTION_MESSAGE_TYPE, ErrorCodec
from hazelcast.util import AtomicInteger

INVOCATION_TIMEOUT = 120
RETRY_WAIT_TIME_IN_SECONDS = 1


class Future(object):
    _result = None
    _exception = None
    logger = logging.getLogger("Future")

    def __init__(self):
        self._callbacks = []
        self._event = threading.Event()
        pass

    def set_result(self, result):
        self._result = result
        self._event.set()

        self._invoke_callbacks()

    def set_exception(self, exception):
        self._exception = exception
        self._event.set()
        self._invoke_callbacks()

    def result(self):
        self._event.wait()
        if self._exception:
            raise self._exception
        return self._result

    def done(self):
        return self._event.isSet()

    def running(self):
        return not self.done()

    def exception(self):
        self._event.wait()
        return self._exception

    def add_done_callback(self, callback):
        self._callbacks.append(callback)
        if self.done():
            self._invoke_cb(callback)

    def _invoke_callbacks(self):
        for callback in self._callbacks:
            self._invoke_cb(callback)

    def _invoke_cb(self, callback):
        try:
            callback(self)
        except:
            logging.exception("Exception when invoking callback")

    def continue_with(self, continuation_func):
        """
        Create a continuation that executes when the future is completed
        :param continuation_func: A function which takes the future as the only parameter. Return value of the function
        will be set as the result of the continuation future
        :return: A new future which will be completed when the continuation is done
        """
        future = Future()

        def callback(f):
            try:
                future.set_result(continuation_func(f))
            except:
                future.set_exception(sys.exc_info()[1])

        self.add_done_callback(callback)
        return future


class Invocation(object):
    sent_connection = None
    timer = None

    def __init__(self, request, partition_id=-1, address=None, connection=None, event_handler=None,
                 timeout=INVOCATION_TIMEOUT):
        self._event = threading.Event()
        self.timeout = timeout + time.time()
        self.address = address
        self.connection = connection
        self.partition_id = partition_id
        self.request = request
        self.event_handler = event_handler
        self.future = Future()

    def has_connection(self):
        return self.connection is not None

    def has_event_handler(self):
        return self.event_handler is not None

    def has_partition_id(self):
        return self.partition_id >= 0

    def has_address(self):
        return self.address is not None

    def set_response(self, response):
        if self.timer:
            self.timer.cancel()
        self.future.set_result(response)

    def set_exception(self, exception):
        if self.timer:
            self.timer.cancel()
        self.future.set_exception(exception)

    def on_timeout(self):
        self.set_exception(TimeoutError("Request timed out after %d seconds." % INVOCATION_TIMEOUT))


class InvocationService(object):
    logger = logging.getLogger("InvocationService")

    def __init__(self, client):
        self._pending = {}
        self._listeners = {}
        self._next_correlation_id = AtomicInteger(1)
        self._client = client
        self._event_queue = Queue()
        self._is_redo_operation = False  # TODO: set from config

    def handle_event(self, invocation, message):
        try:
            invocation.event_handler(message)
        except:
            self.logger.warn("Error handling event %s", message, exc_info=True)

    def invoke_on_connection(self, message, connection, event_handler=None):
        return self._invoke(Invocation(message, connection=connection, event_handler=event_handler))

    def invoke_on_partition(self, message, partition_id, event_handler=None):
        return self._invoke(Invocation(message, partition_id=partition_id, event_handler=event_handler))

    def invoke_on_random_target(self, message, event_handler=None):
        return self._invoke(Invocation(message, event_handler=event_handler))

    def invoke_on_target(self, message, address, event_handler=None):
        return self._invoke(Invocation(message, address=address, event_handler=event_handler))

    def _invoke(self, invocation):
        if invocation.has_connection():
            self._send(invocation, invocation.connection)
        elif invocation.has_partition_id():
            addr = self._client.partition_service.get_partition_owner(invocation.partition_id)
            self._send_to_address(invocation, addr)
        elif invocation.has_address():
            self._send_to_address(invocation, invocation.address)
        else:  # send to random address
            addr = self._client.load_balancer.next_address()
            self._send_to_address(invocation, addr)

        return invocation

    def _send_to_address(self, invocation, address):
        conn = self._client.connection_manager.get_or_connect(address)
        self._send(invocation, conn)

    def _send(self, invocation, connection):
        correlation_id = self._next_correlation_id.increment_and_get()
        message = invocation.request
        message.set_correlation_id(correlation_id)
        message.set_partition_id(invocation.partition_id)
        self._pending[correlation_id] = invocation
        if not invocation.timer:
            invocation.timer = self._client.reactor.add_timer_absolute(invocation.timeout, invocation.on_timeout)

        if invocation.has_event_handler():
            self._listeners[correlation_id] = invocation

        self.logger.debug("Sending %s", message)

        if not connection.callback:
            connection.callback = self.handle_client_message
        connection.send_message(message)

    def handle_client_message(self, message):
        correlation_id = message.get_correlation_id()
        self.logger.debug("Received %s", message)
        if message.has_flags(LISTENER_FLAG):
            if correlation_id not in self._listeners:
                self.logger.warn("Got event message with unknown correlation id: %s", message)
                return
            invocation = self._listeners[correlation_id]
            self.handle_event(invocation, message)
            return
        if correlation_id not in self._pending:
            self.logger.warn("Got message with unknown correlation id: %s", message)
            return
        invocation = self._pending.pop(correlation_id)

        if message.get_message_type() == EXCEPTION_MESSAGE_TYPE:
            error = create_exception(ErrorCodec(message))
            return self.handle_exception(invocation, error)

        invocation.set_response(message)

    def handle_exception(self, invocation, error):
        self.logger.debug("Got exception for request %s: %s", invocation.request,
                          error)
        if isinstance(error, (IOError, HazelcastInstanceNotActiveError)):
            if self.try_retry(invocation):
                return

        if is_retryable_error(error):
            if invocation.request.is_retryable() or self._is_redo_operation:
                if self.try_retry(invocation):
                    return

        invocation.set_exception(error)

    def try_retry(self, invocation):
        if invocation.connection:
            return False
        if invocation.timeout < time.time():
            return False

        invoke_func = functools.partial(self._invoke, invocation)
        self.logger.debug("Rescheduling request %s to be retried in %s seconds", invocation.request,
                          RETRY_WAIT_TIME_IN_SECONDS)
        self._client.reactor.add_timer(RETRY_WAIT_TIME_IN_SECONDS, invoke_func)
        return True

    def connection_closed(self, connection):
        for correlation_id, invocation in dict(self._pending).iteritems():
            if invocation.connection == connection:
                invocation.set_exception(IOError("Connection to server was closed."))
                # fail invocation

        for correlation_id, invocation in dict(self._listeners).iteritems():
            if invocation.connection == connection:
                self._listeners.pop(correlation_id)
                # TODO: re-register listener
