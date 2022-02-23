import pytest
import subprocess
import requests
import json
from typing import List, Dict
import time

@pytest.fixture
def serve_start_stop():
    subprocess.check_output(["ray", "start", "--head"])
    subprocess.check_output(["serve", "start"])
    yield
    subprocess.check_output(["ray", "stop", "--force"])

def deployments_match(list1: List[Dict], list2: List[Dict]) -> bool:
    # Helper that takes in 2 lists of deployment dictionaries and compares them

    if len(list1) != len(list2):
        return False

    for deployment1 in list1:
        matching_deployment = None
        for i in range(len(list2)):
            deployment2 = list2[i]
            if deployment1 == deployment2:
                matching_deployment = i
        if matching_deployment is None:
            return False
        list2.pop(matching_deployment)

    return len(list2) == 0

def test_put_get_success(serve_start_stop):
    URL = "http://localhost:8265/api/serve/deployments/"
    test_env_uri = (
        "https://github.com/shrekris-anyscale/test_deploy_group/archive/HEAD.zip"
    )
    test_module_uri = (
        "https://github.com/shrekris-anyscale/test_module/archive/HEAD.zip"
    )

    ray_actor_options = {
        "runtime_env": {"py_modules": [test_env_uri, test_module_uri]}
    }

    shallow = dict(
        name="shallow",
        num_replicas=3,
        route_prefix="/shallow",
        ray_actor_options=ray_actor_options,
        import_path="test_env.shallow_import.ShallowClass"
    )

    deep = dict(
        name="deep",
        route_prefix="/deep",
        ray_actor_options=ray_actor_options,
        import_path="test_env.subdir1.subdir2.deep_import.DeepClass"
    )

    one = dict(
        name="one",
        num_replicas=3,
        route_prefix="/one",
        ray_actor_options=ray_actor_options,
        import_path="test_module.test.one"
    )

    # Ensure the REST API is idempotent
    for _ in range(3):
        deployments = [shallow, deep, one]

        put_response = requests.put(URL, json={"deployments": deployments})
        assert put_response.status_code == 200
        assert requests.get("http://localhost:8000/shallow").text == "Hello shallow world!"
        assert requests.get("http://localhost:8000/deep").text == "Hello deep world!"
        assert requests.get("http://localhost:8000/one").text == "2"

        get_response = requests.get(URL)
        assert get_response.status_code == 200

        with open("check_json.json", "w+") as f:
            f.write(get_response.json())
        
        with open("three_deployments_response.json", "r") as f:
            response_deployments = json.loads(get_response.json())["deployments"]
            expected_deployments = json.load(f)["deployments"]
            assert deployments_match(response_deployments, expected_deployments)

        deployments = [shallow, one]
        put_response = requests.put(URL, json={"deployments": deployments})
        assert put_response.status_code == 200
        assert requests.get("http://localhost:8000/shallow").text == "Hello shallow world!"
        assert requests.get("http://localhost:8000/deep").status_code == 404
        assert requests.get("http://localhost:8000/one").text == "2"

        get_response = requests.get(URL)
        assert get_response.status_code == 200

        with open("two_deployments_response.json", "r") as f:
            response_deployments = json.loads(get_response.json())["deployments"]
            expected_deployments = json.load(f)["deployments"]
            assert deployments_match(response_deployments, expected_deployments)

def test_delete_success(serve_start_stop):
    URL = "http://localhost:8265/api/serve/deployments/"

    ray_actor_options = {
        "runtime_env": {"working_dir": (
            "https://github.com/shrekris-anyscale/test_deploy_group/archive/HEAD.zip"
        )}
    }

    shallow = dict(
        name="shallow",
        num_replicas=3,
        route_prefix="/shallow",
        ray_actor_options=ray_actor_options,
        import_path="test_env.shallow_import.ShallowClass"
    )

    # Ensure the REST API is idempotent
    for _ in range(5):
        put_response = requests.put(URL, json={"deployments": [shallow]})
        assert put_response.status_code == 200
        assert requests.get("http://localhost:8000/shallow").text == "Hello shallow world!"

        delete_response = requests.delete(URL)
        assert delete_response.status_code == 200

        # Make sure no deployments exist
        get_response = requests.get(URL)
        assert len(json.loads(get_response.json())["deployments"]) == 0