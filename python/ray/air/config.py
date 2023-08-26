import copy
import logging
import os
import warnings
from collections import defaultdict
from dataclasses import _MISSING_TYPE, dataclass, fields
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Union,
    Tuple,
)

import pyarrow.fs

from ray._private.storage import _get_storage_uri
from ray._private.thirdparty.tabulate.tabulate import tabulate
from ray.air.constants import WILDCARD_KEY
from ray.util.annotations import PublicAPI, Deprecated
from ray.widgets import Template, make_table_html_repr
from ray.data.preprocessor import Preprocessor

if TYPE_CHECKING:
    from ray.data import Dataset
    from ray.tune.callback import Callback
    from ray.tune.progress_reporter import ProgressReporter
    from ray.tune.search.sample import Domain
    from ray.tune.stopper import Stopper
    from ray.train import SyncConfig
    from ray.tune.experimental.output import AirVerbosity
    from ray.tune.utils.log import Verbosity
    from ray.tune.execution.placement_groups import PlacementGroupFactory


# Dict[str, List] is to support `tune.grid_search`:
# TODO(sumanthratna/matt): Upstream this to Tune.
SampleRange = Union["Domain", Dict[str, List]]


MAX = "max"
MIN = "min"


logger = logging.getLogger(__name__)


def _repr_dataclass(obj, *, default_values: Optional[Dict[str, Any]] = None) -> str:
    """A utility function to elegantly represent dataclasses.

    In contrast to the default dataclass `__repr__`, which shows all parameters, this
    function only shows parameters with non-default values.

    Args:
        obj: The dataclass to represent.
        default_values: An optional dictionary that maps field names to default values.
            Use this parameter to specify default values that are generated dynamically
            (e.g., in `__post_init__` or by a `default_factory`). If a default value
            isn't specified in `default_values`, then the default value is inferred from
            the `dataclass`.

    Returns:
        A representation of the dataclass.
    """
    if default_values is None:
        default_values = {}

    non_default_values = {}  # Maps field name to value.

    for field in fields(obj):
        value = getattr(obj, field.name)
        default_value = default_values.get(field.name, field.default)
        is_required = isinstance(field.default, _MISSING_TYPE)
        if is_required or value != default_value:
            non_default_values[field.name] = value

    string = f"{obj.__class__.__name__}("
    string += ", ".join(
        f"{name}={value!r}" for name, value in non_default_values.items()
    )
    string += ")"

    return string


