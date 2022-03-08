from collections import defaultdict
from concurrent.futures.thread import ThreadPoolExecutor
import functools
import os
import sys
import time

from pydantic.error_wrappers import ValidationError
import pytest
import requests

import ray
from ray._private.test_utils import SignalActor, wait_for_condition
from ray import serve
from ray.serve.exceptions import RayServeException
from ray.serve.utils import get_random_letters

from ray.serve.application import Application


@pytest.mark.parametrize("use_handle", [True, False])
def test_deploy(serve_instance, use_handle):
    @serve.deployment(version="1")
    def d(*args):
        return f"1|{os.getpid()}"

    def call():
        if use_handle:
            ret = ray.get(d.get_handle().remote())
        else:
            ret = requests.get("http://localhost:8000/d").text

        return ret.split("|")[0], ret.split("|")[1]

    d.deploy()
    val1, pid1 = call()
    assert val1 == "1"

    # Redeploying with the same version and code should do nothing.
    d.deploy()
    val2, pid2 = call()
    assert val2 == "1"
    assert pid2 == pid1

    # Redeploying with a new version should start a new actor.
    d.options(version="2").deploy()
    val3, pid3 = call()
    assert val3 == "1"
    assert pid3 != pid2

    @serve.deployment(version="2")
    def d(*args):
        return f"2|{os.getpid()}"

    # Redeploying with the same version and new code should do nothing.
    d.deploy()
    val4, pid4 = call()
    assert val4 == "1"
    assert pid4 == pid3

    # Redeploying with new code and a new version should start a new actor
    # running the new code.
    d.options(version="3").deploy()
    val5, pid5 = call()
    assert val5 == "2"
    assert pid5 != pid4


def test_empty_decorator(serve_instance):
    @serve.deployment
    def func(*args):
        return "hi"

    @serve.deployment
    class Class:
        def ping(self, *args):
            return "pong"

    assert func.name == "func"
    assert Class.name == "Class"
    func.deploy()
    Class.deploy()

    assert ray.get(func.get_handle().remote()) == "hi"
    assert ray.get(Class.get_handle().ping.remote()) == "pong"


@pytest.mark.parametrize("use_handle", [True, False])
def test_deploy_no_version(serve_instance, use_handle):
    name = "test"

    @serve.deployment(name=name)
    def v1(*args):
        return f"1|{os.getpid()}"

    def call():
        if use_handle:
            ret = ray.get(v1.get_handle().remote())
        else:
            ret = requests.get(f"http://localhost:8000/{name}").text

        return ret.split("|")[0], ret.split("|")[1]

    v1.deploy()
    val1, pid1 = call()
    assert val1 == "1"

    @serve.deployment(name=name)
    def v2(*args):
        return f"2|{os.getpid()}"

    # Not specifying a version tag should cause it to always be updated.
    v2.deploy()
    val2, pid2 = call()
    assert val2 == "2"
    assert pid2 != pid1

    v2.deploy()
    val3, pid3 = call()
    assert val3 == "2"
    assert pid3 != pid2

    # Specifying the version should stop updates from happening.
    v2.options(version="1").deploy()
    val4, pid4 = call()
    assert val4 == "2"
    assert pid4 != pid3

    v2.options(version="1").deploy()
    val5, pid5 = call()
    assert val5 == "2"
    assert pid5 == pid4


@pytest.mark.parametrize("use_handle", [True, False])
def test_deploy_prev_version(serve_instance, use_handle):
    name = "test"

    @serve.deployment(name=name)
    def v1(*args):
        return f"1|{os.getpid()}"

    def call():
        if use_handle:
            ret = ray.get(v1.get_handle().remote())
        else:
            ret = requests.get(f"http://localhost:8000/{name}").text

        return ret.split("|")[0], ret.split("|")[1]

    # Deploy with prev_version specified, where there is no existing deployment
    with pytest.raises(ValueError):
        v1.options(version="1", prev_version="0").deploy()

    v1.deploy()
    val1, pid1 = call()
    assert val1 == "1"

    @serve.deployment(name=name)
    def v2(*args):
        return f"2|{os.getpid()}"

    # Deploying without specifying prev_version should still be possible.
    v2.deploy()
    val2, pid2 = call()
    assert val2 == "2"
    assert pid2 != pid1

    v2.options(version="1").deploy()
    val3, pid3 = call()
    assert val3 == "2"
    assert pid3 != pid2

    @serve.deployment(name=name)
    def v3(*args):
        return f"3|{os.getpid()}"

    # If prev_version does not match with the existing version, it should fail.
    with pytest.raises(ValueError):
        v3.options(version="2", prev_version="0").deploy()

    # If prev_version matches with the existing version, it should succeed.
    v3.options(version="2", prev_version="1").deploy()
    val4, pid4 = call()
    assert val4 == "3"
    assert pid4 != pid3

    # Specifying the version should stop updates from happening.
    v3.options(version="2").deploy()
    val5, pid5 = call()
    assert val5 == "3"
    assert pid5 == pid4

    v2.options(version="3", prev_version="2").deploy()
    val6, pid6 = call()
    assert val6 == "2"
    assert pid6 != pid5

    # Deploying without specifying prev_version should still be possible.
    v1.deploy()
    val7, pid7 = call()
    assert val7 == "1"
    assert pid7 != pid6


