"""Implementation of the invocation-side open-tracing interceptor."""

import sys
import logging
import time

from six import iteritems

import grpc
from grpc_opentracing import grpcext, ClientRequestAttribute
from grpc_opentracing._utilities import get_method_type, get_deadline_millis,\
    log_or_wrap_request_or_iterator
import opentracing


class _OpenTracingRendezvous(grpc.Future, grpc.Call):

  def __init__(self, rendezvous, method, tracer, log_payloads):
    self._rendezvous = rendezvous
    self._method = method
    self._tracer = tracer
    self._log_payloads = log_payloads

  def _start_span(self):
    span_context = None
    error = None

    # The `trailing_metadata` method
    #   http://www.grpc.io/grpc/python/grpc.html#grpc.Call.trailing_metadata
    # can block. Since we want the span duration to reflect how long the
    # invocation-side has spent waiting for a response, we thus record the
    # time before calling `trailing_metadata` and use that as the starting time
    # for the span
    start_time = time.time()
    metadata = self._rendezvous.trailing_metadata()
    try:
      if metadata:
        span_context = self._tracer.extract(opentracing.Format.HTTP_HEADERS,
                                            dict(metadata))
    except (opentracing.UnsupportedFormatException,
            opentracing.InvalidCarrierException,
            opentracing.SpanContextCorruptedException) as e:
      logging.exception('tracer.extract() failed')
      error = e
    tags = {'component': 'grpc', 'span.kind': 'client'}
    references = None
    if span_context is not None:
      references = [opentracing.follows_from(span_context)]
    span = self._tracer.start_span(
        operation_name=self._method,
        references=references,
        tags=tags,
        start_time=start_time)
    if error is not None:
      span.log_kv({'event': 'error', 'error.object': error})
    return span

  def cancel(self, *args, **kwargs):
    return self._rendezvous.cancel(*args, **kwargs)

  def cancelled(self, *args, **kwargs):
    return self._rendezvous.cancelled(*args, **kwargs)

  def running(self, *args, **kwargs):
    return self._rendezvous.running(*args, **kwargs)

  def done(self, *args, **kwargs):
    return self._rendezvous.done(*args, **kwargs)

  def result(self, timeout=None):
    with self._start_span() as span:
      try:
        response = self._rendezvous.result(timeout)
        if self._log_payloads:
          span.log_kv({'response': response})
        return response
      except:
        e = sys.exc_info()[0]
        span.set_tag('error', True)
        span.log_kv({'event': 'error', 'error.object': e})
        raise

  def exception(self, *args, **kwargs):
    return self._rendezvous.exception(*args, **kwargs)

  def traceback(self, *args, **kwargs):
    return self._rendezvous.traceback(*args, **kwargs)

  def add_callback(self, *args, **kwargs):
    return self._rendezvous.add_callback(*args, **kwargs)

  def add_done_callback(self, *args, **kwargs):
    return self._rendezvous.add_done_callback(*args, **kwargs)

  def is_active(self, *args, **kwargs):
    return self._rendezvous.is_active(*args, **kwargs)

  def time_remaining(self, *args, **kwargs):
    return self._rendezvous.time_remaining(*args, **kwargs)

  def initial_metadata(self, *args, **kwargs):
    return self._rendezvous.initial_metadata(*args, **kwargs)

  def trailing_metadata(self, *args, **kwargs):
    return self._rendezvous.trailing_metadata(*args, **kwargs)

  def code(self, *args, **kwargs):
    return self._rendezvous.code(*args, **kwargs)

  def details(self, *args, **kwargs):
    return self._rendezvous.details(*args, **kwargs)


def _inject_span_context(tracer, span, metadata):
  headers = {}
  try:
    tracer.inject(span.context, opentracing.Format.HTTP_HEADERS, headers)
  except (opentracing.UnsupportedFormatException,
          opentracing.InvalidCarrierException,
          opentracing.SpanContextCorruptedException) as e:
    logging.exception('tracer.inject() failed')
    span.log_kv({'event': 'error', 'error.object': e})
    return metadata
  metadata = () if metadata is None else tuple(metadata)
  return metadata + tuple(iteritems(headers))


