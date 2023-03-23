import sys
import pytest
import requests
import subprocess
from typing import Dict
from fastapi import FastAPI
from starlette.requests import Request

import ray
from ray._private.test_utils import wait_for_condition

from ray import serve
from ray.dag.input_node import InputNode
from ray.serve.drivers import DefaultgRPCDriver, DAGDriver
from ray.serve.http_adapters import json_request
from ray.serve._private.utils import get_random_letters


TELEMETRY_ROUTE_PREFIX = "/telemetry"


def check_ray_stopped():
    try:
        requests.get("http://localhost:52365/api/ray/version")
        return False
    except Exception:
        return True


def check_ray_started():
    return requests.get("http://localhost:52365/api/ray/version").status_code == 200


@pytest.fixture
def manage_ray(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("RAY_USAGE_STATS_ENABLED", "1")
        m.setenv(
            "RAY_USAGE_STATS_REPORT_URL",
            f"http://127.0.0.1:8000{TELEMETRY_ROUTE_PREFIX}",
        )
        m.setenv("RAY_USAGE_STATS_REPORT_INTERVAL_S", "1")
        subprocess.check_output(["ray", "stop", "--force"])
        wait_for_condition(check_ray_stopped, timeout=5)
        yield

        # Call Python API shutdown() methods to clear global variable state
        serve.shutdown()
        ray.shutdown()

        # Shut down Ray cluster with CLI
        subprocess.check_output(["ray", "stop", "--force"])
        wait_for_condition(check_ray_stopped, timeout=5)


def start_telemetry_app():
    """Start a telemetry Serve app.

    Ray should be initialized before calling this method.

    NOTE: If you're running the TelemetryReceiver Serve app to check telemetry,
    remember that the receiver itself is counted in the telemetry. E.g. if you
    deploy a Serve app other than the receiver, the number of apps in the
    cluster is 2- not 1– since the receiver is also running.

    Returns a handle to a TelemetryStorage actor. You can use this actor
    to access the latest telemetry reports.
    """

    storage_actor_name = get_random_letters(10)

    @ray.remote(name=storage_actor_name, num_cpus=0)
    class TelemetryStorage:
        def __init__(self):
            self.reports_received = 0
            self.current_report = dict()

        def store_report(self, report: Dict) -> None:
            self.reports_received += 1
            self.current_report = report

        def get_report(self) -> Dict:
            return self.current_report

        def get_reports_received(self) -> int:
            return self.reports_received

    reporter = TelemetryStorage.remote()

    @serve.deployment(ray_actor_options={"num_cpus": 0})
    class TelemetryReceiver:
        def __init__(self, storage_actor_name: str):
            self.storage = ray.get_actor(name=storage_actor_name)

        async def __call__(self, request: Request) -> bool:
            report = await request.json()
            ray.get(self.storage.store_report.remote(report))
            return True

    receiver_app = TelemetryReceiver.bind(storage_actor_name)
    serve.run(receiver_app, name="telemetry", route_prefix=TELEMETRY_ROUTE_PREFIX)

    return reporter


def test_fastapi_detected(manage_ray):
    """
    Check that FastAPI is detected by telemetry.
    """

    subprocess.check_output(["ray", "start", "--head"])
    wait_for_condition(check_ray_started, timeout=5)

    app = FastAPI()

    @serve.deployment
    @serve.ingress(app)
    class FastAPIDeployment:
        @app.get("/route1")
        async def route1(self):
            return "Welcome to route 1!"

        @app.get("/route2")
        async def app2(self):
            return "Welcome to route 2!"

    fastapi_app = FastAPIDeployment.bind()
    serve.run(fastapi_app, name="fastapi_app", route_prefix="/fastapi")

    storage_handle = start_telemetry_app()

    wait_for_condition(
        lambda: ray.get(storage_handle.get_reports_received.remote()) > 0, timeout=5
    )
    report = ray.get(storage_handle.get_report.remote())

    # Check all telemetry relevant to the Serve apps on this cluster
    assert int(report["extra_usage_tags"]["serve_fastapi_used"]) == 1
    assert report["extra_usage_tags"]["serve_api_version"] == "v2"
    assert int(report["extra_usage_tags"]["serve_num_apps"]) == 2
    assert int(report["extra_usage_tags"]["serve_num_deployments"]) == 2
    assert int(report["extra_usage_tags"]["serve_num_gpu_deployments"]) == 0

    # Check that Serve telemetry not relevant to the running apps is omitted
    assert "serve_dag_driver_used" not in report
    assert "serve_http_adapter_used" not in report
    assert "serve_grpc_ingress_used" not in report
    assert "serve_rest_api_version" not in report


def test_grpc_detected(manage_ray):
    """
    Test that FastAPI is detected by telemetry.
    """

    subprocess.check_output(["ray", "start", "--head"])
    wait_for_condition(check_ray_started, timeout=5)

    @serve.deployment(ray_actor_options={"num_cpus": 0})
    def greeter(inputs: Dict[str, bytes]):
        return "Hello!"

    with InputNode() as grpc_input:
        greeter_node = greeter.bind(grpc_input)
        grpc_app = DefaultgRPCDriver.bind(greeter_node)

    serve.run(grpc_app, name="grpc_app", route_prefix="/grpc")

    storage_handle = start_telemetry_app()

    wait_for_condition(
        lambda: ray.get(storage_handle.get_reports_received.remote()) > 0, timeout=5
    )
    report = ray.get(storage_handle.get_report.remote())

    # Check all telemetry relevant to the Serve apps on this cluster
    assert int(report["extra_usage_tags"]["serve_grpc_ingress_used"]) == 1
    assert report["extra_usage_tags"]["serve_api_version"] == "v2"
    assert int(report["extra_usage_tags"]["serve_num_apps"]) == 2
    assert int(report["extra_usage_tags"]["serve_num_deployments"]) == 3
    assert int(report["extra_usage_tags"]["serve_num_gpu_deployments"]) == 0

    # Check that Serve telemetry not relevant to the running apps is omitted
    assert "serve_dag_driver_used" not in report
    assert "serve_http_adapter_used" not in report
    assert "serve_fastapi_used" not in report
    assert "serve_rest_api_version" not in report


@pytest.mark.parametrize("use_adapter", [True, False])
def test_graph_detected(manage_ray, use_adapter):
    """
    Test that DAGDriver and HTTP adapters are detected by telemetry.
    """

    subprocess.check_output(["ray", "start", "--head"])
    wait_for_condition(check_ray_started, timeout=5)

    @serve.deployment(ray_actor_options={"num_cpus": 0})
    def greeter(input):
        return "Hello!"

    with InputNode() as input:
        greeter_node = greeter.bind(input)

    if use_adapter:
        graph_app = DAGDriver.bind(greeter_node, http_adapter=json_request)
    else:
        graph_app = DAGDriver.bind(greeter_node)

    serve.run(graph_app, name="graph_app", route_prefix="/graph")

    storage_handle = start_telemetry_app()

    wait_for_condition(
        lambda: ray.get(storage_handle.get_reports_received.remote()) > 0, timeout=5
    )
    report = ray.get(storage_handle.get_report.remote())

    # Check all telemetry relevant to the Serve apps on this cluster
    assert int(report["extra_usage_tags"]["serve_dag_driver_used"]) == 1
    assert report["extra_usage_tags"]["serve_api_version"] == "v2"
    assert int(report["extra_usage_tags"]["serve_num_apps"]) == 2
    assert int(report["extra_usage_tags"]["serve_num_deployments"]) == 3
    assert int(report["extra_usage_tags"]["serve_num_gpu_deployments"]) == 0
    if use_adapter:
        assert int(report["extra_usage_tags"]["serve_http_adapter_used"]) == 1

    # Check that Serve telemetry not relevant to the running apps is omitted
    assert "serve_fastapi_used" not in report
    assert "serve_grpc_ingress_used" not in report
    assert "serve_rest_api_version" not in report
    if not use_adapter:
        assert "serve_http_adapter_used" not in report


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))