@pytest.mark.parametrize("use_handle", [True, False])
def test_config_change(serve_instance, use_handle):
    @serve.deployment(version="1")
    class D:
        def __init__(self):
            self.ret = "1"

        def reconfigure(self, d):
            self.ret = d["ret"]

        def __call__(self, *args):
            return f"{self.ret}|{os.getpid()}"

    def call():
        if use_handle:
            ret = ray.get(D.get_handle().remote())
        else:
            ret = requests.get("http://localhost:8000/D").text

        return ret.split("|")[0], ret.split("|")[1]

    # First deploy with no user config set.
    D.deploy()
    val1, pid1 = call()
    assert val1 == "1"

    # Now update the user config without changing versions. Actor should stay
    # alive but return value should change.
    D.options(user_config={"ret": "2"}).deploy()
    val2, pid2 = call()
    assert pid2 == pid1
    assert val2 == "2"

    # Update the user config without changing the version again.
    D.options(user_config={"ret": "3"}).deploy()
    val3, pid3 = call()
    assert pid3 == pid2
    assert val3 == "3"

    # Update the version without changing the user config.
    D.options(version="2", user_config={"ret": "3"}).deploy()
    val4, pid4 = call()
    assert pid4 != pid3
    assert val4 == "3"

    # Update the version and the user config.
    D.options(version="3", user_config={"ret": "4"}).deploy()
    val5, pid5 = call()
    assert pid5 != pid4
    assert val5 == "4"


def test_reconfigure_with_exception(serve_instance):
    @serve.deployment
    class A:
        def __init__(self):
            self.config = "yoo"

        def reconfigure(self, config):
            if config == "hi":
                raise Exception("oops")

            self.config = config

        def __call__(self, *args):
            return self.config

    A.options(user_config="not_hi").deploy()
    config = ray.get(A.get_handle().remote())
    assert config == "not_hi"

    with pytest.raises(RuntimeError):
        A.options(user_config="hi").deploy()


@pytest.mark.parametrize("use_handle", [True, False])
def test_redeploy_single_replica(serve_instance, use_handle):
    # Tests that redeploying a deployment with a single replica waits for the
    # replica to completely shut down before starting a new one.
    client = serve_instance

    name = "test"

    @ray.remote
    def call(block=False):
        if use_handle:
            handle = serve.get_deployment(name).get_handle()
            ret = ray.get(handle.handler.remote(block))
        else:
            ret = requests.get(
                f"http://localhost:8000/{name}", params={"block": block}
            ).text

        return ret.split("|")[0], ret.split("|")[1]

    signal_name = f"signal-{get_random_letters()}"
    signal = SignalActor.options(name=signal_name).remote()

    @serve.deployment(name=name, version="1")
    class V1:
        async def handler(self, block: bool):
            if block:
                signal = ray.get_actor(signal_name)
                await signal.wait.remote()

            return f"1|{os.getpid()}"

        async def __call__(self, request):
            return await self.handler(request.query_params["block"] == "True")

    class V2:
        async def handler(self, *args):
            return f"2|{os.getpid()}"

        async def __call__(self, request):
            return await self.handler()

    V1.deploy()
    ref1 = call.remote(block=False)
    val1, pid1 = ray.get(ref1)
    assert val1 == "1"

    # ref2 will block until the signal is sent.
    ref2 = call.remote(block=True)
    assert len(ray.wait([ref2], timeout=2.1)[0]) == 0

    # Redeploy new version. This should not go through until the old version
    # replica completely stops.
    V2 = V1.options(func_or_class=V2, version="2")
    V2.deploy(_blocking=False)
    with pytest.raises(TimeoutError):
        client._wait_for_deployment_healthy(V2.name, timeout_s=0.1)

    # It may take some time for the handle change to propagate and requests
    # to get sent to the new version. Repeatedly send requests until they
    # start blocking
    start = time.time()
    new_version_ref = None
    while time.time() - start < 30:
        ready, not_ready = ray.wait([call.remote(block=False)], timeout=5)
        if len(ready) == 1:
            # If the request doesn't block, it must have been the old version.
            val, pid = ray.get(ready[0])
            assert val == "1"
            assert pid == pid1
        elif len(not_ready) == 1:
            # If the request blocks, it must have been the new version.
            new_version_ref = not_ready[0]
            break
    else:
        assert False, "Timed out waiting for new version to be called."

    # Signal the original call to exit.
    ray.get(signal.send.remote())
    val2, pid2 = ray.get(ref2)
    assert val2 == "1"
    assert pid2 == pid1

    # Now the goal and request to the new version should complete.
    client._wait_for_deployment_healthy(V2.name)
    new_version_val, new_version_pid = ray.get(new_version_ref)
    assert new_version_val == "2"
    assert new_version_pid != pid2