@dataclass
@PublicAPI(stability="beta")
class ScalingConfig:
    """Configuration for scaling training.

    Args:
        trainer_resources: Resources to allocate for the trainer. If None is provided,
            will default to 1 CPU.
        num_workers: The number of workers (Ray actors) to launch.
            Each worker will reserve 1 CPU by default. The number of CPUs
            reserved by each worker can be overridden with the
            ``resources_per_worker`` argument.
        use_gpu: If True, training will be done on GPUs (1 per worker).
            Defaults to False. The number of GPUs reserved by each
            worker can be overridden with the ``resources_per_worker``
            argument.
        resources_per_worker: If specified, the resources
            defined in this Dict is reserved for each worker.
            Define the ``"CPU"`` and ``"GPU"`` keys (case-sensitive) to
            override the number of CPU or GPUs used by each worker.
        placement_strategy: The placement strategy to use for the
            placement group of the Ray actors. See :ref:`Placement Group
            Strategies <pgroup-strategy>` for the possible options.
        _max_cpu_fraction_per_node: [Experimental] The max fraction of CPUs per node
            that Train will use for scheduling training actors. The remaining CPUs
            can be used for dataset tasks. It is highly recommended that you set this
            to less than 1.0 (e.g., 0.8) when passing datasets to trainers, to avoid
            hangs / CPU starvation of dataset tasks. Warning: this feature is
            experimental and is not recommended for use with autoscaling (scale-up will
            not trigger properly).
    """

    # If adding new attributes here, please also update
    # ray.train.gbdt_trainer._convert_scaling_config_to_ray_params
    trainer_resources: Optional[Union[Dict, SampleRange]] = None
    num_workers: Optional[Union[int, SampleRange]] = None
    use_gpu: Union[bool, SampleRange] = False
    resources_per_worker: Optional[Union[Dict, SampleRange]] = None
    placement_strategy: Union[str, SampleRange] = "PACK"
    _max_cpu_fraction_per_node: Optional[Union[float, SampleRange]] = None

    def __post_init__(self):
        if self.resources_per_worker:
            if not self.use_gpu and self.num_gpus_per_worker > 0:
                raise ValueError(
                    "`use_gpu` is False but `GPU` was found in "
                    "`resources_per_worker`. Either set `use_gpu` to True or "
                    "remove `GPU` from `resources_per_worker."
                )

            if self.use_gpu and self.num_gpus_per_worker == 0:
                raise ValueError(
                    "`use_gpu` is True but `GPU` is set to 0 in "
                    "`resources_per_worker`. Either set `use_gpu` to False or "
                    "request a positive number of `GPU` in "
                    "`resources_per_worker."
                )

    def __repr__(self):
        return _repr_dataclass(self)

    def _repr_html_(self) -> str:
        return make_table_html_repr(obj=self, title=type(self).__name__)

    def __eq__(self, o: "ScalingConfig") -> bool:
        if not isinstance(o, type(self)):
            return False
        return self.as_placement_group_factory() == o.as_placement_group_factory()

    @property
    def _resources_per_worker_not_none(self):
        if self.resources_per_worker is None:
            if self.use_gpu:
                # Note that we don't request any CPUs, which avoids possible
                # scheduling contention. Generally nodes have many more CPUs than
                # GPUs, so not requesting a CPU does not lead to oversubscription.
                return {"GPU": 1}
            else:
                return {"CPU": 1}
        resources_per_worker = {
            k: v for k, v in self.resources_per_worker.items() if v != 0
        }
        resources_per_worker.setdefault("GPU", int(self.use_gpu))
        return resources_per_worker

    @property
    def _trainer_resources_not_none(self):
        if self.trainer_resources is None:
            if self.num_workers:
                # For Google Colab, don't allocate resources to the base Trainer.
                # Colab only has 2 CPUs, and because of this resource scarcity,
                # we have to be careful on where we allocate resources. Since Colab
                # is not distributed, the concern about many parallel Ray Tune trials
                # leading to all Trainers being scheduled on the head node if we set
                # `trainer_resources` to 0 is no longer applicable.
                try:
                    import google.colab  # noqa: F401

                    trainer_resources = 0
                except ImportError:
                    trainer_resources = 1
            else:
                # If there are no additional workers, then always reserve 1 CPU for
                # the Trainer.
                trainer_resources = 1

            return {"CPU": trainer_resources}
        return {k: v for k, v in self.trainer_resources.items() if v != 0}

    @property
    def total_resources(self):
        """Map of total resources required for the trainer."""
        total_resource_map = defaultdict(float, self._trainer_resources_not_none)
        num_workers = self.num_workers or 0
        for k, value in self._resources_per_worker_not_none.items():
            total_resource_map[k] += value * num_workers
        return dict(total_resource_map)

    @property
    def num_cpus_per_worker(self):
        """The number of CPUs to set per worker."""
        return self._resources_per_worker_not_none.get("CPU", 0)

    @property
    def num_gpus_per_worker(self):
        """The number of GPUs to set per worker."""
        return self._resources_per_worker_not_none.get("GPU", 0)

    @property
    def additional_resources_per_worker(self):
        """Resources per worker, not including CPU or GPU resources."""
        return {
            k: v
            for k, v in self._resources_per_worker_not_none.items()
            if k not in ["CPU", "GPU"]
        }

    def as_placement_group_factory(self) -> "PlacementGroupFactory":
        """Returns a PlacementGroupFactory to specify resources for Tune."""
        from ray.tune.execution.placement_groups import PlacementGroupFactory

        trainer_resources = self._trainer_resources_not_none
        trainer_bundle = [trainer_resources]
        worker_resources = {
            "CPU": self.num_cpus_per_worker,
            "GPU": self.num_gpus_per_worker,
        }
        worker_resources_extra = (
            {} if self.resources_per_worker is None else self.resources_per_worker
        )
        worker_bundles = [
            {**worker_resources, **worker_resources_extra}
            for _ in range(self.num_workers if self.num_workers else 0)
        ]
        bundles = trainer_bundle + worker_bundles
        if self._max_cpu_fraction_per_node is not None:
            kwargs = {
                "_max_cpu_fraction_per_node": self._max_cpu_fraction_per_node,
            }
        else:
            kwargs = {}
        return PlacementGroupFactory(
            bundles, strategy=self.placement_strategy, **kwargs
        )

    @classmethod
    def from_placement_group_factory(
        cls, pgf: "PlacementGroupFactory"
    ) -> "ScalingConfig":
        """Create a ScalingConfig from a Tune's PlacementGroupFactory"""
        if pgf.head_bundle_is_empty:
            trainer_resources = {}
            worker_bundles = pgf.bundles
        else:
            trainer_resources = pgf.bundles[0]
            worker_bundles = pgf.bundles[1:]

        use_gpu = False
        placement_strategy = pgf.strategy
        resources_per_worker = None
        num_workers = None
        max_cpu_fraction_per_node = None

        if worker_bundles:
            first_bundle = worker_bundles[0]
            if not all(bundle == first_bundle for bundle in worker_bundles[1:]):
                raise ValueError(
                    "All worker bundles (any other than the first one) "
                    "must be equal to each other."
                )
            use_gpu = bool(first_bundle.get("GPU"))
            num_workers = len(worker_bundles)
            resources_per_worker = first_bundle

        if "_max_cpu_fraction_per_node" in pgf._kwargs:
            max_cpu_fraction_per_node = pgf._kwargs["_max_cpu_fraction_per_node"]

        return ScalingConfig(
            trainer_resources=trainer_resources,
            num_workers=num_workers,
            use_gpu=use_gpu,
            resources_per_worker=resources_per_worker,
            placement_strategy=placement_strategy,
            _max_cpu_fraction_per_node=max_cpu_fraction_per_node,
        )


