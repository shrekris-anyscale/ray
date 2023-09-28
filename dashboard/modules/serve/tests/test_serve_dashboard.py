import copy
import grpc
import os
import sys
from typing import Dict

import pytest
import requests

import ray
from ray import serve
from ray._private.test_utils import wait_for_condition
import ray._private.ray_constants as ray_constants
from ray.util.state import list_actors
from ray.serve._private.constants import SERVE_NAMESPACE, MULTI_APP_MIGRATION_MESSAGE
from ray.serve.tests.conftest import *  # noqa: F401 F403
from ray.serve.schema import ServeInstanceDetails, HTTPOptionsSchema
from ray.serve._private.common import (
    ApplicationStatus,
    DeploymentStatus,
    ReplicaState,
    ProxyStatus,
)
from ray.serve.generated import serve_pb2, serve_pb2_grpc


# For local testing. If you're running these tests locally on a Macbook, set
# this variable to False to enable tests.
DISABLE_DARWIN = False


SERVE_AGENT_URLs = {
    "GET_OR_PUT": "http://localhost:52365/api/serve/deployments/",
    "STATUS": "http://localhost:52365/api/serve/deployments/status",
    "GET_OR_PUT_V2": "http://localhost:52365/api/serve/applications/",
}

SERVE_HEAD_URLs = {
    "GET_OR_PUT": "http://localhost:8265/api/serve/deployments/",
    "STATUS": "http://localhost:8265/api/serve/deployments/status",
    "GET_OR_PUT_V2": "http://localhost:8265/api/serve/applications/",
}


def deploy_and_check_config(config: Dict, urls: Dict[str, str]):
    put_response = requests.put(urls["GET_OR_PUT"], json=config, timeout=30)
    assert put_response.status_code == 200
    print("PUT request sent successfully.")

    # Config should be immediately retrievable
    get_response = requests.get(urls["GET_OR_PUT"], timeout=15)
    assert get_response.status_code == 200
    assert get_response.json() == config
    print("GET request returned correct config.")