@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
@pytest.mark.parametrize("use_handle", [True, False])
def test_redeploy_multiple_replicas(serve_instance, use_handle):
    # Tests that redeploying a deployment with multiple replicas performs
    # a rolling update.
    client = serve_instance

    name = "test"

    @ray.remote(num_cpus=0)
    def call(block=False):
        if use_handle:
            handle = serve.get_deployment(name).get_handle()
            ret = ray.get(handle.handler.remote(block))
        else:
            ret = requests.get(
                f"http://localhost:8000/{name}", params={"block": block}
            ).text

        return ret.split("|")[0], ret.split("|")[1]

    signal_name = f"signal-{get_random_letters()}"
    signal = SignalActor.options(name=signal_name).remote()

    @serve.deployment(name=name, version="1", num_replicas=2)
    class V1:
        async def handler(self, block: bool):
            if block:
                signal = ray.get_actor(signal_name)
                await signal.wait.remote()

            return f"1|{os.getpid()}"

        async def __call__(self, request):
            return await self.handler(request.query_params["block"] == "True")

    class V2:
        async def handler(self, *args):
            return f"2|{os.getpid()}"

        async def __call__(self, request):
            return await self.handler()

    def make_nonblocking_calls(expected, expect_blocking=False):
        # Returns dict[val, set(pid)].
        blocking = []
        responses = defaultdict(set)
        start = time.time()
        while time.time() - start < 30:
            refs = [call.remote(block=False) for _ in range(10)]
            ready, not_ready = ray.wait(refs, timeout=5)
            for ref in ready:
                val, pid = ray.get(ref)
                responses[val].add(pid)
            for ref in not_ready:
                blocking.extend(not_ready)

            if all(len(responses[val]) == num for val, num in expected.items()) and (
                expect_blocking is False or len(blocking) > 0
            ):
                break
        else:
            assert False, f"Timed out, responses: {responses}."

        return responses, blocking

    V1.deploy()
    responses1, _ = make_nonblocking_calls({"1": 2})
    pids1 = responses1["1"]

    # ref2 will block a single replica until the signal is sent. Check that
    # some requests are now blocking.
    ref2 = call.remote(block=True)
    responses2, blocking2 = make_nonblocking_calls({"1": 1}, expect_blocking=True)
    assert list(responses2["1"])[0] in pids1

    # Redeploy new version. Since there is one replica blocking, only one new
    # replica should be started up.
    V2 = V1.options(func_or_class=V2, version="2")
    V2.deploy(_blocking=False)
    with pytest.raises(TimeoutError):
        client._wait_for_deployment_healthy(V2.name, timeout_s=0.1)
    responses3, blocking3 = make_nonblocking_calls({"1": 1}, expect_blocking=True)

    # Signal the original call to exit.
    ray.get(signal.send.remote())
    val, pid = ray.get(ref2)
    assert val == "1"
    assert pid in responses1["1"]

    # Now the goal and requests to the new version should complete.
    # We should have two running replicas of the new version.
    client._wait_for_deployment_healthy(V2.name)
    make_nonblocking_calls({"2": 2})


@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
@pytest.mark.parametrize("use_handle", [True, False])
def test_reconfigure_multiple_replicas(serve_instance, use_handle):
    # Tests that updating the user_config with multiple replicas performs a
    # rolling update.
    client = serve_instance

    name = "test"

    @ray.remote(num_cpus=0)
    def call():
        if use_handle:
            handle = serve.get_deployment(name).get_handle()
            ret = ray.get(handle.handler.remote())
        else:
            ret = requests.get(f"http://localhost:8000/{name}").text

        return ret.split("|")[0], ret.split("|")[1]

    signal_name = f"signal-{get_random_letters()}"
    signal = SignalActor.options(name=signal_name).remote()

    @serve.deployment(name=name, version="1", num_replicas=2)
    class V1:
        def __init__(self):
            self.config = None

        async def reconfigure(self, config):
            # Don't block when the replica is first created.
            if self.config is not None:
                signal = ray.get_actor(signal_name)
                ray.get(signal.wait.remote())
            self.config = config

        async def handler(self):
            return f"{self.config}|{os.getpid()}"

        async def __call__(self, request):
            return await self.handler()

    def make_nonblocking_calls(expected, expect_blocking=False):
        # Returns dict[val, set(pid)].
        blocking = []
        responses = defaultdict(set)
        start = time.time()
        while time.time() - start < 30:
            refs = [call.remote() for _ in range(10)]
            ready, not_ready = ray.wait(refs, timeout=5)
            for ref in ready:
                val, pid = ray.get(ref)
                responses[val].add(pid)
            for ref in not_ready:
                blocking.extend(not_ready)

            if all(len(responses[val]) == num for val, num in expected.items()) and (
                expect_blocking is False or len(blocking) > 0
            ):
                break
        else:
            assert False, f"Timed out, responses: {responses}."

        return responses, blocking

    V1.options(user_config="1").deploy()
    responses1, _ = make_nonblocking_calls({"1": 2})
    pids1 = responses1["1"]

    # Reconfigure should block one replica until the signal is sent. Check that
    # some requests are now blocking.
    V1.options(user_config="2").deploy(_blocking=False)
    responses2, blocking2 = make_nonblocking_calls({"1": 1}, expect_blocking=True)
    assert list(responses2["1"])[0] in pids1

    # Signal reconfigure to finish. Now the goal should complete and both
    # replicas should have the updated config.
    ray.get(signal.send.remote())
    client._wait_for_deployment_healthy(V1.name)
    make_nonblocking_calls({"2": 2})


