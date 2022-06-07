import json
import tempfile
import numpy as np
import os
import sys
import subprocess
import pytest

from fastapi.encoders import jsonable_encoder

import ray
from ray import serve
from ray.serve.utils import (
    serve_encoders,
    get_deployment_import_path,
    node_id_to_ip_addr,
    merge_runtime_envs,
)


def test_node_id_to_ip_addr():
    assert node_id_to_ip_addr("node:127.0.0.1-0") == "127.0.0.1"
    assert node_id_to_ip_addr("127.0.0.1-0") == "127.0.0.1"
    assert node_id_to_ip_addr("127.0.0.1") == "127.0.0.1"
    assert node_id_to_ip_addr("node:127.0.0.1") == "127.0.0.1"


def test_bytes_encoder():
    data_before = {"inp": {"nest": b"bytes"}}
    data_after = {"inp": {"nest": "bytes"}}
    assert json.loads(json.dumps(jsonable_encoder(data_before))) == data_after


def test_numpy_encoding():
    data = [1, 2]
    floats = np.array(data).astype(np.float32)
    ints = floats.astype(np.int32)
    uints = floats.astype(np.uint32)
    list_of_uints = [np.int64(1), np.int64(2)]

    for np_data in [floats, ints, uints, list_of_uints]:
        assert (
            json.loads(
                json.dumps(jsonable_encoder(np_data, custom_encoder=serve_encoders))
            )
            == data
        )
    nested = {"a": np.array([1, 2])}
    assert json.loads(
        json.dumps(jsonable_encoder(nested, custom_encoder=serve_encoders))
    ) == {"a": [1, 2]}


@serve.deployment
def decorated_f(*args):
    return "reached decorated_f"


@ray.remote
class DecoratedActor:
    def __call__(self, *args):
        return "reached decorated_actor"


def gen_func():
    @serve.deployment
    def f():
        pass

    return f


def gen_class():
    @serve.deployment
    class A:
        pass

    return A


class TestGetDeploymentImportPath:
    def test_invalid_inline_defined(self):
        @serve.deployment
        def inline_f():
            pass

        with pytest.raises(RuntimeError, match="must be importable"):
            get_deployment_import_path(inline_f, enforce_importable=True)

        with pytest.raises(RuntimeError, match="must be importable"):
            get_deployment_import_path(gen_func(), enforce_importable=True)

        @serve.deployment
        class InlineCls:
            pass

        with pytest.raises(RuntimeError, match="must be importable"):
            get_deployment_import_path(InlineCls, enforce_importable=True)

        with pytest.raises(RuntimeError, match="must be importable"):
            get_deployment_import_path(gen_class(), enforce_importable=True)

    def test_get_import_path_basic(self):
        d = decorated_f.options()

        # CI may change the parent path, so check only that the suffix matches.
        assert get_deployment_import_path(d).endswith(
            "ray.serve.tests.test_util.decorated_f"
        )

    def test_get_import_path_nested_actor(self):
        d = serve.deployment(name="actor")(DecoratedActor)

        # CI may change the parent path, so check only that the suffix matches.
        assert get_deployment_import_path(d).endswith(
            "ray.serve.tests.test_util.DecoratedActor"
        )

    @pytest.mark.skipif(
        sys.platform == "win32", reason="File path incorrect on Windows."
    )
    def test_replace_main(self):

        temp_fname = "testcase.py"
        expected_import_path = "testcase.main_f"

        code = (
            "from ray import serve\n"
            "from ray.serve.utils import get_deployment_import_path\n"
            "@serve.deployment\n"
            "def main_f(*args):\n"
            "\treturn 'reached main_f'\n"
            "assert get_deployment_import_path(main_f, replace_main=True) == "
            f"'{expected_import_path}'"
        )

        with tempfile.TemporaryDirectory() as dirpath:
            full_fname = os.path.join(dirpath, temp_fname)

            with open(full_fname, "w+") as f:
                f.write(code)

            subprocess.check_output(["python", full_fname])


class TestMergeRuntimeEnvs:
    def test_merge_empty(self):
        assert {"env_vars": {}} == merge_runtime_envs({}, {})

    def test_merge_empty_parent(self):
        child = {"env_vars": {"test1": "test_val"}, "working_dir": "."}
        assert child == merge_runtime_envs({}, child)

    def test_merge_empty_child(self):
        parent = {"env_vars": {"test1": "test_val"}, "working_dir": "."}
        assert parent == merge_runtime_envs(parent, {})

    @pytest.mark.parametrize("invalid_env", [None, 0, "runtime_env", set()])
    def test_invalid_type(self, invalid_env):
        with pytest.raises(TypeError):
            merge_runtime_envs(invalid_env, {})
        with pytest.raises(TypeError):
            merge_runtime_envs({}, invalid_env)
        with pytest.raises(TypeError):
            merge_runtime_envs(invalid_env, invalid_env)

    def test_basic_merge(self):
        parent = {
            "py_modules": ["http://test.com/test0.zip", "s3://path/test1.zip"],
            "working_dir": "gs://path/test2.zip",
            "env_vars": {"test": "val", "trial": "val2"},
            "pip": ["pandas", "numpy"],
            "excludes": ["my_file.txt"],
        }
        original_parent = parent.copy()

        child = {
            "py_modules": [],
            "working_dir": "s3://path/test1.zip",
            "env_vars": {"test": "val", "trial": "val2"},
            "pip": ["numpy"],
        }
        original_child = child.copy()

        merged = merge_runtime_envs(parent, child)

        assert original_parent == parent
        assert original_child == child
        assert merged == {
            "py_modules": [],
            "working_dir": "s3://path/test1.zip",
            "env_vars": {"test": "val", "trial": "val2"},
            "pip": ["numpy"],
            "excludes": ["my_file.txt"],
        }

    def test_merge_deep_copy(self):
        """Check that merge_runtime_envs actually deep copies the env values."""

        parent_env_vars = {"parent": "pval"}
        child_env_vars = {"child": "cval"}

        parent = {"env_vars": parent_env_vars}
        child = {"env_vars": child_env_vars}
        original_parent = parent.copy()
        original_child = child.copy()

        merged = merge_runtime_envs(parent, child)
        assert merged["env_vars"] == {"parent": "pval", "child": "cval"}
        assert original_parent == parent
        assert original_child == child

    def test_merge_empty_env_vars(self):
        env_vars = {"test": "val", "trial": "val2"}

        non_empty = {"env_vars": {"test": "val", "trial": "val2"}}
        empty = {}

        assert env_vars == merge_runtime_envs(non_empty, empty)["env_vars"]
        assert env_vars == merge_runtime_envs(empty, non_empty)["env_vars"]
        assert {} == merge_runtime_envs(empty, empty)["env_vars"]

    def test_merge_env_vars(self):
        parent = {
            "py_modules": ["http://test.com/test0.zip", "s3://path/test1.zip"],
            "working_dir": "gs://path/test2.zip",
            "env_vars": {"parent": "pval", "override": "old"},
            "pip": ["pandas", "numpy"],
            "excludes": ["my_file.txt"],
        }

        child = {
            "py_modules": [],
            "working_dir": "s3://path/test1.zip",
            "env_vars": {"child": "cval", "override": "new"},
            "pip": ["numpy"],
        }

        merged = merge_runtime_envs(parent, child)
        assert merged == {
            "py_modules": [],
            "working_dir": "s3://path/test1.zip",
            "env_vars": {"parent": "pval", "child": "cval", "override": "new"},
            "pip": ["numpy"],
            "excludes": ["my_file.txt"],
        }


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
