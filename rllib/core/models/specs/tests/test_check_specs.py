import abc
import time
import unittest
from typing import Dict, Any, Type

import numpy as np
import torch

from ray.rllib.core.models.specs.checker import SpecCheckingError
from ray.rllib.core.models.specs.checker import (
    convert_to_canonical_format,
    check_input_specs,
    check_output_specs,
)
from ray.rllib.core.models.specs.specs_base import TensorSpec, TypeSpec
from ray.rllib.core.models.specs.specs_dict import SpecDict
from ray.rllib.utils.annotations import override

ONLY_ONE_KEY_ALLOWED = "Only one key is allowed in the data dict."


class AbstractInterfaceClass(abc.ABC):
    """An abstract class that has a couple of methods, each having their own
    input/output constraints."""

    @property
    @abc.abstractmethod
    def input_specs(self) -> SpecDict:
        pass

    @property
    @abc.abstractmethod
    def output_specs(self) -> SpecDict:
        pass

    @check_input_specs("input_specs", cache=False, only_check_on_retry=False)
    @check_output_specs("output_specs", cache=False)
    def check_input_and_output(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        return self._check_input_and_output(input_dict)

    @abc.abstractmethod
    def _check_input_and_output(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        pass

    @check_input_specs("input_specs", cache=False, only_check_on_retry=False)
    def check_only_input(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        """should not override this method"""
        return self._check_only_input(input_dict)

    @abc.abstractmethod
    def _check_only_input(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        pass

    @check_output_specs("output_specs", cache=False)
    def check_only_output(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        """should not override this method"""
        return self._check_only_output(input_dict)

    @abc.abstractmethod
    def _check_only_output(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        pass

    @check_input_specs("input_specs", cache=True, only_check_on_retry=False)
    @check_output_specs("output_specs", cache=True)
    def check_input_and_output_with_cache(
        self, input_dict: Dict[str, Any]
    ) -> Dict[str, Any]:
        """should not override this method"""
        return self._check_input_and_output(input_dict)

    @check_input_specs("input_specs", cache=False, only_check_on_retry=False)
    @check_output_specs("output_specs", cache=False)
    def check_input_and_output_wo_filter(self, input_dict) -> Dict[str, Any]:
        """should not override this method"""
        return self._check_input_and_output(input_dict)


class InputNumberOutputFloat(AbstractInterfaceClass):
    """This is an abstract class enforcing a contraint on input/output"""

    @property
    def input_specs(self) -> SpecDict:
        return SpecDict({"input": (float, int)})

    @property
    def output_specs(self) -> SpecDict:
        return SpecDict({"output": float})


class CorrectImplementation(InputNumberOutputFloat):
    def run(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        output = float(input_dict["input"]) * 2
        return {"output": output}

    @override(AbstractInterfaceClass)
    def _check_input_and_output(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        # check if there is any key other than input in the input_dict
        if "input" not in input_dict:
            raise ValueError(ONLY_ONE_KEY_ALLOWED)
        return self.run(input_dict)

    @override(AbstractInterfaceClass)
    def _check_only_input(self, input_dict: Dict[str, Any]) -> Dict[str, Any]:
        # check if there is any key other than input in the input_dict
        if "input" not in input_dict:
            raise ValueError(ONLY_ONE_KEY_ALLOWED)

        out = self.run(input_dict)

        # Output can be anything since there are no `output_specs`.
        return {"output": str(out)}

    @override(AbstractInterfaceClass)
    def _check_only_output(self, input_dict) -> Dict[str, Any]:
        # there is no input spec, so we can pass anything
        if "input" in input_dict:
            raise ValueError(
                "input_dict should not have `input` key in check_only_output"
            )

        return self.run({"input": input_dict["not_input"]})


class IncorrectImplementation(CorrectImplementation):
    @override(CorrectImplementation)
    def run(self, input_dict) -> Dict[str, Any]:
        output = str(input_dict["input"] * 2)
        return {"output": output}


class TestCheckSpecs(unittest.TestCase):
    def test_check_input_and_output(self):

        correct_module = CorrectImplementation()

        output = correct_module.check_input_and_output({"input": 2})
        # Output should also match the `output_specs`.
        correct_module.output_specs.validate(output)

        # This should raise an error saying that the `input` key is missing.
        self.assertRaises(
            SpecCheckingError,
            lambda: correct_module.check_input_and_output({"not_input": 2}),
        )

    def test_check_only_input(self):
        correct_module = CorrectImplementation()
        # this should not raise any error since input matches the input specs
        output = correct_module.check_only_input({"input": 2})
        # Output can be anything since ther is no `output_specs`.
        self.assertRaises(
            ValueError,
            lambda: correct_module.output_specs.validate(output),
        )

    def test_check_only_output(self):
        correct_module = CorrectImplementation()
        # This should not raise any error since input does not have to match
        # `input_specs`.
        output = correct_module.check_only_output({"not_input": 2})
        # Output should match the `output_specs`.
        correct_module.output_specs.validate(output)

    def test_incorrect_implementation(self):
        incorrect_module = IncorrectImplementation()
        # this should raise an error saying that the output does not match the
        # `output_specs`.
        self.assertRaises(
            SpecCheckingError,
            lambda: incorrect_module.check_input_and_output({"input": 2}),
        )

        # this should not raise an error because output is not forced to be checked
        incorrect_module.check_only_input({"input": 2})

        # This should raise an error because output does not match the `output_specs`.
        self.assertRaises(
            SpecCheckingError,
            lambda: incorrect_module.check_only_output({"not_input": 2}),
        )

    def test_filter(self):
        # create an arbitrary large input dict and test the behavior with and without a
        # filter
        input_dict = {"input": 2}
        for i in range(100):
            inds = (str(i),) + tuple(str(j) for j in range(i + 1, i + 11))
            input_dict[inds] = i

        correct_module = CorrectImplementation()

        # should run without errors
        correct_module.check_input_and_output(input_dict)

    def test_cache(self):
        # warning: this could be a flakey test
        # for N times, run the function twice and compare the time of each run.
        # the second run should be faster since the output is cached
        # to make sure the time is not too small, we run this on an input dict that is
        # arbitrarily large and nested.
        # we also check if cache is not working the second run is as slow as the first
        # run.

        input_dict = {"input": 2}
        for i in range(100):
            inds = (str(i),) + tuple(str(j) for j in range(i + 1, i + 11))
            input_dict[inds] = i

        N = 500
        time1, time2 = [], []
        for _ in range(N):

            module = CorrectImplementation()

            fn = getattr(module, "check_input_and_output_with_cache")
            start = time.time()
            fn(input_dict)
            end = time.time()
            time1.append(end - start)

            start = time.time()
            fn(input_dict)
            end = time.time()
            time2.append(end - start)

        lower_bound_time1 = np.mean(time1)  # - 3 * np.std(time1)
        upper_bound_time2 = np.mean(time2)  # + 3 * np.std(time2)
        print(f"time1: {np.mean(time1)}")
        print(f"time2: {np.mean(time2)}")

        self.assertGreater(lower_bound_time1, upper_bound_time2)

    def test_tensor_specs(self):
        # Test if the `input_specs` can be a tensor spec.
        class ClassWithTensorSpec:
            @property
            def input_spec1(self) -> TensorSpec:
                return TensorSpec("b, h", h=4, framework="torch")

            @check_input_specs("input_spec1", cache=False, only_check_on_retry=False)
            def forward(self, input_data) -> Any:
                return input_data

        module = ClassWithTensorSpec()
        module.forward(torch.rand(2, 4))
        self.assertRaises(SpecCheckingError, lambda: module.forward(torch.rand(2, 3)))

    def test_type_specs(self):
        class SpecialOutputType:
            pass

        class WrongOutputType:
            pass

        class ClassWithTypeSpec:
            @property
            def output_specs(self) -> Type:
                return SpecialOutputType

            @check_output_specs("output_specs", cache=False)
            def forward_pass(self, input_data) -> Any:
                return SpecialOutputType()

            @check_output_specs("output_specs", cache=False)
            def forward_fail(self, input_data) -> Any:
                return WrongOutputType()

        module = ClassWithTypeSpec()
        output = module.forward_pass(torch.rand(2, 4))
        self.assertIsInstance(output, SpecialOutputType)
        self.assertRaises(
            SpecCheckingError, lambda: module.forward_fail(torch.rand(2, 3))
        )

    def test_convert_to_canonical_format(self):

        # Case: input is a list of strs
        self.assertDictEqual(
            convert_to_canonical_format(["foo", "bar"]),
            SpecDict({"foo": None, "bar": None}),
        )

        # Case: input is a list of strs and nested strs
        self.assertDictEqual(
            convert_to_canonical_format(["foo", ("bar", "jar")]),
            SpecDict({"foo": None, "bar": {"jar": None}}),
        )

        # Case: input is a Nested Mapping
        returned = convert_to_canonical_format(
            {
                "foo": {"bar": TensorSpec("b", framework="torch")},
                "jar": {"tar": TypeSpec(int), "car": None},
            }
        )
        self.assertIsInstance(returned, SpecDict)
        self.assertDictEqual(
            returned,
            SpecDict(
                {
                    "foo": {"bar": TensorSpec("b", framework="torch")},
                    "jar": {"tar": TypeSpec(int), "car": None},
                }
            ),
        )

        # Case: input is a SpecDict already
        returned = convert_to_canonical_format(
            SpecDict(
                {
                    "foo": {"bar": TensorSpec("b", framework="torch")},
                    "jar": {"tar": TypeSpec(int)},
                }
            )
        )
        self.assertIsInstance(returned, SpecDict)
        self.assertDictEqual(
            returned,
            SpecDict(
                {
                    "foo": {"bar": TensorSpec("b", framework="torch")},
                    "jar": {"tar": TypeSpec(int)},
                }
            ),
        )


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))