def test_reconfigure_with_queries(serve_instance):
    signal = SignalActor.remote()

    @serve.deployment(max_concurrent_queries=10, num_replicas=3)
    class A:
        def __init__(self):
            self.state = None

        def reconfigure(self, config):
            self.state = config

        async def __call__(self):
            await signal.wait.remote()
            return self.state["a"]

    A.options(version="1", user_config={"a": 1}).deploy()
    handle = A.get_handle()
    refs = []
    for _ in range(30):
        refs.append(handle.remote())

    @ray.remote(num_cpus=0)
    def reconfigure():
        A.options(version="1", user_config={"a": 2}).deploy()

    reconfigure_ref = reconfigure.remote()
    signal.send.remote()
    ray.get(reconfigure_ref)
    for ref in refs:
        assert ray.get(ref) == 1
    assert ray.get(handle.remote()) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
@pytest.mark.parametrize("use_handle", [True, False])
def test_redeploy_scale_down(serve_instance, use_handle):
    # Tests redeploying with a new version and lower num_replicas.
    name = "test"

    @serve.deployment(name=name, version="1", num_replicas=4)
    def v1(*args):
        return f"1|{os.getpid()}"

    @ray.remote(num_cpus=0)
    def call():
        if use_handle:
            handle = v1.get_handle()
            ret = ray.get(handle.remote())
        else:
            ret = requests.get(f"http://localhost:8000/{name}").text

        return ret.split("|")[0], ret.split("|")[1]

    def make_calls(expected):
        # Returns dict[val, set(pid)].
        responses = defaultdict(set)
        start = time.time()
        while time.time() - start < 30:
            refs = [call.remote() for _ in range(10)]
            ready, not_ready = ray.wait(refs, timeout=5)
            for ref in ready:
                val, pid = ray.get(ref)
                responses[val].add(pid)

            if all(len(responses[val]) == num for val, num in expected.items()):
                break
        else:
            assert False, f"Timed out, responses: {responses}."

        return responses

    v1.deploy()
    responses1 = make_calls({"1": 4})
    pids1 = responses1["1"]

    @serve.deployment(name=name, version="2", num_replicas=2)
    def v2(*args):
        return f"2|{os.getpid()}"

    v2.deploy()
    responses2 = make_calls({"2": 2})
    assert all(pid not in pids1 for pid in responses2["2"])


@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
@pytest.mark.parametrize("use_handle", [True, False])
def test_redeploy_scale_up(serve_instance, use_handle):
    # Tests redeploying with a new version and higher num_replicas.
    name = "test"

    @serve.deployment(name=name, version="1", num_replicas=2)
    def v1(*args):
        return f"1|{os.getpid()}"

    @ray.remote(num_cpus=0)
    def call():
        if use_handle:
            handle = v1.get_handle()
            ret = ray.get(handle.remote())
        else:
            ret = requests.get(f"http://localhost:8000/{name}").text

        return ret.split("|")[0], ret.split("|")[1]

    def make_calls(expected):
        # Returns dict[val, set(pid)].
        responses = defaultdict(set)
        start = time.time()
        while time.time() - start < 30:
            refs = [call.remote() for _ in range(10)]
            ready, not_ready = ray.wait(refs, timeout=5)
            for ref in ready:
                val, pid = ray.get(ref)
                responses[val].add(pid)

            if all(len(responses[val]) == num for val, num in expected.items()):
                break
        else:
            assert False, f"Timed out, responses: {responses}."

        return responses

    v1.deploy()
    responses1 = make_calls({"1": 2})
    pids1 = responses1["1"]

    @serve.deployment(name=name, version="2", num_replicas=4)
    def v2(*args):
        return f"2|{os.getpid()}"

    v2.deploy()
    responses2 = make_calls({"2": 4})
    assert all(pid not in pids1 for pid in responses2["2"])


def test_deploy_handle_validation(serve_instance):
    @serve.deployment
    class A:
        def b(self, *args):
            return "hello"

    A.deploy()
    handle = A.get_handle()

    # Legacy code path
    assert ray.get(handle.options(method_name="b").remote()) == "hello"
    # New code path
    assert ray.get(handle.b.remote()) == "hello"
    with pytest.raises(RayServeException):
        ray.get(handle.c.remote())


