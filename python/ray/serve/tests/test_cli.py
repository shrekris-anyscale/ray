import os
import sys
import time
import yaml
import signal
import pytest
import requests
import subprocess
from typing import List
from tempfile import NamedTemporaryFile

import ray
from ray import serve
from ray.tests.conftest import tmp_working_dir  # noqa: F401, E501
from ray._private.test_utils import wait_for_condition
from ray.serve.application import Application
from ray.serve.deployment_graph import RayServeDAGHandle

CONNECTION_ERROR_MSG = "connection error"


def ping_endpoint(endpoint: str, params: str = ""):
    try:
        return requests.get(f"http://localhost:8000/{endpoint}{params}").text
    except requests.exceptions.ConnectionError:
        return CONNECTION_ERROR_MSG

def assert_deployments_live(names: List[str]):
    """Checks if all deployments named in names have at least 1 living replica."""

    running_actor_names = [actor["name"] for actor in ray.util.list_named_actors(all_namespaces=True)]

    all_deployments_live, nonliving_deployment = True, ""
    for deployment_name in names:
        for actor_name in running_actor_names:
            if deployment_name in actor_name:
                break
        else:
            all_deployments_live, nonliving_deployment = False, deployment_name
    assert all_deployments_live, f'"{nonliving_deployment}" deployment is not live.'


@pytest.fixture
def ray_start_stop():
    subprocess.check_output(["ray", "start", "--head"])
    yield
    subprocess.check_output(["ray", "stop", "--force"])


def test_start_shutdown(ray_start_stop):
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.check_output(["serve", "shutdown"])

    subprocess.check_output(["serve", "start"])
    subprocess.check_output(["serve", "shutdown"])


def test_start_shutdown_in_namespace(ray_start_stop):
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.check_output(["serve", "shutdown", "-n", "test"])

    subprocess.check_output(["serve", "start", "-n", "test"])
    subprocess.check_output(["serve", "shutdown", "-n", "test"])


@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
def test_deploy(ray_start_stop):
    """Deploys some valid config files and checks that the deployments work."""

    # Initialize serve in test to enable calling serve.list_deployments()
    ray.init(address="auto", namespace="serve")

    # Create absolute file names to YAML config files
    pizza_file_name = os.path.join(
        os.path.dirname(__file__), "test_config_files", "pizza.yaml"
    )
    arithmetic_file_name = os.path.join(
        os.path.dirname(__file__), "test_config_files", "arithmetic.yaml"
    )

    success_message_fragment = b"Sent deploy request successfully!"

    # Ensure the CLI is idempotent
    num_iterations = 2
    for iteration in range(1, num_iterations + 1):
        print(f"*** Starting Iteration {iteration}/{num_iterations} ***\n")

        print("Deploying pizza config.")
        deploy_response = subprocess.check_output(
            ["serve", "deploy", pizza_file_name]
        )
        assert success_message_fragment in deploy_response
        print("Deploy request sent successfully.")

        wait_for_condition(
            lambda: requests.post("http://localhost:8000/", json=["ADD", 2]).json()
            == "3 pizzas please!",
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/", json=["MUL", 2]).json()
            == "-4 pizzas please!",
            timeout=15,
        )
        print("Deployments are reachable over HTTP.")

        deployment_names = ["DAGDriver", "create_order", "Router", "Multiplier", "Adder"]
        assert_deployments_live(deployment_names)
        print("All deployments are live.\n")

        print("Deploying arithmetic config.")
        deploy_response = subprocess.check_output(
            ["serve", "deploy", arithmetic_file_name]
        )
        assert success_message_fragment in deploy_response
        print("Deploy request sent successfully.")

        wait_for_condition(
            lambda: requests.post("http://localhost:8000/", json=["ADD", 0]).json()
            == 1,
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/", json=["SUB", 5]).json()
            == 3,
            timeout=15,
        )
        print("Deployments are reachable over HTTP.")

        deployment_names = ["DAGDriver", "Router", "Add", "Subtract"]
        assert_deployments_live(deployment_names)
        print("All deployments are live.\n")

    ray.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
