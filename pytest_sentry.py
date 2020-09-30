import os
import pytest
import functools

import wrapt
import sentry_sdk
from sentry_sdk.integrations import Integration

from sentry_sdk import Hub, push_scope, capture_exception


class PytestIntegration(Integration):
    # Right now this integration type is only a carrier for options, and to
    # disable the pytest plugin. `setup_once` is unused.

    identifier = "pytest"

    def __init__(self, always_report=None):
        if always_report is None:
            always_report = os.environ.get(
                "PYTEST_SENTRY_ALWAYS_REPORT", ""
            ).lower() in ("1", "true", "yes")

        self.always_report = always_report

    @staticmethod
    def setup_once():
        pass


class Client(sentry_sdk.Client):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("dsn", os.environ.get("PYTEST_SENTRY_DSN", None))
        kwargs.setdefault("traces_sample_rate", 1.0)
        kwargs.setdefault("_experiments", {}).setdefault(
            "auto_enabling_integrations", True
        )
        kwargs.setdefault("environment", "test")
        kwargs.setdefault("integrations", []).append(PytestIntegration())

        sentry_sdk.Client.__init__(self, *args, **kwargs)


def hookwrapper(itemgetter, **kwargs):
    """
    A version of pytest.hookimpl that sets the current hub to the correct one
    and skips the hook if the integration is disabled.

    Assumes the function is a hookwrapper, ie yields once
    """

    @wrapt.decorator
    def _with_hub(wrapped, instance, args, kwargs):
        item = itemgetter(*args, **kwargs)
        hub = _resolve_hub_marker_value(item.get_closest_marker("sentry_client"))

        if hub.get_integration(PytestIntegration) is None:
            yield
        else:
            with hub:
                gen = wrapped(*args, **kwargs)

            while True:
                try:
                    with hub:
                        y = yield next(gen)
                        gen.send(y)

                except StopIteration:
                    break

    def inner(f):
        return pytest.hookimpl(hookwrapper=True, **kwargs)(_with_hub(f))

    return inner


def pytest_load_initial_conftests(early_config, parser, args):
    early_config.addinivalue_line(
        "markers",
        "sentry_client(client=None): Use this client instance for reporting tests. You can also pass a DSN string directly, or a `Hub` if you need it.",
    )


@hookwrapper(itemgetter=lambda item: item)
def pytest_runtest_protocol(item):
    with sentry_sdk.push_scope() as scope:
        scope.add_event_processor(_process_event)
        yield


@hookwrapper(itemgetter=lambda item: item)
def pytest_runtest_call(item):
    op = "pytest.runtest.call"

    name = item.nodeid

    # Assumption: Parameters are full of unreadable garbage and the test
    # timings are going to be comparable. The product can then identify slowest
    # runs anyway.
    if name.endswith("]"):
        params_start = name.rfind("[")
        if params_start != -1:
            name = name[:params_start]

    with sentry_sdk.start_transaction(op=op, name=u"{} {}".format(op, name)):
        yield


@hookwrapper(itemgetter=lambda fixturedef, request: request._pyfuncitem)
def pytest_fixture_setup(fixturedef, request):
    op = "pytest.fixture.setup"
    with sentry_sdk.start_transaction(
        op=op, name=u"{} {}".format(op, fixturedef.argname)
    ):
        yield


@hookwrapper(tryfirst=True, itemgetter=lambda item, call: item)
def pytest_runtest_makereport(item, call):
    sentry_sdk.set_tag("pytest.result", "pending")
    report = yield
    sentry_sdk.set_tag("pytest.result", report.get_result().outcome)

    if call.when == "call":
        cur_exc_chain = getattr(item, "pytest_sentry_exc_chain", [])

        if call.excinfo is not None:
            item.pytest_sentry_exc_chain = cur_exc_chain = cur_exc_chain + [
                call.excinfo
            ]

        integration = Hub.current.get_integration(PytestIntegration)

        if (cur_exc_chain and call.excinfo is None) or integration.always_report:
            for exc_info in cur_exc_chain:
                capture_exception((exc_info.type, exc_info.value, exc_info.tb))


DEFAULT_HUB = Hub(Client())


def _resolve_hub_marker_value(marker_value):
    if marker_value is None:
        marker_value = DEFAULT_HUB
    else:
        marker_value = marker_value.args[0]

    if callable(marker_value):
        marker_value = marker_value()

    if marker_value is None:
        # user explicitly disabled reporting
        return Hub()

    if isinstance(marker_value, str):
        return Hub(Client(marker_value))

    if isinstance(marker_value, dict):
        return Hub(Client(**marker_value))

    if isinstance(marker_value, Client):
        return Hub(marker_value)

    if isinstance(marker_value, Hub):
        return marker_value

    raise RuntimeError(
        "The `sentry_client` value must be a client, hub or string, not {}".format(
            repr(type(marker_value))
        )
    )


@pytest.fixture
def sentry_test_hub(request):
    """
    Gives back the current hub.
    """

    item = request.node
    return _resolve_hub_marker_value(item.get_closest_marker("sentry_client"))


def _process_event(event, hint):
    if "exception" in event:
        for exception in event["exception"]["values"]:
            if "stacktrace" in exception:
                _process_stacktrace(exception["stacktrace"])

    if "stacktrace" in event:
        _process_stacktrace(event["stacktrace"])

    return event


def _process_stacktrace(stacktrace):
    for frame in stacktrace["frames"]:
        frame["in_app"] = not frame["module"].startswith(
            ("_pytest.", "pytest.", "pluggy.")
        )