def test_init_args(serve_instance):
    @serve.deployment(init_args=(1, 2, 3))
    class D:
        def __init__(self, *args):
            self._args = args

        def get_args(self, *args):
            return self._args

    D.deploy()
    handle = D.get_handle()

    def check(*args):
        assert ray.get(handle.get_args.remote()) == args

    # Basic sanity check.
    assert ray.get(handle.get_args.remote()) == (1, 2, 3)
    check(1, 2, 3)

    # Check passing args to `.deploy()`.
    D.deploy(4, 5, 6)
    check(4, 5, 6)

    # Passing args to `.deploy()` shouldn't override those passed in decorator.
    D.deploy()
    check(1, 2, 3)

    # Check setting with `.options()`.
    new_D = D.options(init_args=(7, 8, 9))
    new_D.deploy()
    check(7, 8, 9)

    # Should not have changed old deployment object.
    D.deploy()
    check(1, 2, 3)

    # Check that args are only updated on version change.
    D.options(version="1").deploy()
    check(1, 2, 3)

    D.options(version="1").deploy(10, 11, 12)
    check(1, 2, 3)

    D.options(version="2").deploy(10, 11, 12)
    check(10, 11, 12)


def test_init_kwargs(serve_instance):
    with pytest.raises(TypeError):

        @serve.deployment(init_kwargs=[1, 2, 3])
        class BadInitArgs:
            pass

    @serve.deployment(init_kwargs={"a": 1, "b": 2})
    class D:
        def __init__(self, **kwargs):
            self._kwargs = kwargs

        def get_kwargs(self, *args):
            return self._kwargs

    D.deploy()
    handle = D.get_handle()

    def check(kwargs):
        assert ray.get(handle.get_kwargs.remote()) == kwargs

    # Basic sanity check.
    check({"a": 1, "b": 2})

    # Check passing args to `.deploy()`.
    D.deploy(a=3, b=4)
    check({"a": 3, "b": 4})

    # Passing args to `.deploy()` shouldn't override those passed in decorator.
    D.deploy()
    check({"a": 1, "b": 2})

    # Check setting with `.options()`.
    new_D = D.options(init_kwargs={"c": 8, "d": 10})
    new_D.deploy()
    check({"c": 8, "d": 10})

    # Should not have changed old deployment object.
    D.deploy()
    check({"a": 1, "b": 2})

    # Check that args are only updated on version change.
    D.options(version="1").deploy()
    check({"a": 1, "b": 2})

    D.options(version="1").deploy(c=10, d=11)
    check({"a": 1, "b": 2})

    D.options(version="2").deploy(c=10, d=11)
    check({"c": 10, "d": 11})


def test_input_validation():
    name = "test"

    @serve.deployment(name=name)
    class Base:
        pass

    with pytest.raises(RuntimeError):
        Base()

    with pytest.raises(TypeError):

        @serve.deployment(name=name, version=1)
        class BadVersion:
            pass

    with pytest.raises(TypeError):
        Base.options(version=1)

    with pytest.raises(ValidationError):

        @serve.deployment(num_replicas="hi")
        class BadNumReplicas:
            pass

    with pytest.raises(ValidationError):
        Base.options(num_replicas="hi")

    with pytest.raises(ValidationError):

        @serve.deployment(num_replicas=0)
        class ZeroNumReplicas:
            pass

    with pytest.raises(ValidationError):
        Base.options(num_replicas=0)

    with pytest.raises(ValidationError):

        @serve.deployment(num_replicas=-1)
        class NegativeNumReplicas:
            pass

    with pytest.raises(ValidationError):
        Base.options(num_replicas=-1)

    with pytest.raises(TypeError):

        @serve.deployment(init_args={1, 2, 3})
        class BadInitArgs:
            pass

    with pytest.raises(TypeError):
        Base.options(init_args="hi")

    with pytest.raises(TypeError):

        @serve.deployment(ray_actor_options=[1, 2, 3])
        class BadActorOpts:
            pass

    with pytest.raises(TypeError):
        Base.options(ray_actor_options="hi")

    with pytest.raises(ValidationError):

        @serve.deployment(max_concurrent_queries="hi")
        class BadMaxQueries:
            pass

    with pytest.raises(ValidationError):
        Base.options(max_concurrent_queries=[1])

    with pytest.raises(ValueError):

        @serve.deployment(max_concurrent_queries=0)
        class ZeroMaxQueries:
            pass

    with pytest.raises(ValueError):
        Base.options(max_concurrent_queries=0)

    with pytest.raises(ValueError):

        @serve.deployment(max_concurrent_queries=-1)
        class NegativeMaxQueries:
            pass

    with pytest.raises(ValueError):
        Base.options(max_concurrent_queries=-1)