@dataclass
@Deprecated(
    message="Use `ray.train.DataConfig` instead of DatasetConfig to "
    "configure data ingest for training. "
    "See https://docs.ray.io/en/master/ray-air/check-ingest.html#migrating-from-the-legacy-datasetconfig-api for more details."  # noqa: E501
)
class DatasetConfig:
    """Configuration for ingest of a single Dataset.

    See :ref:`the AIR Dataset configuration guide <data-ingest-torch>` for
    usage examples.

    This config defines how the Dataset should be read into the DataParallelTrainer.
    It configures the preprocessing, splitting, and ingest strategy per-dataset.

    DataParallelTrainers declare default DatasetConfigs for each dataset passed in the
    ``datasets`` argument. Users have the opportunity to selectively override these
    configs by passing the ``dataset_config`` argument. Trainers can also define user
    customizable values (e.g., XGBoostTrainer doesn't support streaming ingest).

    Args:
        fit: Whether to fit preprocessors on this dataset. This can be set on at most
            one dataset at a time. True by default for the "train" dataset only.
        split: Whether the dataset should be split across multiple workers.
            True by default for the "train" dataset only.
        required: Whether to raise an error if the Dataset isn't provided by the user.
            False by default.
        transform: Whether to transform the dataset with the fitted preprocessor.
            This must be enabled at least for the dataset that is fit.
            True by default.
        max_object_store_memory_fraction [Experimental]: The maximum fraction
            of Ray's shared-memory object store to use for the dataset. The
            default value is -1, meaning that the preprocessed dataset should
            be cached, which may cause spilling if its size is larger than the
            object store's capacity. Pipelined ingest (all other values, 0 or
            higher) is experimental. Note that the absolute memory capacity
            used is based on the object store capacity at invocation time; this
            does not currently cover autoscaling cases where the size of the
            cluster may change.
        global_shuffle: Whether to enable global shuffle (per pipeline window
            in streaming mode). Note that this is an expensive all-to-all operation,
            and most likely you want to use local shuffle instead.
            See https://docs.ray.io/en/master/data/faq.html and
            https://docs.ray.io/en/master/ray-air/check-ingest.html.
            False by default.
        randomize_block_order: Whether to randomize the iteration order over blocks.
            The main purpose of this is to prevent data fetching hotspots in the
            cluster when running many parallel workers / trials on the same data.
            We recommend enabling it always. True by default.
        per_epoch_preprocessor [Experimental]: A preprocessor to re-apply on
            each pass of the dataset. The main use case for this is to apply a
            random transform on a training dataset on each epoch. The
            per-epoch preprocessor will be applied *after* all other
            preprocessors and in parallel with the dataset consumer.
        use_stream_api: Deprecated. Use max_object_store_memory_fraction instead.
        stream_window_size: Deprecated. Use max_object_store_memory_fraction instead.
    """

    # TODO(ekl) could we unify DataParallelTrainer and Trainer so the same data ingest
    # strategy applies to all Trainers?

    fit: Optional[bool] = None
    split: Optional[bool] = None
    required: Optional[bool] = None
    transform: Optional[bool] = None
    max_object_store_memory_fraction: Optional[float] = None
    global_shuffle: Optional[bool] = None
    randomize_block_order: Optional[bool] = None
    per_epoch_preprocessor: Optional["Preprocessor"] = None
    # Deprecated.
    use_stream_api: Optional[int] = None
    stream_window_size: Optional[int] = None

    def __repr__(self):
        return _repr_dataclass(self)

    def _repr_html_(self, title=None) -> str:
        if title is None:
            title = type(self).__name__
        return make_table_html_repr(obj=self, title=title)

    def __post_init__(self):
        if self.use_stream_api is not None or self.stream_window_size is not None:
            raise DeprecationWarning(
                "DatasetConfig.use_stream_api and DatasetConfig.stream_window_size "
                "have been removed as of Ray 2.3. Instead, use "
                "DatasetConfig.max_object_store_memory_fraction with a value "
                "0 or greater "
                "(https://docs.ray.io/en/latest/ray-air/package-ref.html"
                "#ray.air.config.DatasetConfig)."
            )

    def fill_defaults(self) -> "DatasetConfig":
        """Return a copy of this config with all default values filled in."""
        return DatasetConfig(
            fit=self.fit or False,
            split=self.split or False,
            required=self.required or False,
            max_object_store_memory_fraction=self.max_object_store_memory_fraction
            if self.max_object_store_memory_fraction is not None
            else -1,
            global_shuffle=self.global_shuffle or False,
            transform=self.transform if self.transform is not None else True,
            randomize_block_order=self.randomize_block_order
            if self.randomize_block_order is not None
            else True,
            per_epoch_preprocessor=self.per_epoch_preprocessor,
        )

    @staticmethod
    def merge(
        a: Dict[str, "DatasetConfig"], b: Optional[Dict[str, "DatasetConfig"]]
    ) -> Dict[str, "DatasetConfig"]:
        """Merge two given DatasetConfigs, the second taking precedence.

        Raises:
            ValueError: if validation fails on the merged configs.
        """
        has_wildcard = WILDCARD_KEY in a
        result = a.copy()
        if b is None:
            return result
        for key in b:
            if key in a:
                result[key] = a[key]._merge(b[key])
            elif has_wildcard:
                result[key] = a[WILDCARD_KEY]._merge(b[key])
            else:
                raise ValueError(
                    f"Invalid dataset config `{key}`. It must be one of `{list(a)}`."
                )
        return result

    @staticmethod
    def validated(
        config: Dict[str, "DatasetConfig"], datasets: Optional[Dict[str, "Dataset"]]
    ) -> Dict[str, "DatasetConfig"]:
        """Validate the given config and datasets are usable.

        Returns dict of validated configs with defaults filled out.
        """
        datasets = datasets or {}
        has_wildcard = WILDCARD_KEY in config
        fittable = set()
        result = {k: v.fill_defaults() for k, v in config.items()}
        for k, v in result.items():
            if v.fit:
                fittable.add(k)
                if not v.transform:
                    raise ValueError(
                        f"Error configuring dataset `{k}`: cannot specify both "
                        "fit=True and transform=False."
                    )
            if v.required:
                if k not in datasets:
                    raise ValueError(
                        f"The required dataset `{k}` was not found in {datasets}."
                    )
            if not isinstance(v.max_object_store_memory_fraction, (float, int)):
                raise ValueError(
                    f"Error configuring dataset `{k}`: "
                    "max_object_store_memory_fraction "
                    "must be None or a float with value -1 or >=0, but got "
                    f"{v.max_object_store_memory_fraction}."
                )
            if not (
                v.max_object_store_memory_fraction == -1
                or v.max_object_store_memory_fraction >= 0
            ):
                raise ValueError(
                    f"Error configuring dataset `{k}`: "
                    "max_object_store_memory_fraction "
                    "must be None or a float with value -1 or >=0, but got "
                    f"{v.max_object_store_memory_fraction}."
                )
            if v.per_epoch_preprocessor is not None:
                if not isinstance(v.per_epoch_preprocessor, Preprocessor):
                    raise ValueError(
                        "`per_epoch_preprocessor` must be a ray.data.Preprocessor "
                        f"but got {v.per_epoch_preprocessor}."
                    )
                if (
                    v.per_epoch_preprocessor.fit_status()
                    != Preprocessor.FitStatus.NOT_FITTABLE
                ):
                    raise ValueError(
                        "`per_epoch_preprocessor` currently does not support "
                        "fittable ray.data.Preprocessors."
                    )

        if len(fittable) > 1:
            raise ValueError(
                f"More than one dataset was specified to be fit: {fittable}"
            )
        if not has_wildcard:
            for k, v in datasets.items():
                if k not in result:
                    raise ValueError(
                        f"An unexpected dataset `{k}` was given. The list of expected "
                        f"datasets is `{list(result)}`."
                    )
        return result

    def _merge(self, other: "DatasetConfig") -> "DatasetConfig":
        """Merge the given DatasetConfig into this one."""
        new_config = DatasetConfig(
            fit=self.fit if other.fit is None else other.fit,
            split=self.split if other.split is None else other.split,
            required=self.required if other.required is None else other.required,
            transform=self.transform if other.transform is None else other.transform,
            max_object_store_memory_fraction=self.max_object_store_memory_fraction
            if other.max_object_store_memory_fraction is None
            else other.max_object_store_memory_fraction,
            global_shuffle=self.global_shuffle
            if other.global_shuffle is None
            else other.global_shuffle,
            randomize_block_order=self.randomize_block_order
            if other.randomize_block_order is None
            else other.randomize_block_order,
            per_epoch_preprocessor=self.per_epoch_preprocessor
            if other.per_epoch_preprocessor is None
            else other.per_epoch_preprocessor,
        )
        return new_config