def test_config(ray_start_stop):
    # Deploys valid config file and checks that serve info returns correct
    # response

    config_file_name = os.path.join(
        os.path.dirname(__file__), "test_config_files", "two_deployments.yaml"
    )
    success_message_fragment = b"Sent deploy request successfully!"
    deploy_response = subprocess.check_output(["serve", "deploy", config_file_name])
    assert success_message_fragment in deploy_response

    info_response = subprocess.check_output(["serve", "config"])
    info = yaml.safe_load(info_response)

    assert "deployments" in info
    assert len(info["deployments"]) == 2

    # Validate non-default information about shallow deployment
    shallow_info = None
    for deployment_info in info["deployments"]:
        if deployment_info["name"] == "shallow":
            shallow_info = deployment_info

    assert shallow_info is not None
    assert shallow_info["import_path"] == "test_env.shallow_import.ShallowClass"
    assert shallow_info["num_replicas"] == 3
    assert shallow_info["route_prefix"] == "/shallow"
    assert (
        "https://github.com/shrekris-anyscale/test_deploy_group/archive/HEAD.zip"
        in shallow_info["ray_actor_options"]["runtime_env"]["py_modules"]
    )
    assert (
        "https://github.com/shrekris-anyscale/test_module/archive/HEAD.zip"
        in shallow_info["ray_actor_options"]["runtime_env"]["py_modules"]
    )

    # Validate non-default information about one deployment
    one_info = None
    for deployment_info in info["deployments"]:
        if deployment_info["name"] == "one":
            one_info = deployment_info

    assert one_info is not None
    assert one_info["import_path"] == "test_module.test.one"
    assert one_info["num_replicas"] == 2
    assert one_info["route_prefix"] == "/one"
    assert (
        "https://github.com/shrekris-anyscale/test_deploy_group/archive/HEAD.zip"
        in one_info["ray_actor_options"]["runtime_env"]["py_modules"]
    )
    assert (
        "https://github.com/shrekris-anyscale/test_module/archive/HEAD.zip"
        in one_info["ray_actor_options"]["runtime_env"]["py_modules"]
    )