def test_deployment_properties():
    class DClass:
        pass

    D = serve.deployment(
        name="name",
        init_args=("hello", 123),
        version="version",
        num_replicas=2,
        user_config="hi",
        max_concurrent_queries=100,
        route_prefix="/hello",
        ray_actor_options={"num_cpus": 2},
    )(DClass)

    assert D.name == "name"
    assert D.init_args == ("hello", 123)
    assert D.version == "version"
    assert D.num_replicas == 2
    assert D.user_config == "hi"
    assert D.max_concurrent_queries == 100
    assert D.route_prefix == "/hello"
    assert D.ray_actor_options == {"num_cpus": 2}

    D = serve.deployment(
        version=None,
        route_prefix=None,
    )(DClass)
    assert D.version is None
    assert D.route_prefix is None


class TestGetDeployment:
    def get_deployment(self, name, use_list_api):
        if use_list_api:
            return serve.list_deployments()[name]
        else:
            return serve.get_deployment(name)

    @pytest.mark.parametrize("use_list_api", [True, False])
    def test_basic_get(self, serve_instance, use_list_api):
        name = "test"

        @serve.deployment(name=name, version="1")
        def d(*args):
            return "1", os.getpid()

        with pytest.raises(KeyError):
            self.get_deployment(name, use_list_api)

        d.deploy()
        val1, pid1 = ray.get(d.get_handle().remote())
        assert val1 == "1"

        del d

        d2 = self.get_deployment(name, use_list_api)
        val2, pid2 = ray.get(d2.get_handle().remote())
        assert val2 == "1"
        assert pid2 == pid1

    @pytest.mark.parametrize("use_list_api", [True, False])
    def test_get_after_delete(self, serve_instance, use_list_api):
        name = "test"

        @serve.deployment(name=name, version="1")
        def d(*args):
            return "1", os.getpid()

        d.deploy()
        del d

        d2 = self.get_deployment(name, use_list_api)
        d2.delete()
        del d2

        with pytest.raises(KeyError):
            self.get_deployment(name, use_list_api)

    @pytest.mark.parametrize("use_list_api", [True, False])
    def test_deploy_new_version(self, serve_instance, use_list_api):
        name = "test"

        @serve.deployment(name=name, version="1")
        def d(*args):
            return "1", os.getpid()

        d.deploy()
        val1, pid1 = ray.get(d.get_handle().remote())
        assert val1 == "1"

        del d

        d2 = self.get_deployment(name, use_list_api)
        d2.options(version="2").deploy()
        val2, pid2 = ray.get(d2.get_handle().remote())
        assert val2 == "1"
        assert pid2 != pid1

    @pytest.mark.parametrize("use_list_api", [True, False])
    def test_deploy_empty_version(self, serve_instance, use_list_api):
        name = "test"

        @serve.deployment(name=name)
        def d(*args):
            return "1", os.getpid()

        d.deploy()
        val1, pid1 = ray.get(d.get_handle().remote())
        assert val1 == "1"

        del d

        d2 = self.get_deployment(name, use_list_api)
        d2.deploy()
        val2, pid2 = ray.get(d2.get_handle().remote())
        assert val2 == "1"
        assert pid2 != pid1

    @pytest.mark.parametrize("use_list_api", [True, False])
    def test_init_args(self, serve_instance, use_list_api):
        name = "test"

        @serve.deployment(name=name)
        class D:
            def __init__(self, val):
                self._val = val

            def __call__(self, *arg):
                return self._val, os.getpid()

        D.deploy("1")
        val1, pid1 = ray.get(D.get_handle().remote())
        assert val1 == "1"

        del D

        D2 = self.get_deployment(name, use_list_api)
        D2.deploy()
        val2, pid2 = ray.get(D2.get_handle().remote())
        assert val2 == "1"
        assert pid2 != pid1

        D2 = self.get_deployment(name, use_list_api)
        D2.deploy("2")
        val3, pid3 = ray.get(D2.get_handle().remote())
        assert val3 == "2"
        assert pid3 != pid2

    @pytest.mark.parametrize("use_list_api", [True, False])
    def test_scale_replicas(self, serve_instance, use_list_api):
        name = "test"

        @serve.deployment(name=name)
        def d(*args):
            return os.getpid()

        def check_num_replicas(num):
            handle = self.get_deployment(name, use_list_api).get_handle()
            assert len(set(ray.get([handle.remote() for _ in range(50)]))) == num

        d.deploy()
        check_num_replicas(1)
        del d

        d2 = self.get_deployment(name, use_list_api)
        d2.options(num_replicas=2).deploy()
        check_num_replicas(2)


def test_list_deployments(serve_instance):
    assert serve.list_deployments() == {}

    @serve.deployment(name="hi", num_replicas=2)
    def d1(*args):
        pass

    d1.deploy()

    assert serve.list_deployments() == {"hi": d1}