def deploy_config_multi_app(config: Dict, urls: Dict[str, str]):
    put_response = requests.put(urls["GET_OR_PUT_V2"], json=config, timeout=30)
    assert put_response.status_code == 200
    print("PUT request sent successfully.")


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
def test_put_get(ray_start_stop):
    config1 = {
        "import_path": (
            "ray.serve.tests.test_config_files.test_dag.conditional_dag.serve_dag"
        ),
        "runtime_env": {},
        "deployments": [
            {
                "name": "Multiplier",
                "user_config": {"factor": 1},
            },
            {
                "name": "Adder",
                "ray_actor_options": {
                    "runtime_env": {"env_vars": {"override_increment": "1"}}
                },
            },
        ],
    }

    # Use empty dictionary for Adder's ray_actor_options.
    config2 = copy.deepcopy(config1)
    config2["deployments"][1]["ray_actor_options"] = {}

    config3 = {
        "import_path": "ray.serve.tests.test_config_files.world.DagNode",
    }

    # Ensure the REST API is idempotent
    num_iterations = 2
    for iteration in range(1, num_iterations + 1):
        print(f"*** Starting Iteration {iteration}/{num_iterations} ***\n")

        print("Sending PUT request for config1.")
        deploy_and_check_config(config1, SERVE_HEAD_URLs)
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
        print("Deployments are live and reachable over HTTP.\n")

        print("Sending PUT request for config2.")
        deploy_and_check_config(config2, SERVE_HEAD_URLs)
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/", json=["ADD", 2]).json()
            == "4 pizzas please!",
            timeout=15,
        )
        print("Adder deployment updated correctly.\n")

        print("Sending PUT request for config3.")
        deploy_and_check_config(config3, SERVE_HEAD_URLs)
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/").text == "wonderful world",
            timeout=15,
        )
        print("Deployments are live and reachable over HTTP.\n")


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
@pytest.mark.parametrize("urls", [SERVE_AGENT_URLs, SERVE_HEAD_URLs])
def test_put_get_multi_app(ray_start_stop, urls):
    pizza_import_path = (
        "ray.serve.tests.test_config_files.test_dag.conditional_dag.serve_dag"
    )
    world_import_path = "ray.serve.tests.test_config_files.world.DagNode"
    config1 = {
        "http_options": {
            "host": "127.0.0.1",
            "port": 8000,
        },
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/app1",
                "import_path": pizza_import_path,
                "deployments": [
                    {
                        "name": "Adder",
                        "ray_actor_options": {
                            "runtime_env": {"env_vars": {"override_increment": "3"}}
                        },
                    },
                    {
                        "name": "Multiplier",
                        "ray_actor_options": {
                            "runtime_env": {"env_vars": {"override_factor": "4"}}
                        },
                    },
                ],
            },
            {
                "name": "app2",
                "route_prefix": "/app2",
                "import_path": world_import_path,
            },
        ],
    }

    # Use empty dictionary for app1 Adder's ray_actor_options.
    config2 = copy.deepcopy(config1)
    config2["applications"][0]["deployments"][0]["ray_actor_options"] = {}

    config3 = copy.deepcopy(config1)
    config3["applications"][0] = {
        "name": "app1",
        "route_prefix": "/app1",
        "import_path": world_import_path,
    }

    # Ensure the REST API is idempotent
    num_iterations = 2
    for iteration in range(num_iterations):
        print(f"*** Starting Iteration {iteration + 1}/{num_iterations} ***\n")

        # APPLY CONFIG 1
        print("Sending PUT request for config1.")
        deploy_config_multi_app(config1, urls)
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app1", json=["ADD", 2]).json()
            == "5 pizzas please!",
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app1", json=["MUL", 2]).json()
            == "8 pizzas please!",
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app2").text
            == "wonderful world",
            timeout=15,
        )
        print("Deployments are live and reachable over HTTP.\n")

        # APPLY CONFIG 2: App #1 Adder should add 2 to input.
        print("Sending PUT request for config2.")
        deploy_config_multi_app(config2, urls)
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app1", json=["ADD", 2]).json()
            == "4 pizzas please!",
            timeout=15,
        )
        print("Adder deployment updated correctly.\n")

        # APPLY CONFIG 3: App #1 should be overwritten to world:DagNode
        print("Sending PUT request for config3.")
        deploy_config_multi_app(config3, urls)
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app1").text
            == "wonderful world",
            timeout=15,
        )
        print("Deployments are live and reachable over HTTP.\n")


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
@pytest.mark.parametrize(
    "put_url",
    [
        SERVE_AGENT_URLs["GET_OR_PUT"],
        SERVE_AGENT_URLs["GET_OR_PUT_V2"],
        SERVE_HEAD_URLs["GET_OR_PUT"],
        SERVE_HEAD_URLs["GET_OR_PUT_V2"],
    ],
)
def test_put_bad_schema(ray_start_stop, put_url: str):
    config = {"not_a_real_field": "value"}

    put_response = requests.put(put_url, json=config, timeout=5)
    assert put_response.status_code == 400


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
@pytest.mark.parametrize("urls", [SERVE_AGENT_URLs, SERVE_HEAD_URLs])
def test_put_duplicate_apps(ray_start_stop, urls):
    """If a config with duplicate app names is deployed, the PUT request should fail.
    The response should clearly indicate a validation error.
    """

    config = {
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/a",
                "import_path": "module.graph",
            },
            {
                "name": "app1",
                "route_prefix": "/b",
                "import_path": "module.graph",
            },
        ],
    }
    put_response = requests.put(urls["GET_OR_PUT_V2"], json=config, timeout=5)
    assert put_response.status_code == 400 and "ValidationError" in put_response.text


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
@pytest.mark.parametrize("urls", [SERVE_AGENT_URLs, SERVE_HEAD_URLs])
def test_put_duplicate_routes(ray_start_stop, urls):
    """If a config with duplicate routes is deployed, the PUT request should fail.
    The response should clearly indicate a validation error.
    """

    config = {
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/alice",
                "import_path": "module.graph",
            },
            {
                "name": "app2",
                "route_prefix": "/alice",
                "import_path": "module.graph",
            },
        ],
    }
    put_response = requests.put(urls["GET_OR_PUT_V2"], json=config, timeout=5)
    assert put_response.status_code == 400 and "ValidationError" in put_response.text


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
def test_delete(ray_start_stop):
    config = {
        "import_path": "dir.subdir.a.add_and_sub.serve_dag",
        "runtime_env": {
            "working_dir": (
                "https://github.com/ray-project/test_dag/archive/"
                "40d61c141b9c37853a7014b8659fc7f23c1d04f6.zip"
            )
        },
        "deployments": [
            {
                "name": "Subtract",
                "ray_actor_options": {
                    "runtime_env": {
                        "py_modules": [
                            (
                                "https://github.com/ray-project/test_module/archive/"
                                "aa6f366f7daa78c98408c27d917a983caa9f888b.zip"
                            )
                        ]
                    }
                },
            }
        ],
    }

    # Ensure the REST API is idempotent
    num_iterations = 2
    for iteration in range(1, num_iterations + 1):
        print(f"*** Starting Iteration {iteration}/{num_iterations} ***\n")

        print("Sending PUT request for config.")
        deploy_and_check_config(config, SERVE_HEAD_URLs)
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/", json=["ADD", 1]).json()
            == 2,
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/", json=["SUB", 1]).json()
            == -1,
            timeout=15,
        )
        print("Deployments are live and reachable over HTTP.\n")

        print("Sending DELETE request for config.")
        delete_response = requests.delete(SERVE_HEAD_URLs["GET_OR_PUT"], timeout=15)
        assert delete_response.status_code == 200
        print("DELETE request sent successfully.")

        # Make sure all deployments are deleted
        wait_for_condition(
            lambda: len(
                requests.get(SERVE_HEAD_URLs["GET_OR_PUT"], timeout=15).json()[
                    "deployments"
                ]
            )
            == 0,
            timeout=15,
        )
        print("GET request returned empty config successfully.")

        with pytest.raises(requests.exceptions.ConnectionError):
            requests.post("http://localhost:8000/", json=["ADD", 1]).raise_for_status()
        with pytest.raises(requests.exceptions.ConnectionError):
            requests.post("http://localhost:8000/", json=["SUB", 1]).raise_for_status()
        print("Deployments have been deleted and are not reachable.\n")


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
@pytest.mark.parametrize("urls", [SERVE_AGENT_URLs, SERVE_HEAD_URLs])
def test_delete_multi_app(ray_start_stop, urls):
    py_module = (
        "https://github.com/ray-project/test_module/archive/"
        "aa6f366f7daa78c98408c27d917a983caa9f888b.zip"
    )
    config = {
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/app1",
                "import_path": "dir.subdir.a.add_and_sub.serve_dag",
                "runtime_env": {
                    "working_dir": (
                        "https://github.com/ray-project/test_dag/archive/"
                        "40d61c141b9c37853a7014b8659fc7f23c1d04f6.zip"
                    )
                },
                "deployments": [
                    {
                        "name": "Subtract",
                        "ray_actor_options": {
                            "runtime_env": {"py_modules": [py_module]}
                        },
                    }
                ],
            },
            {
                "name": "app2",
                "route_prefix": "/app2",
                "import_path": "ray.serve.tests.test_config_files.world.DagNode",
            },
        ],
    }

    # Ensure the REST API is idempotent
    num_iterations = 2
    for iteration in range(1, num_iterations + 1):
        print(f"*** Starting Iteration {iteration}/{num_iterations} ***\n")

        print("Sending PUT request for config.")
        deploy_config_multi_app(config, urls)
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app1", json=["ADD", 1]).json()
            == 2,
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app1", json=["SUB", 1]).json()
            == -1,
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app2").text
            == "wonderful world",
            timeout=15,
        )
        print("Deployments are live and reachable over HTTP.\n")

        print("Sending DELETE request for config.")
        delete_response = requests.delete(urls["GET_OR_PUT_V2"], timeout=15)
        assert delete_response.status_code == 200
        print("DELETE request sent successfully.")

        wait_for_condition(
            lambda: len(
                list_actors(
                    filters=[
                        ("ray_namespace", "=", SERVE_NAMESPACE),
                        ("state", "=", "ALIVE"),
                    ]
                )
            )
            == 0
        )

        with pytest.raises(requests.exceptions.ConnectionError):
            requests.post(
                "http://localhost:8000/app1", json=["ADD", 1]
            ).raise_for_status()
        with pytest.raises(requests.exceptions.ConnectionError):
            requests.post("http://localhost:8000/app2").raise_for_status()
        print("Deployments have been deleted and are not reachable.\n")


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
@pytest.mark.parametrize("urls", [SERVE_AGENT_URLs, SERVE_HEAD_URLs])
def test_get_status(ray_start_stop, urls):
    print("Checking status info before any deployments.")

    status_response = requests.get(urls["STATUS"], timeout=30)
    assert status_response.status_code == 200
    print("Retrieved status info successfully.")

    serve_status = status_response.json()
    assert len(serve_status["deployment_statuses"]) == 0
    assert serve_status["app_status"]["status"] == "NOT_STARTED"
    assert serve_status["app_status"]["deployment_timestamp"] == 0
    assert serve_status["app_status"]["message"] == ""
    print("Status info on fresh Serve application is correct.\n")

    config = {
        "import_path": "ray.serve.tests.test_config_files.world.DagNode",
    }

    print("Deploying config.")
    deploy_and_check_config(config, urls)
    wait_for_condition(
        lambda: requests.post("http://localhost:8000/").text == "wonderful world",
        timeout=15,
    )
    print("Deployments are live and reachable over HTTP.\n")

    status_response = requests.get(urls["STATUS"], timeout=30)
    assert status_response.status_code == 200
    print("Retrieved status info successfully.")

    serve_status = status_response.json()

    deployment_statuses = serve_status["deployment_statuses"]
    assert len(deployment_statuses) == 2
    expected_deployment_names = {"f", "BasicDriver"}
    for deployment_status in deployment_statuses:
        assert deployment_status["name"] in expected_deployment_names
        expected_deployment_names.remove(deployment_status["name"])
        assert deployment_status["status"] in {"UPDATING", "HEALTHY"}
        assert deployment_status["message"] == ""
    assert len(expected_deployment_names) == 0
    print("Deployments' statuses are correct.")

    assert serve_status["app_status"]["status"] in {"DEPLOYING", "RUNNING"}
    assert serve_status["app_status"]["deployment_timestamp"] > 0
    assert serve_status["app_status"]["message"] == ""
    print("Serve app status is correct.")


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
@pytest.mark.parametrize("urls", [SERVE_AGENT_URLs, SERVE_HEAD_URLs])
def test_get_serve_instance_details_not_started(ray_start_stop, urls):
    """Test REST API when Serve hasn't started yet."""

    ServeInstanceDetails(**requests.get(urls["GET_OR_PUT_V2"]).json())


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
@pytest.mark.parametrize(
    "f_deployment_options",
    [
        {"name": "f", "ray_actor_options": {"num_cpus": 0.2}},
        {
            "name": "f",
            "autoscaling_config": {
                "min_replicas": 1,
                "initial_replicas": 3,
                "max_replicas": 10,
            },
        },
    ],
)
@pytest.mark.parametrize("urls", [SERVE_AGENT_URLs, SERVE_HEAD_URLs])
def test_get_serve_instance_details(ray_start_stop, f_deployment_options, urls):
    grpc_port = 9001
    grpc_servicer_functions = [
        "ray.serve.generated.serve_pb2_grpc.add_UserDefinedServiceServicer_to_server",
        "ray.serve.generated.serve_pb2_grpc.add_FruitServiceServicer_to_server",
    ]
    world_import_path = "ray.serve.tests.test_config_files.world.DagNode"
    fastapi_import_path = "ray.serve.tests.test_config_files.fastapi_deployment.node"
    config = {
        "proxy_location": "HeadOnly",
        "http_options": {
            "host": "127.0.0.1",
            "port": 8005,
        },
        "grpc_options": {
            "port": grpc_port,
            "grpc_servicer_functions": grpc_servicer_functions,
        },
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/apple",
                "import_path": world_import_path,
                "deployments": [f_deployment_options],
            },
            {
                "name": "app2",
                "route_prefix": "/banana",
                "import_path": fastapi_import_path,
            },
        ],
    }
    expected_values = {
        "app1": {
            "route_prefix": "/apple",
            "docs_path": None,
            "deployments": {"f", "BasicDriver"},
        },
        "app2": {
            "route_prefix": "/banana",
            "docs_path": "/my_docs",
            "deployments": {"FastAPIDeployment"},
        },
    }

    deploy_config_multi_app(config, urls)

    def applications_running():
        response = requests.get(urls["GET_OR_PUT_V2"], timeout=15)
        assert response.status_code == 200

        serve_details = ServeInstanceDetails(**response.json())
        return (
            serve_details.applications["app1"].status == ApplicationStatus.RUNNING
            and serve_details.applications["app2"].status == ApplicationStatus.RUNNING
        )

    wait_for_condition(applications_running, timeout=15)
    print("All applications are in a RUNNING state.")

    serve_details = ServeInstanceDetails(**requests.get(urls["GET_OR_PUT_V2"]).json())
    # CHECK: proxy location, HTTP host, and HTTP port
    assert serve_details.proxy_location == "HeadOnly"
    assert serve_details.http_options.host == "127.0.0.1"
    assert serve_details.http_options.port == 8005

    # CHECK: gRPC port and grpc_servicer_functions
    assert serve_details.grpc_options.port == grpc_port
    assert serve_details.grpc_options.grpc_servicer_functions == grpc_servicer_functions
    print(
        "Confirmed fetched proxy location, HTTP host, HTTP port, gRPC port, and grpc_"
        "servicer_functions metadata correct."
    )

    # Check HTTP Proxy statuses
    for proxy in serve_details.proxies.values():
        assert proxy.status == ProxyStatus.HEALTHY
        assert os.path.exists("/tmp/ray/session_latest/logs" + proxy.log_file_path)
    print("Checked HTTP Proxy details.")
    # Check controller info
    assert serve_details.controller_info.actor_id
    assert serve_details.controller_info.actor_name
    assert serve_details.controller_info.node_id
    assert serve_details.controller_info.node_ip
    assert os.path.exists(
        "/tmp/ray/session_latest/logs" + serve_details.controller_info.log_file_path
    )

    app_details = serve_details.applications
    # CHECK: application details
    for i, app in enumerate(["app1", "app2"]):
        assert (
            app_details[app].deployed_app_config.dict(exclude_unset=True)
            == config["applications"][i]
        )
        assert app_details[app].last_deployed_time_s > 0
        assert app_details[app].route_prefix == expected_values[app]["route_prefix"]
        assert app_details[app].docs_path == expected_values[app]["docs_path"]

        # CHECK: all deployments are present
        assert (
            app_details[app].deployments.keys() == expected_values[app]["deployments"]
        )

        for deployment in app_details[app].deployments.values():
            assert deployment.status == DeploymentStatus.HEALTHY
            # Route prefix should be app level options eventually
            assert "route_prefix" not in deployment.deployment_config.dict(
                exclude_unset=True
            )
            if isinstance(deployment.deployment_config.num_replicas, int):
                assert (
                    len(deployment.replicas)
                    == deployment.deployment_config.num_replicas
                )

            for replica in deployment.replicas:
                assert replica.state == ReplicaState.RUNNING
                assert (
                    deployment.name in replica.replica_id
                    and deployment.name in replica.actor_name
                )
                assert replica.actor_id and replica.node_id and replica.node_ip
                assert replica.start_time_s > app_details[app].last_deployed_time_s
                file_path = "/tmp/ray/session_latest/logs" + replica.log_file_path
                assert os.path.exists(file_path)

    print("Finished checking application details.")


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
def test_put_single_then_multi(ray_start_stop):
    world_import_path = "ray.serve.tests.test_config_files.world.DagNode"
    pizza_import_path = "ray.serve.tests.test_config_files.pizza.serve_dag"
    multi_app_config = {
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/app1",
                "import_path": world_import_path,
            },
            {
                "name": "app2",
                "route_prefix": "/app2",
                "import_path": pizza_import_path,
            },
        ],
    }
    single_app_config = {"import_path": world_import_path}

    def check_app():
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/").text == "wonderful world",
            timeout=15,
        )

    # Deploy single app config
    deploy_and_check_config(single_app_config, SERVE_HEAD_URLs)
    check_app()

    # Deploying multi app config afterwards should fail
    put_response = requests.put(
        SERVE_HEAD_URLs["GET_OR_PUT_V2"], json=multi_app_config, timeout=5
    )
    assert put_response.status_code == 400
    print(put_response.text)

    # The original application should still be up and running
    check_app()


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
def test_put_multi_then_single(ray_start_stop):
    world_import_path = "ray.serve.tests.test_config_files.world.DagNode"
    pizza_import_path = "ray.serve.tests.test_config_files.pizza.serve_dag"
    multi_app_config = {
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/app1",
                "import_path": world_import_path,
            },
            {
                "name": "app2",
                "route_prefix": "/app2",
                "import_path": pizza_import_path,
            },
        ],
    }
    single_app_config = {"import_path": world_import_path}

    def check_apps():
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app1").text
            == "wonderful world",
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app2", json=["ADD", 2]).text
            == "4 pizzas please!",
            timeout=15,
        )
        wait_for_condition(
            lambda: requests.post("http://localhost:8000/app2", json=["MUL", 2]).text
            == "6 pizzas please!",
            timeout=15,
        )

    # Deploy multi app config
    deploy_config_multi_app(multi_app_config, SERVE_HEAD_URLs)
    check_apps()

    # Deploying single app config afterwards should fail
    put_response = requests.put(
        SERVE_HEAD_URLs["GET_OR_PUT"], json=single_app_config, timeout=5
    )
    assert put_response.status_code == 400

    # The original applications should still be up and running
    check_apps()