@dataclass
@PublicAPI(stability="beta")
class FailureConfig:
    """Configuration related to failure handling of each training/tuning run.

    Args:
        max_failures: Tries to recover a run at least this many times.
            Will recover from the latest checkpoint if present.
            Setting to -1 will lead to infinite recovery retries.
            Setting to 0 will disable retries. Defaults to 0.
        fail_fast: Whether to fail upon the first error. Only used for
            Ray Tune - this does not apply
            to single training runs (e.g. with ``Trainer.fit()``).
            If fail_fast='raise' provided, Ray Tune will automatically
            raise the exception received by the Trainable. fail_fast='raise'
            can easily leak resources and should be used with caution (it
            is best used with `ray.init(local_mode=True)`).
    """

    max_failures: int = 0
    fail_fast: Union[bool, str] = False

    def __post_init__(self):
        # Same check as in tune.run
        if self.fail_fast and self.max_failures != 0:
            raise ValueError("max_failures must be 0 if fail_fast=True.")

        # Same check as in TuneController
        if not (isinstance(self.fail_fast, bool) or self.fail_fast.upper() == "RAISE"):
            raise ValueError(
                "fail_fast must be one of {bool, 'raise'}. " f"Got {self.fail_fast}."
            )

    def __repr__(self):
        return _repr_dataclass(self)

    def _repr_html_(self):
        return Template("scrollableTable.html.j2").render(
            table=tabulate(
                {
                    "Setting": ["Max failures", "Fail fast"],
                    "Value": [self.max_failures, self.fail_fast],
                },
                tablefmt="html",
                showindex=False,
                headers="keys",
            ),
            max_height="none",
        )


