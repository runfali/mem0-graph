import logging
import time

from mem0.llms.base import LLMBase

logger = logging.getLogger(__name__)

_MAX_LAYER_RETRIES = 3
_RETRY_SLEEP_SECONDS = 0.5


class FallbackLLM(LLMBase):
    """
    Try a primary LLM then fallbacks in order; switch layer on failure.

    Fast-fail errors (connection errors, HTTP 5xx/401/403) are retried within a
    layer up to ``_MAX_LAYER_RETRIES`` times; timeouts and any other error switch
    to the next layer immediately. Every layer call injects a per-request
    ``timeout`` (ignored by providers that reject it). SDK-level retries on each
    sub-LLM are disabled so a timeout surfaces promptly instead of being retried
    internally by the SDK.
    """

    def __init__(self, primary, fallbacks, layer_timeout=120.0):
        self._llms = [primary, *fallbacks]
        self.layer_timeout = layer_timeout
        for llm in self._llms:
            try:
                llm.client.max_retries = 0
            except AttributeError:
                pass

    def generate_response(self, messages, response_format=None, tools=None, tool_choice="auto", **kwargs):
        last_exc = None
        for idx, llm in enumerate(self._llms):
            start = time.monotonic()
            try:
                result = self._call_layer(llm, messages, response_format, tools, tool_choice, kwargs)
                logger.info("FallbackLLM layer %s succeeded in %.2fs", idx, time.monotonic() - start)
                return result
            except Exception as exc:  # noqa: BLE001 - surface last layer's exception as-is
                last_exc = exc
                logger.warning(
                    "FallbackLLM layer %s failed (%s) in %.2fs",
                    idx,
                    type(exc).__name__,
                    time.monotonic() - start,
                )
                if idx < len(self._llms) - 1:
                    logger.info("FallbackLLM switching to layer %s", idx + 1)
        raise last_exc

    def _call_layer(self, llm, messages, response_format, tools, tool_choice, kwargs):
        for attempt in range(_MAX_LAYER_RETRIES):
            try:
                return self._invoke(llm, messages, response_format, tools, tool_choice, kwargs)
            except Exception as exc:  # noqa: BLE001
                if _is_timeout_error(exc) or not _is_fast_fail(exc):
                    raise
                if attempt == _MAX_LAYER_RETRIES - 1:
                    raise
                time.sleep(_RETRY_SLEEP_SECONDS)

    def _invoke(self, llm, messages, response_format, tools, tool_choice, kwargs):
        layer_kwargs = dict(kwargs)
        layer_kwargs.update({"timeout": self.layer_timeout})
        try:
            return llm.generate_response(
                messages, response_format=response_format, tools=tools, tool_choice=tool_choice, **layer_kwargs
            )
        except TypeError:
            pass
        return llm.generate_response(messages, response_format=response_format, tools=tools, tool_choice=tool_choice, **kwargs)


def _is_timeout_error(exc):
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in str(exc).lower()


def _is_fast_fail(exc):
    if isinstance(exc, (ConnectionError, OSError)):
        return True
    if type(exc).__name__ in ("APIConnectionError", "APIConnectionPoolTimeoutError"):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status >= 500 or status in (401, 403)):
        return True
    return False