def test_deploy_change_route_prefix(serve_instance):
    name = "test"

    @serve.deployment(name=name, version="1", route_prefix="/old")
    def d(*args):
        return f"1|{os.getpid()}"

    def call(route):
        ret = requests.get(f"http://localhost:8000/{route}").text
        return ret.split("|")[0], ret.split("|")[1]

    d.deploy()
    val1, pid1 = call("old")
    assert val1 == "1"

    # Check that the old route is gone and the response from the new route
    # has the same value and PID (replica wasn't restarted).
    def check_switched():
        try:
            print(call("old"))
            return False
        except Exception:
            print("failed")
            pass

        try:
            val2, pid2 = call("new")
        except Exception:
            return False

        assert val2 == "1"
        assert pid2 == pid1
        return True

    d.options(route_prefix="/new").deploy()
    wait_for_condition(check_switched)


@pytest.mark.parametrize("prefixes", [[None, "/f", None], ["/f", None, "/f"]])
def test_deploy_nullify_route_prefix(serve_instance, prefixes):
    @serve.deployment
    def f(*args):
        return "got me"

    for prefix in prefixes:
        f.options(route_prefix=prefix).deploy()
        if prefix is None:
            assert requests.get("http://localhost:8000/f").status_code == 404
        else:
            assert requests.get("http://localhost:8000/f").text == "got me"
        assert ray.get(f.get_handle().remote()) == "got me"


@pytest.mark.timeout(10, method="thread")
def test_deploy_empty_bundle(serve_instance):
    @serve.deployment(ray_actor_options={"num_cpus": 0})
    class D:
        def hello(self, _):
            return "hello"

    # This should succesfully terminate within the provided time-frame.
    D.deploy()


def test_deployment_error_handling(serve_instance):
    @serve.deployment
    def f():
        pass

    with pytest.raises(RuntimeError, match=". is not a valid URI"):
        # This is an invalid configuration since dynamic upload of working
        # directories is not supported. The error this causes in the controller
        # code should be caught and reported back to the `deploy` caller.

        f.options(ray_actor_options={"runtime_env": {"working_dir": "."}}).deploy()


def test_http_proxy_request_cancellation(serve_instance):
    # https://github.com/ray-project/ray/issues/21425
    s = SignalActor.remote()

    @serve.deployment(max_concurrent_queries=1)
    class A:
        def __init__(self) -> None:
            self.counter = 0

        async def __call__(self):
            self.counter += 1
            ret_val = self.counter
            await s.wait.remote()
            return ret_val

    A.deploy()

    url = "http://127.0.0.1:8000/A"
    with ThreadPoolExecutor() as pool:
        # Send the first request, it should block for the result
        first_blocking_fut = pool.submit(
            functools.partial(requests.get, url, timeout=100)
        )
        time.sleep(1)
        assert not first_blocking_fut.done()

        # Send more requests, these should be queued in handle.
        # But because first request is hanging and these have low timeout.
        # They should all disconnect from http connection.
        # These requests should never reach the replica.
        rest_blocking_futs = [
            pool.submit(functools.partial(requests.get, url, timeout=0.5))
            for _ in range(3)
        ]
        time.sleep(1)
        assert all(f.done() for f in rest_blocking_futs)

        # Now unblock the first request.
        ray.get(s.send.remote())
        assert first_blocking_fut.result().text == "1"

    # Sending another request to verify that only one request has been
    # processed so far.
    assert requests.get(url).text == "2"