@dataclass
@PublicAPI(stability="beta")
class CheckpointConfig:
    """Configurable parameters for defining the checkpointing strategy.

    Default behavior is to persist all checkpoints to disk. If
    ``num_to_keep`` is set, the default retention policy is to keep the
    checkpoints with maximum timestamp, i.e. the most recent checkpoints.

    Args:
        num_to_keep: The number of checkpoints to keep
            on disk for this run. If a checkpoint is persisted to disk after
            there are already this many checkpoints, then an existing
            checkpoint will be deleted. If this is ``None`` then checkpoints
            will not be deleted. Must be >= 1.
        checkpoint_score_attribute: The attribute that will be used to
            score checkpoints to determine which checkpoints should be kept
            on disk when there are greater than ``num_to_keep`` checkpoints.
            This attribute must be a key from the checkpoint
            dictionary which has a numerical value. Per default, the last
            checkpoints will be kept.
        checkpoint_score_order: Either "max" or "min".
            If "max", then checkpoints with highest values of
            ``checkpoint_score_attribute`` will be kept.
            If "min", then checkpoints with lowest values of
            ``checkpoint_score_attribute`` will be kept.
        checkpoint_frequency: Number of iterations between checkpoints. If 0
            this will disable checkpointing.
            Please note that most trainers will still save one checkpoint at
            the end of training.
            This attribute is only supported
            by trainers that don't take in custom training loops.
        checkpoint_at_end: If True, will save a checkpoint at the end of training.
            This attribute is only supported by trainers that don't take in
            custom training loops. Defaults to True for trainers that support it
            and False for generic function trainables.
        _checkpoint_keep_all_ranks: If True, will save checkpoints from all ranked
            training workers. If False, only checkpoint from rank 0 worker is kept.
            NOTE: This API is experimental and subject to change between minor
            releases.
        _checkpoint_upload_from_workers: If True, distributed workers
            will upload their checkpoints to cloud directly. This is to avoid the
            need for transferring large checkpoint files to the training worker
            group coordinator for persistence. NOTE: This API is experimental and
            subject to change between minor releases.
    """

    num_to_keep: Optional[int] = None
    checkpoint_score_attribute: Optional[str] = None
    checkpoint_score_order: Optional[str] = MAX
    checkpoint_frequency: Optional[int] = 0
    checkpoint_at_end: Optional[bool] = None
    _checkpoint_keep_all_ranks: Optional[bool] = False
    _checkpoint_upload_from_workers: Optional[bool] = False

    def __post_init__(self):
        if self.num_to_keep is not None and self.num_to_keep <= 0:
            raise ValueError(
                f"Received invalid num_to_keep: "
                f"{self.num_to_keep}. "
                f"Must be None or an integer >= 1."
            )
        if self.checkpoint_score_order not in (MAX, MIN):
            raise ValueError(
                f"checkpoint_score_order must be either " f'"{MAX}" or "{MIN}".'
            )

        if self.checkpoint_frequency < 0:
            raise ValueError(
                f"checkpoint_frequency must be >=0, got {self.checkpoint_frequency}"
            )

    def __repr__(self):
        return _repr_dataclass(self)

    def _repr_html_(self) -> str:
        if self.num_to_keep is None:
            num_to_keep_repr = "All"
        else:
            num_to_keep_repr = self.num_to_keep

        if self.checkpoint_score_attribute is None:
            checkpoint_score_attribute_repr = "Most recent"
        else:
            checkpoint_score_attribute_repr = self.checkpoint_score_attribute

        if self.checkpoint_at_end is None:
            checkpoint_at_end_repr = ""
        else:
            checkpoint_at_end_repr = self.checkpoint_at_end

        return Template("scrollableTable.html.j2").render(
            table=tabulate(
                {
                    "Setting": [
                        "Number of checkpoints to keep",
                        "Checkpoint score attribute",
                        "Checkpoint score order",
                        "Checkpoint frequency",
                        "Checkpoint at end",
                    ],
                    "Value": [
                        num_to_keep_repr,
                        checkpoint_score_attribute_repr,
                        self.checkpoint_score_order,
                        self.checkpoint_frequency,
                        checkpoint_at_end_repr,
                    ],
                },
                tablefmt="html",
                showindex=False,
                headers="keys",
            ),
            max_height="none",
        )

    @property
    def _tune_legacy_checkpoint_score_attr(self) -> Optional[str]:
        """Same as ``checkpoint_score_attr`` in ``tune.run``.

        Only used for Legacy API compatibility.
        """
        if self.checkpoint_score_attribute is None:
            return self.checkpoint_score_attribute
        prefix = ""
        if self.checkpoint_score_order == MIN:
            prefix = "min-"
        return f"{prefix}{self.checkpoint_score_attribute}"


