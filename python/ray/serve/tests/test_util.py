import json

import numpy as np
import pytest

import ray
from ray import serve
from ray.serve.utils import ServeEncoder, get_deployment_import_path


def test_bytes_encoder():
    data_before = {"inp": {"nest": b"bytes"}}
    data_after = {"inp": {"nest": "bytes"}}
    assert json.loads(json.dumps(data_before, cls=ServeEncoder)) == data_after


def test_numpy_encoding():
    data = [1, 2]
    floats = np.array(data).astype(np.float32)
    ints = floats.astype(np.int32)
    uints = floats.astype(np.uint32)

    assert json.loads(json.dumps(floats, cls=ServeEncoder)) == data
    assert json.loads(json.dumps(ints, cls=ServeEncoder)) == data
    assert json.loads(json.dumps(uints, cls=ServeEncoder)) == data


@serve.deployment
def decorated_f(*args):
    return "reached decorated_f"


@ray.remote
class DecoratedActor:
    def __call__(self, *args):
        return "reached decorated_actor"


class TestGetDeploymentImportPath:
    def test_get_import_path_basic(self):
        d = decorated_f.options()
        assert get_deployment_import_path(d) == "ray.serve.tests.test_util.decorated_f"

    def test_get_import_path_nested_actor(self):
        d = serve.deployment(name="actor")(DecoratedActor)
        assert (
            get_deployment_import_path(d) == "ray.serve.tests.test_util.DecoratedActor"
        )


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