class TestDeployGroup:
    @serve.deployment
    def f():
        return "f reached"

    @serve.deployment
    def g():
        return "g reached"

    @serve.deployment
    class C:
        async def __call__(self):
            return "C reached"

    @serve.deployment
    class D:
        async def __call__(self):
            return "D reached"

    def deploy_and_check_responses(
        self, deployments, responses, blocking=True, client=None
    ):
        """
        Helper function that deploys the list of deployments, calls them with
        their handles, and checks whether they return the objects in responses.
        If blocking is False, this function uses a non-blocking deploy and uses
        the client to wait until the deployments finish deploying.
        """

        Application(deployments).deploy(blocking=blocking)

        def check_all_deployed():
            try:
                for deployment, response in zip(deployments, responses):
                    if ray.get(deployment.get_handle().remote()) != response:
                        return False
            except Exception:
                return False

            return True

        if blocking:
            # If blocking, this should be guaranteed to pass immediately.
            assert check_all_deployed()
        else:
            # If non-blocking, this should pass eventually.
            wait_for_condition(check_all_deployed)

    def test_basic_deploy_group(self, serve_instance):
        """
        Atomically deploys a group of deployments, including both functions and
        classes. Checks whether they deploy correctly.
        """

        deployments = [self.f, self.g, self.C, self.D]
        responses = ["f reached", "g reached", "C reached", "D reached"]

        self.deploy_and_check_responses(deployments, responses)

    def test_non_blocking_deploy_group(self, serve_instance):
        """Checks Application's deploy() behavior when blocking=False."""

        deployments = [self.f, self.g, self.C, self.D]
        responses = ["f reached", "g reached", "C reached", "D reached"]
        self.deploy_and_check_responses(
            deployments, responses, blocking=False, client=serve_instance
        )

    def test_mutual_handles(self, serve_instance):
        """
        Atomically deploys a group of deployments that get handles to other
        deployments in the group inside their __init__ functions. The handle
        references should fail in a non-atomic deployment. Checks whether the
        deployments deploy correctly.
        """

        @serve.deployment
        class MutualHandles:
            async def __init__(self, handle_name):
                self.handle = serve.get_deployment(handle_name).get_handle()

            async def __call__(self, echo: str):
                return await self.handle.request_echo.remote(echo)

            async def request_echo(self, echo: str):
                return echo

        names = []
        for i in range(10):
            names.append("a" * i)

        deployments = []
        for idx in range(len(names)):
            # Each deployment will hold a ServeHandle with the next name in
            # the list
            deployment_name = names[idx]
            handle_name = names[(idx + 1) % len(names)]

            deployments.append(
                MutualHandles.options(name=deployment_name, init_args=(handle_name,))
            )

        Application(deployments).deploy(blocking=True)

        for deployment in deployments:
            assert (ray.get(deployment.get_handle().remote("hello"))) == "hello"

    def test_decorated_deployments(self, serve_instance):
        """
        Checks Application's deploy behavior when deployments have options set
        in their @serve.deployment decorator.
        """

        @serve.deployment(num_replicas=2, max_concurrent_queries=5)
        class DecoratedClass1:
            async def __call__(self):
                return "DecoratedClass1 reached"

        @serve.deployment(num_replicas=4, max_concurrent_queries=2)
        class DecoratedClass2:
            async def __call__(self):
                return "DecoratedClass2 reached"

        deployments = [DecoratedClass1, DecoratedClass2]
        responses = ["DecoratedClass1 reached", "DecoratedClass2 reached"]
        self.deploy_and_check_responses(deployments, responses)

    def test_empty_list(self, serve_instance):
        """Checks Application's deploy behavior when deployment group is empty."""

        self.deploy_and_check_responses([], [])

    def test_invalid_input(self, serve_instance):
        """
        Checks Application's deploy behavior when deployment group contains
        non-Deployment objects.
        """

        with pytest.raises(TypeError):
            Application([self.f, self.C, "not a Deployment object"]).deploy(
                blocking=True
            )

    def test_import_path_deployment(self, serve_instance):
        test_env_uri = (
            "https://github.com/shrekris-anyscale/test_deploy_group/archive/HEAD.zip"
        )
        test_module_uri = (
            "https://github.com/shrekris-anyscale/test_module/archive/HEAD.zip"
        )

        ray_actor_options = {
            "runtime_env": {"py_modules": [test_env_uri, test_module_uri]}
        }

        shallow = serve.deployment(
            name="shallow",
            ray_actor_options=ray_actor_options,
        )("test_env.shallow_import.ShallowClass")

        deep = serve.deployment(
            name="deep",
            ray_actor_options=ray_actor_options,
        )("test_env.subdir1.subdir2.deep_import.DeepClass")

        one = serve.deployment(
            name="one",
            ray_actor_options=ray_actor_options,
        )("test_module.test.one")

        deployments = [shallow, deep, one]
        responses = ["Hello shallow world!", "Hello deep world!", 2]

        self.deploy_and_check_responses(deployments, responses)

    def test_different_pymodules(self, serve_instance):
        test_env_uri = (
            "https://github.com/shrekris-anyscale/test_deploy_group/archive/HEAD.zip"
        )
        test_module_uri = (
            "https://github.com/shrekris-anyscale/test_module/archive/HEAD.zip"
        )

        shallow = serve.deployment(
            name="shallow",
            ray_actor_options={"runtime_env": {"py_modules": [test_env_uri]}},
        )("test_env.shallow_import.ShallowClass")

        one = serve.deployment(
            name="one",
            ray_actor_options={"runtime_env": {"py_modules": [test_module_uri]}},
        )("test_module.test.one")

        deployments = [shallow, one]
        responses = ["Hello shallow world!", 2]

        self.deploy_and_check_responses(deployments, responses)

    def test_import_path_deployment_decorated(self, serve_instance):
        func = serve.deployment(
            name="decorated_func",
        )("ray.serve.tests.test_deploy.decorated_func")

        clss = serve.deployment(
            name="decorated_clss",
        )("ray.serve.tests.test_deploy.DecoratedClass")

        deployments = [func, clss]
        responses = ["got decorated func", "got decorated class"]

        self.deploy_and_check_responses(deployments, responses)

        # Check that non-default decorated values were overwritten
        assert serve.get_deployment("decorated_func").max_concurrent_queries != 17
        assert serve.get_deployment("decorated_clss").max_concurrent_queries != 17


# Decorated function with non-default max_concurrent queries
@serve.deployment(max_concurrent_queries=17)
def decorated_func(req=None):
    return "got decorated func"


# Decorated class with non-default max_concurrent queries
@serve.deployment(max_concurrent_queries=17)
class DecoratedClass:
    def __call__(self, req=None):
        return "got decorated class"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