@dataclass
@PublicAPI(stability="beta")
class RunConfig:
    """Runtime configuration for training and tuning runs.

    Upon resuming from a training or tuning run checkpoint,
    Ray Train/Tune will automatically apply the RunConfig from
    the previously checkpointed run.

    Args:
        name: Name of the trial or experiment. If not provided, will be deduced
            from the Trainable.
        storage_path: Path to store results at. Can be a local directory or
            a destination on cloud storage. If Ray storage is set up,
            defaults to the storage location. Otherwise, this defaults to
            the local ``~/ray_results`` directory.
        stop: Stop conditions to consider. Refer to ray.tune.stopper.Stopper
            for more info. Stoppers should be serializable.
        callbacks: Callbacks to invoke.
            Refer to ray.tune.callback.Callback for more info.
            Callbacks should be serializable.
            Currently only stateless callbacks are supported for resumed runs.
            (any state of the callback will not be checkpointed by Tune
            and thus will not take effect in resumed runs).
        failure_config: Failure mode configuration.
        sync_config: Configuration object for syncing. See train.SyncConfig.
        checkpoint_config: Checkpointing configuration.
        progress_reporter: Progress reporter for reporting
            intermediate experiment progress. Defaults to CLIReporter if
            running in command-line, or JupyterNotebookReporter if running in
            a Jupyter notebook.
        verbose: 0, 1, or 2. Verbosity mode.
            0 = silent, 1 = default, 2 = verbose. Defaults to 1.
            If the ``RAY_AIR_NEW_OUTPUT=1`` environment variable is set,
            uses the old verbosity settings:
            0 = silent, 1 = only status updates, 2 = status and brief
            results, 3 = status and detailed results.
        log_to_file: Log stdout and stderr to files in
            trial directories. If this is `False` (default), no files
            are written. If `true`, outputs are written to `trialdir/stdout`
            and `trialdir/stderr`, respectively. If this is a single string,
            this is interpreted as a file relative to the trialdir, to which
            both streams are written. If this is a Sequence (e.g. a Tuple),
            it has to have length 2 and the elements indicate the files to
            which stdout and stderr are written, respectively.

    """

    name: Optional[str] = None
    storage_path: Optional[str] = None
    storage_filesystem: Optional[pyarrow.fs.FileSystem] = None
    callbacks: Optional[List["Callback"]] = None
    stop: Optional[Union[Mapping, "Stopper", Callable[[str, Mapping], bool]]] = None
    failure_config: Optional[FailureConfig] = None
    sync_config: Optional["SyncConfig"] = None
    checkpoint_config: Optional[CheckpointConfig] = None
    progress_reporter: Optional["ProgressReporter"] = None
    verbose: Optional[Union[int, "AirVerbosity", "Verbosity"]] = None
    log_to_file: Union[bool, str, Tuple[str, str]] = False

    # Deprecated
    local_dir: Optional[str] = None

    def __post_init__(self):
        from ray.train import SyncConfig
        from ray.tune.experimental.output import AirVerbosity, get_air_verbosity

        if not self.failure_config:
            self.failure_config = FailureConfig()

        if not self.sync_config:
            self.sync_config = SyncConfig()

        if not self.checkpoint_config:
            self.checkpoint_config = CheckpointConfig()

        if self.verbose is None:
            # Default `verbose` value. For new output engine,
            # this is AirVerbosity.DEFAULT.
            # For old output engine, this is Verbosity.V3_TRIAL_DETAILS
            # Todo (krfricke): Currently uses number to pass test_configs::test_repr
            self.verbose = get_air_verbosity(AirVerbosity.DEFAULT) or 3

        # Convert Paths to strings
        if isinstance(self.local_dir, Path):
            self.local_dir = str(self.local_dir)

        if isinstance(self.storage_path, Path):
            self.storage_path = str(self.storage_path)

        # TODO(justinvyu): [code_removal] Legacy stuff below.
        from ray.tune.utils.util import _resolve_storage_path
        from ray.train._internal.storage import _use_storage_context
        from ray.train._internal.syncer import Syncer

        if _use_storage_context():
            return

        local_path, remote_path = _resolve_storage_path(
            self.storage_path, self.local_dir, self.sync_config.upload_dir
        )

        if self.sync_config.upload_dir:
            assert remote_path == self.sync_config.upload_dir
            warnings.warn(
                "Setting a `SyncConfig.upload_dir` is deprecated and will be removed "
                "in the future. Pass `RunConfig.storage_path` instead."
            )
            # Set upload_dir to None to avoid further downstream resolution.
            # Copy object first to not alter user input.
            self.sync_config = copy.copy(self.sync_config)
            self.sync_config.upload_dir = None

        if self.local_dir:
            assert local_path == self.local_dir
            warnings.warn(
                "Setting a `RunConfig.local_dir` is deprecated and will be removed "
                "in the future. If you are not using remote storage,"
                "set the `RunConfig.storage_path` instead. Otherwise, set the"
                "`RAY_AIR_LOCAL_CACHE_DIR` environment variable to control "
                "the local cache location."
            )
            self.local_dir = None

        if not remote_path:
            remote_path = _get_storage_uri()
            if remote_path:
                logger.info(
                    "Using configured Ray storage URI as storage path: "
                    f"{remote_path}"
                )

        if remote_path:
            self.storage_path = remote_path
            if local_path:
                # If storage_path is a remote path set by SyncConfig.upload_dir,
                # this may not have been set in the previous if clause.
                os.environ["RAY_AIR_LOCAL_CACHE_DIR"] = local_path
        elif local_path:
            self.storage_path = local_path

        if isinstance(self.sync_config.syncer, Syncer) and not remote_path:
            raise ValueError(
                "Must specify a remote `storage_path` to use a custom `syncer`."
            )

    def __repr__(self):
        from ray.train import SyncConfig

        return _repr_dataclass(
            self,
            default_values={
                "failure_config": FailureConfig(),
                "sync_config": SyncConfig(),
                "checkpoint_config": CheckpointConfig(),
            },
        )

    def _repr_html_(self) -> str:
        reprs = []
        if self.failure_config is not None:
            reprs.append(
                Template("title_data_mini.html.j2").render(
                    title="Failure Config", data=self.failure_config._repr_html_()
                )
            )
        if self.sync_config is not None:
            reprs.append(
                Template("title_data_mini.html.j2").render(
                    title="Sync Config", data=self.sync_config._repr_html_()
                )
            )
        if self.checkpoint_config is not None:
            reprs.append(
                Template("title_data_mini.html.j2").render(
                    title="Checkpoint Config", data=self.checkpoint_config._repr_html_()
                )
            )

        # Create a divider between each displayed repr
        subconfigs = [Template("divider.html.j2").render()] * (2 * len(reprs) - 1)
        subconfigs[::2] = reprs

        settings = Template("scrollableTable.html.j2").render(
            table=tabulate(
                {
                    "Name": self.name,
                    "Local results directory": self.local_dir,
                    "Verbosity": self.verbose,
                    "Log to file": self.log_to_file,
                }.items(),
                tablefmt="html",
                headers=["Setting", "Value"],
                showindex=False,
            ),
            max_height="300px",
        )

        return Template("title_data.html.j2").render(
            title="RunConfig",
            data=Template("run_config.html.j2").render(
                subconfigs=subconfigs,
                settings=settings,
            ),
        )