@pytest.mark.parametrize("name", ["", "my_app"])
def test_put_single_with_name(ray_start_stop, name):
    world_import_path = "ray.serve.tests.test_config_files.world.DagNode"
    single_app_config = {"name": name, "import_path": world_import_path}

    resp = requests.put(
        SERVE_HEAD_URLs["GET_OR_PUT"], json=single_app_config, timeout=30
    )
    assert resp.status_code == 400
    # Error should tell user specifying the name is not allowed
    assert "name" in resp.text
    assert MULTI_APP_MIGRATION_MESSAGE in resp.text


@pytest.mark.skipif(sys.platform == "darwin" and DISABLE_DARWIN, reason="Flaky on OSX.")
def test_serve_namespace(ray_start_stop):
    """Check that the driver can interact with Serve using the Python API."""

    config = {
        "applications": [
            {
                "name": "my_app",
                "import_path": "ray.serve.tests.test_config_files.world.DagNode",
            }
        ],
    }

    print("Deploying config.")
    deploy_config_multi_app(config, SERVE_HEAD_URLs)
    wait_for_condition(
        lambda: requests.post("http://localhost:8000/").text == "wonderful world",
        timeout=15,
    )
    print("Deployments are live and reachable over HTTP.\n")

    ray.init(address="auto", namespace="serve")
    my_app_status = serve.status().applications["my_app"]
    assert (
        len(my_app_status.deployments) == 2
        and my_app_status.deployments["f"] is not None
    )
    print("Successfully retrieved deployment statuses with Python API.")
    print("Shutting down Python API.")
    serve.shutdown()
    ray.shutdown()