def _wrap_result(tracer, span, method, log_payloads, result):
  # If the RPC is called asynchronously, wrap the future so that an
  # additional span can be created once it's realized.
  if isinstance(result, grpc.Future):
    return _OpenTracingRendezvous(result, method, tracer, log_payloads)
  elif log_payloads:
    response = result
    # Handle the case when the RPC is initiated via the with_call
    # method and the result is a tuple with the first element as the
    # response.
    # http://www.grpc.io/grpc/python/grpc.html#grpc.UnaryUnaryMultiCallable.with_call
    if isinstance(result, tuple):
      response = result[0]
    span.log_kv({'response': response})
  return result


class OpenTracingClientInterceptor(grpcext.UnaryClientInterceptor,
                                   grpcext.StreamClientInterceptor):

  def __init__(self, tracer, active_span_source, log_payloads,
               traced_attributes):
    self._tracer = tracer
    self._active_span_source = active_span_source
    self._log_payloads = log_payloads
    self._traced_attributes = traced_attributes

  def _start_span(self, method, metadata, is_client_stream, is_server_stream,
                  timeout):
    active_span_context = None
    if self._active_span_source is not None:
      active_span = self._active_span_source.get_active_span()
      if active_span is not None:
        active_span_context = active_span.context
    tags = {'component': 'grpc', 'span.kind': 'client'}
    for traced_attribute in self._traced_attributes:
      if traced_attribute == ClientRequestAttribute.HEADERS:
        tags['grpc.headers'] = str(metadata)
      elif traced_attribute == ClientRequestAttribute.METHOD_TYPE:
        tags['grpc.method_type'] = get_method_type(is_client_stream,
                                                   is_server_stream)
      elif traced_attribute == ClientRequestAttribute.METHOD_NAME:
        tags['grpc.method_name'] = method
      elif traced_attribute == ClientRequestAttribute.DEADLINE:
        tags['grpc.deadline_millis'] = get_deadline_millis(timeout)
      else:
        logging.warning('OpenTracing Attribute \"%s\" is not supported',
                        str(traced_attribute))
    return self._tracer.start_span(
        operation_name=method, child_of=active_span_context, tags=tags)

  def intercept_unary(self, request, metadata, client_info, invoker):
    with self._start_span(client_info.full_method, metadata, False, False,
                          client_info.timeout) as span:
      metadata = _inject_span_context(self._tracer, span, metadata)
      if self._log_payloads:
        span.log_kv({'request': request})
      try:
        result = invoker(request, metadata)
      except:
        e = sys.exc_info()[0]
        span.set_tag('error', True)
        span.log_kv({'event': 'error', 'error.object': e})
        raise
      return _wrap_result(self._tracer, span, client_info.full_method,
                          self._log_payloads, result)

  # For RPCs that stream responses, the result can be a generator. To record
  # the span across the generated responses and detect any errors, we wrap the
  # result in a new generator that yields the response values.
  def _intercept_server_stream(self, request_or_iterator, metadata, client_info,
                               invoker):
    with self._start_span(client_info.full_method, metadata,
                          client_info.is_client_stream, True,
                          client_info.timeout) as span:
      metadata = _inject_span_context(self._tracer, span, metadata)
      if self._log_payloads:
        request_or_iterator = log_or_wrap_request_or_iterator(
            span, client_info.is_client_stream, request_or_iterator)
      try:
        result = invoker(request_or_iterator, metadata)
        for response in result:
          if self._log_payloads:
            span.log_kv({'response': response})
          yield response
      except:
        e = sys.exc_info()[0]
        span.set_tag('error', True)
        span.log_kv({'event': 'error', 'error.object': e})
        raise

  def intercept_stream(self, request_or_iterator, metadata, client_info,
                       invoker):
    if client_info.is_server_stream:
      return self._intercept_server_stream(request_or_iterator, metadata,
                                           client_info, invoker)
    with self._start_span(client_info.full_method, metadata,
                          client_info.is_client_stream, False,
                          client_info.timeout) as span:
      metadata = _inject_span_context(self._tracer, span, metadata)
      try:
        result = invoker(request_or_iterator, metadata)
      except:
        e = sys.exc_info()[0]
        span.set_tag('error', True)
        span.log_kv({'event': 'error', 'error.object': e})
        raise
      return _wrap_result(self._tracer, span, client_info.full_method, False,
                          result)