@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
def test_status(ray_start_stop):
    # Deploys a config file and checks its status

    config_file_name = os.path.join(
        os.path.dirname(__file__), "test_config_files", "three_deployments.yaml"
    )

    subprocess.check_output(["serve", "deploy", config_file_name])
    status_response = subprocess.check_output(["serve", "status"])
    serve_status = yaml.safe_load(status_response)

    expected_deployments = {"shallow", "deep", "one"}
    for status in serve_status["deployment_statuses"]:
        expected_deployments.remove(status["name"])
        assert status["status"] in {"HEALTHY", "UPDATING"}
        assert "message" in status
    assert len(expected_deployments) == 0

    assert serve_status["app_status"]["status"] in {"DEPLOYING", "RUNNING"}
    wait_for_condition(
        lambda: time.time() > serve_status["app_status"]["deployment_timestamp"],
        timeout=2,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
def test_delete(ray_start_stop):
    # Deploys a config file and deletes it

    def get_num_deployments():
        info_response = subprocess.check_output(["serve", "config"])
        info = yaml.safe_load(info_response)
        return len(info["deployments"])

    config_file_name = os.path.join(
        os.path.dirname(__file__), "test_config_files", "two_deployments.yaml"
    )

    # Check idempotence
    for _ in range(2):
        subprocess.check_output(["serve", "deploy", config_file_name])
        wait_for_condition(lambda: get_num_deployments() == 2, timeout=35)

        subprocess.check_output(["serve", "delete", "-y"])
        wait_for_condition(lambda: get_num_deployments() == 0, timeout=35)


@serve.deployment
def parrot(request):
    return request.query_params["sound"]


parrot_app = Application([parrot])


@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
def test_run_application(ray_start_stop):
    # Deploys valid config file and import path via serve run

    # Deploy via config file
    config_file_name = os.path.join(
        os.path.dirname(__file__), "test_config_files", "two_deployments.yaml"
    )

    p = subprocess.Popen(["serve", "run", "--address=auto", config_file_name])
    wait_for_condition(lambda: ping_endpoint("one") == "2", timeout=10)
    wait_for_condition(
        lambda: ping_endpoint("shallow") == "Hello shallow world!", timeout=10
    )

    p.send_signal(signal.SIGINT)  # Equivalent to ctrl-C
    p.wait()
    assert ping_endpoint("one") == CONNECTION_ERROR_MSG
    assert ping_endpoint("shallow") == CONNECTION_ERROR_MSG

    # Deploy via import path
    p = subprocess.Popen(
        ["serve", "run", "--address=auto", "ray.serve.tests.test_cli.parrot_app"]
    )
    wait_for_condition(
        lambda: ping_endpoint("parrot", params="?sound=squawk") == "squawk", timeout=10
    )

    p.send_signal(signal.SIGINT)  # Equivalent to ctrl-C
    p.wait()
    assert ping_endpoint("parrot", params="?sound=squawk") == CONNECTION_ERROR_MSG


@serve.deployment
class Macaw:
    def __init__(self, color, name="Mulligan", surname=None):
        self.color = color
        self.name = name
        self.surname = surname

    def __call__(self):
        if self.surname is not None:
            return f"{self.name} {self.surname} is {self.color}!"
        else:
            return f"{self.name} is {self.color}!"


molly_macaw = Macaw.bind("green", name="Molly")


@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
def test_run_deployment_node(ray_start_stop):
    # Tests serve run with specified args and kwargs

    # Deploy via import path
    p = subprocess.Popen(
        [
            "serve",
            "run",
            "--address=auto",
            "ray.serve.tests.test_cli.molly_macaw",
        ]
    )
    wait_for_condition(lambda: ping_endpoint("Macaw") == "Molly is green!", timeout=10)
    p.send_signal(signal.SIGINT)
    p.wait()
    assert ping_endpoint("Macaw") == CONNECTION_ERROR_MSG


@serve.deployment
class MetalDetector:
    def __call__(self, *args):
        return os.environ.get("buried_item", "no dice")


metal_detector_node = MetalDetector.bind()


@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
def test_run_runtime_env(ray_start_stop):
    # Test serve run with runtime_env passed in

    # With import path
    p = subprocess.Popen(
        [
            "serve",
            "run",
            "--address=auto",
            "ray.serve.tests.test_cli.metal_detector_node",
            "--runtime-env-json",
            ('{"env_vars": {"buried_item": "lucky coin"} }'),
        ]
    )
    wait_for_condition(
        lambda: ping_endpoint("MetalDetector") == "lucky coin", timeout=10
    )
    p.send_signal(signal.SIGINT)
    p.wait()

    # With config
    p = subprocess.Popen(
        [
            "serve",
            "run",
            "--address=auto",
            os.path.join(
                os.path.dirname(__file__),
                "test_config_files",
                "missing_runtime_env.yaml",
            ),
            "--runtime-env-json",
            (
                '{"py_modules": ["https://github.com/shrekris-anyscale/'
                'test_deploy_group/archive/HEAD.zip"],'
                '"working_dir": "http://nonexistentlink-q490123950ni34t"}'
            ),
            "--working-dir",
            "https://github.com/shrekris-anyscale/test_module/archive/HEAD.zip",
        ]
    )
    wait_for_condition(lambda: ping_endpoint("one") == "2", timeout=10)
    p.send_signal(signal.SIGINT)
    p.wait()


@serve.deployment
def global_f(*args):
    return "wonderful world"


@serve.deployment
class NoArgDriver:
    def __init__(self, dag: RayServeDAGHandle):
        self.dag = dag

    async def __call__(self):
        return await self.dag.remote()


TestBuildFNode = global_f.bind()
TestBuildDagNode = NoArgDriver.bind(TestBuildFNode)


# TODO(Shreyas): Add TestBuildDagNode back once serve build new PRs out.
@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
@pytest.mark.parametrize("node", ["TestBuildFNode"])
def test_build(ray_start_stop, node):
    with NamedTemporaryFile(mode="w+", suffix=".yaml") as tmp:

        # Build an app
        subprocess.check_output(
            [
                "serve",
                "build",
                f"ray.serve.tests.test_cli.{node}",
                "-o",
                tmp.name,
            ]
        )
        subprocess.check_output(["serve", "deploy", tmp.name])
        assert ping_endpoint("") == "wonderful world"
        subprocess.check_output(["serve", "delete", "-y"])
        assert ping_endpoint("") == CONNECTION_ERROR_MSG


@pytest.mark.skipif(sys.platform == "win32", reason="File path incorrect on Windows.")
@pytest.mark.parametrize("use_command", [True, False])
def test_idempotence_after_controller_death(ray_start_stop, use_command: bool):
    """Check that CLI is idempotent even if controller dies."""

    config_file_name = os.path.join(
        os.path.dirname(__file__), "test_config_files", "two_deployments.yaml"
    )
    success_message_fragment = b"Sent deploy request successfully!"
    deploy_response = subprocess.check_output(["serve", "deploy", config_file_name])
    assert success_message_fragment in deploy_response

    ray.init(address="auto", namespace="serve")
    serve.start(detached=True)
    assert len(serve.list_deployments()) == 2

    # Kill controller
    if use_command:
        subprocess.check_output(["serve", "shutdown"])
    else:
        serve.shutdown()

    info_response = subprocess.check_output(["serve", "config"])
    info = yaml.safe_load(info_response)

    assert "deployments" in info
    assert len(info["deployments"]) == 0

    deploy_response = subprocess.check_output(["serve", "deploy", config_file_name])
    assert success_message_fragment in deploy_response

    # Restore testing controller
    serve.start(detached=True)
    assert len(serve.list_deployments()) == 2
    serve.shutdown()
    ray.shutdown()


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))