@pytest.mark.parametrize(
    "option,override",
    [
        ("proxy_location", "HeadOnly"),
        ("http_options", {"host": "127.0.0.2"}),
        ("http_options", {"port": 8000}),
        ("http_options", {"root_path": "/serve_updated"}),
    ],
)
def test_put_with_http_options(ray_start_stop, option, override):
    """Submits a config with HTTP options specified.

    Trying to submit a config to the serve agent with the HTTP options modified should
    NOT fail:
      - If Serve is NOT running, HTTP options will be honored when starting Serve
      - If Serve is running, HTTP options will be ignored, and warning will be logged
      urging users to restart Serve if they want their options to take effect
    """

    pizza_import_path = "ray.serve.tests.test_config_files.pizza.serve_dag"
    world_import_path = "ray.serve.tests.test_config_files.world.DagNode"
    original_http_options_json = {
        "host": "127.0.0.1",
        "port": 8000,
        "root_path": "/serve",
    }
    original_serve_config_json = {
        "proxy_location": "EveryNode",
        "http_options": original_http_options_json,
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/app1",
                "import_path": pizza_import_path,
            },
            {
                "name": "app2",
                "route_prefix": "/app2",
                "import_path": world_import_path,
            },
        ],
    }
    deploy_config_multi_app(original_serve_config_json, SERVE_HEAD_URLs)

    # Wait for deployments to be up
    wait_for_condition(
        lambda: requests.post("http://localhost:8000/serve/app1", json=["ADD", 2]).text
        == "4 pizzas please!",
        timeout=15,
    )
    wait_for_condition(
        lambda: requests.post("http://localhost:8000/serve/app2").text
        == "wonderful world",
        timeout=15,
    )

    updated_serve_config_json = copy.deepcopy(original_serve_config_json)
    updated_serve_config_json[option] = override

    put_response = requests.put(
        SERVE_HEAD_URLs["GET_OR_PUT_V2"], json=updated_serve_config_json, timeout=5
    )
    assert put_response.status_code == 200

    # Fetch Serve status and confirm that HTTP options are unchanged
    get_response = requests.get(SERVE_HEAD_URLs["GET_OR_PUT_V2"], timeout=5)
    serve_details = ServeInstanceDetails.parse_obj(get_response.json())

    original_http_options = HTTPOptionsSchema.parse_obj(original_http_options_json)

    assert original_http_options == serve_details.http_options.dict(exclude_unset=True)

    # Deployments should still be up
    assert (
        requests.post("http://localhost:8000/serve/app1", json=["ADD", 2]).text
        == "4 pizzas please!"
    )
    assert requests.post("http://localhost:8000/serve/app2").text == "wonderful world"


def test_put_with_grpc_options(ray_start_stop):
    """Submits a config with gRPC options specified.

    Ensure gRPC options can be accepted by the api. HTTP deployment continue to
    accept requests. gRPC deployment is also able to accept requests.
    """

    grpc_servicer_functions = [
        "ray.serve.generated.serve_pb2_grpc.add_UserDefinedServiceServicer_to_server",
    ]
    test_files_import_path = "ray.serve.tests.test_config_files."
    grpc_import_path = f"{test_files_import_path}grpc_deployment:g"
    world_import_path = "ray.serve.tests.test_config_files.world.DagNode"
    original_config = {
        "proxy_location": "EveryNode",
        "http_options": {
            "host": "127.0.0.1",
            "port": 8000,
            "root_path": "/serve",
        },
        "grpc_options": {
            "port": 9000,
            "grpc_servicer_functions": grpc_servicer_functions,
        },
        "applications": [
            {
                "name": "app1",
                "route_prefix": "/app1",
                "import_path": grpc_import_path,
            },
            {
                "name": "app2",
                "route_prefix": "/app2",
                "import_path": world_import_path,
            },
        ],
    }
    # Ensure api can accept config with gRPC options
    deploy_config_multi_app(original_config, SERVE_HEAD_URLs)

    # Ensure HTTP requests are still working
    wait_for_condition(
        lambda: requests.post("http://localhost:8000/serve/app2").text
        == "wonderful world",
        timeout=15,
    )

    # Ensure gRPC requests also work
    channel = grpc.insecure_channel("localhost:9000")
    stub = serve_pb2_grpc.UserDefinedServiceStub(channel)
    test_in = serve_pb2.UserDefinedMessage(name="foo", num=30)
    metadata = (("application", "app1"),)
    response = stub.Method1(request=test_in, metadata=metadata)
    assert response.greeting == "Hello foo from method1"


def test_default_dashboard_agent_listen_port():
    """
    Defaults in the code and the documentation assume
    the dashboard agent listens to HTTP on port 52365.
    """
    assert ray_constants.DEFAULT_DASHBOARD_AGENT_LISTEN_PORT == 52365


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
