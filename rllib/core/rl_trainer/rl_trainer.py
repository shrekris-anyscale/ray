import abc

from dataclasses import dataclass, field
import logging
import numpy as np
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Hashable,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)

from ray.rllib.utils.framework import try_import_tf, try_import_torch
from ray.rllib.core.rl_module.rl_module import (
    RLModule,
    ModuleID,
    SingleAgentRLModuleSpec,
)
from ray.rllib.core.rl_module.marl_module import (
    MultiAgentRLModule,
    MultiAgentRLModuleSpec,
)
from ray.rllib.policy.sample_batch import SampleBatch, MultiAgentBatch
from ray.rllib.utils.nested_dict import NestedDict
from ray.rllib.utils.numpy import convert_to_numpy
from ray.rllib.utils.typing import TensorType, ResultDict
from ray.rllib.utils.minibatch_utils import (
    MiniBatchDummyIterator,
    MiniBatchCyclicIterator,
)
from ray.rllib.core.rl_trainer.scaling_config import TrainerScalingConfig
from ray.rllib.core.rl_trainer.reduce_result_dict_fn import _reduce_mean_results
from ray.rllib.utils.annotations import (
    OverrideToImplementCustomLogic,
    OverrideToImplementCustomLogic_CallToSuperRecommended,
)

torch, _ = try_import_torch()
tf1, tf, tfv = try_import_tf()

logger = logging.getLogger(__name__)

Optimizer = Union["torch.optim.Optimizer", "tf.keras.optimizers.Optimizer"]
ParamType = Union["torch.Tensor", "tf.Variable"]
ParamOptimizerPairs = List[Tuple[Sequence[ParamType], Optimizer]]
ParamRef = Hashable
ParamDictType = Dict[ParamRef, ParamType]


@dataclass
class FrameworkHPs:
    """The framework specific hyper-parameters.

    Args:
        eager_tracing: Whether to trace the model in eager mode. This enables tf
            tracing mode by wrapping the loss function computation in a tf.function.
            This is useful for speeding up the training loop. However, it is not
            compatible with all tf operations. For example, tf.print is not supported
            in tf.function.
    """

    eager_tracing: bool = False


@dataclass
class RLTrainerHPs:
    """The hyper-parameters for RLTrainer.

    When creating a new RLTrainer, the new hyper-parameters have to be defined by
    subclassing this class and adding the new hyper-parameters as fields.

    # TODO (Kourosh): The things that could be part of the base class:
    - lr_schedule
    - grad_clip
    """

    pass


class RLTrainer:
    """Base class for learners.

    This class will be used to train RLModules. It is responsible for defining the loss
    function, and updating the neural network weights that it owns. It also provides a
    way to add/remove modules to/from RLModules in a multi-agent scenario, in the
    middle of training (This is useful for league based training).

    TF and Torch specific implementation of this class fills in the framework-specific
    implementation details for distributed training, and for computing and applying
    gradients. User should not need to sub-class this class, but instead inherit from
    the TF or Torch specific sub-classes to implement their algorithm-specific update
    logic.


    Args:
        module_spec: The module specification for the RLModule that is being trained.
            If the module is a single agent module, after building the module it will
            be converted to a multi-agent module with a default key. Can be none if the
            module is provided directly via the `module` argument. Refer to
            ray.rllib.core.rl_module.SingleAgentRLModuleSpec
            or ray.rllib.core.rl_module.MultiAgentRLModuleSpec for more info.
        module: If learner is being used stand-alone, the RLModule can be optionally
            passed in directly instead of the through the `module_spec`.
        optimizer_config: The deep learning gradient optimizer configuration to be
            used. For example lr=0.0001, momentum=0.9, etc.
        scaling_config: Configuration for scaling the learner actors.
            Refer to ray.rllib.core.rl_trainer.scaling_config.TrainerScalingConfig
            for more info.
        trainer_hyperparameters: The hyper-parameters for the Learner.
            Algorithm specific learner hyper-parameters will passed in via this
            argument. For example in PPO the `vf_loss_coeff` hyper-parameter will be
            passed in via this argument. Refer to
            ray.rllib.core.rl_trainer.rl_trainer.RLTrainerHPs for more info.
        framework_hps: The framework specific hyper-parameters. This will be used to
            pass in any framework specific hyper-parameter that will impact the module
            creation. For example eager_tracing in TF or compile in Torch.
            Refer to ray.rllib.core.rl_trainer.rl_trainer.FrameworkHPs for more info.


    Usage pattern:

        .. code-block:: python

        # create a single agent RL module spec.
        module_spec = SingleAgentRLModuleSpec(
            module_class=MyModule,
            observation_space=env.observation_space,
            action_space=env.action_space,
            model_config = {"hidden": [128, 128]}
        )

        # create a learner instance that will train the module
        learner = MyLearner(module_spec=module_spec)

        # Note: the learner should be built before it can be used.
        learner.build()

        # take one gradient update on the module and report the results
        results = learner.update(batch)

        # add a new module, perhaps for league based training
        learner.add_module(
            module_id="new_player",
            module_spec=SingleAgentRLModuleSpec(
                module_class=NewPlayerModule,
                observation_space=env.observation_space,
                action_space=env.action_space,
                model_config = {"hidden": [128, 128]}
            )
        )

        # Take another gradient update with both previous and new modules.
        results = learner.update(batch)

        # remove a module
        learner.remove_module("new_player")

        # will train previous modules only.
        results = learner.update(batch)

        # get the state of the trainer
        state = learner.get_state()

        # set the state of the trainer
        learner.set_state(state)

        # get the weights of the underly multi-agent RLModule
        weights = learner.get_weights()

        # set the weights of the underly multi-agent RLModule
        learner.set_weights(weights)


    Extension pattern:

        .. code-block:: python

        class MyLearner(TorchLearner):

            def compute_loss(self, fwd_out, batch):
                # compute the loss based on batch and output of the forward pass
                # to access the learner hyper-parameters use `self.hps`

                return {self.TOTAL_LOSS_KEY: loss}
    """

    framework: str = None
    TOTAL_LOSS_KEY: str = "total_loss"

    def __init__(
        self,
        *,
        module_spec: Optional[
            Union[SingleAgentRLModuleSpec, MultiAgentRLModuleSpec]
        ] = None,
        module: Optional[RLModule] = None,
        optimizer_config: Mapping[str, Any] = None,
        trainer_scaling_config: TrainerScalingConfig = TrainerScalingConfig(),
        trainer_hyperparameters: Optional[RLTrainerHPs] = RLTrainerHPs(),
        framework_hyperparameters: Optional[FrameworkHPs] = FrameworkHPs(),
    ):
        # TODO (Kourosh): convert optimizer configs to dataclasses
        if module_spec is not None and module is not None:
            raise ValueError(
                "Only one of module spec or module can be provided to RLTrainer."
            )

        if module_spec is None and module is None:
            raise ValueError(
                "Either module_spec or module should be provided to RLTrainer."
            )

        self._module_spec = module_spec
        self._module_obj = module
        self._optimizer_config = optimizer_config
        self._hps = trainer_hyperparameters

        # pick the configs that we need for the trainer from scaling config
        self._distributed = trainer_scaling_config.num_workers > 1
        self._use_gpu = trainer_scaling_config.num_gpus_per_worker > 0
        # if we are using gpu but we are not distributed, use this gpu for training
        self._local_gpu_idx = trainer_scaling_config.local_gpu_idx

        # These are the attributes that are set during build
        self._module: MultiAgentRLModule = None
        # These are set for properly applying optimizers and adding or removing modules.
        self._optim_to_param: Dict[Optimizer, List[ParamRef]] = {}
        self._param_to_optim: Dict[ParamRef, Optimizer] = {}
        self._params: ParamDictType = {}

    @property
    def distributed(self) -> bool:
        """Whether the learner is running in distributed mode."""
        return self._distributed

    @property
    def module(self) -> MultiAgentRLModule:
        """The multi-agent RLModule that is being trained."""
        return self._module

    @property
    def hps(self) -> RLTrainerHPs:
        """The hyper-parameters for the trainer."""
        return self._hps

    @abc.abstractmethod
    def configure_optimizers(self) -> ParamOptimizerPairs:
        """Configures the optimizers for the Learner.

        This method is responsible for setting up the optimizers that will be used to
        train the model. The optimizers are responsible for updating the model's
        parameters during training, based on the computed gradients. The method should
        return a list of tuples, where each tuple consists of a list of model
        parameters and a deep learning optimizer that should be used to optimize those
        parameters. To support both tf and torch, we must explicitly return the
        parameters as the first element of the tuple regardless of whether those
        exist in the optimizer objects or not. This method is called once at
        initialization.

        Returns:
            A list of tuples (parameters, optimizer), where parameters is a list of
            model parameters and optimizer is a deep learning optimizer.
        """

    @abc.abstractmethod
    def compute_gradients(self, loss: Mapping[str, Any]) -> ParamDictType:
        """Computes the gradients based on the loss.

        Args:
            loss: The computed loss dict. It should include the key
                `self.TOTAL_LOSS_KEY` that contains the total loss.
        Returns:
            The gradients in teh same format as self._params.
        """

    @abc.abstractmethod
    def apply_gradients(self, gradients: ParamDictType) -> None:
        """Applies the gradients to the MultiAgentRLModule parameters.

        Args:
            gradients: A dictionary of gradients, in the same format as self._params.
        """

    @abc.abstractmethod
    def get_weights(self, module_ids: Optional[Set[str]] = None) -> Mapping[str, Any]:
        """Returns the weights of the underlying MultiAgentRLModule.

        The output should be numpy-friendly for easy serialization, not framework
        specific tensors.

        Args:
            module_ids: The ids of the modules to get the weights for. If None, all
                modules will be returned.

        Returns:
            A dictionary that holds the weights of the modules in a numpy-friendly
            format.
        """

    @abc.abstractmethod
    def set_weights(self, weights: Mapping[str, Any]) -> None:
        """Sets the weights of the underlying MultiAgentRLModule"""

    @abc.abstractmethod
    def get_param_ref(self, param: ParamType) -> Hashable:
        """Returns a hashable reference to a trainable parameter.

        This should be overriden in framework specific specialization. For example in
        torch it will return the parameter itself, while in tf it returns the .ref() of
        the variable. The purpose is to retrieve a unique reference to the parameters.

        Args:
            param: The parameter to get the reference to.

        Returns:
            A reference to the parameter.
        """

    @abc.abstractmethod
    def get_parameters(self, module: RLModule) -> Sequence[ParamType]:
        """Returns the list of parameters of a module.

        This should be overriden in framework specific trainer. For example in torch it
        will return .parameters(), while in tf it returns .trainable_variables.

        Args:
            module: The module to get the parameters from.

        Returns:
            The parameters of the module.
        """

    @abc.abstractmethod
    def get_optimizer_obj(
        self, module: RLModule, optimizer_cls: Type[Optimizer]
    ) -> Optimizer:
        """Returns the optimizer instance of type optimizer_cls given the module.

        In torch this is the optimizer object initialize with module parameters. In tf
        this is initialized without module parameters.

        Args:
            module: The module of type RLModule to get the optimizer from.
            optimizer_cls: The optimizer class to use.

        Returns:
            The optimizer object.
        """

    @abc.abstractmethod
    def _convert_batch_type(self, batch: MultiAgentBatch) -> NestedDict[TensorType]:
        """Converts a MultiAgentBatch to a NestedDict of Tensors.

        This should convert the input batch from a MultiAgentBatch format to framework
        specific tensor format located on the correct device.

        Args:
            batch: A MultiAgentBatch.

        Returns:
            A NestedDict.
        """

    @OverrideToImplementCustomLogic_CallToSuperRecommended
    def compile_results(
        self,
        batch: NestedDict,
        fwd_out: Mapping[str, Any],
        postprocessed_loss: Mapping[str, Any],
        postprocessed_gradients: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Compile results from the update in a numpy-friendly format.

        Args:
            batch: The batch that was used for the update.
            fwd_out: The output of the forward train pass.
            postprocessed_loss: The loss after postprocessing.
            postprocessed_gradients: The gradients after postprocessing.

        Returns:
            A dictionary of results.
        """
        loss_numpy = convert_to_numpy(postprocessed_loss)
        mean_grads = [
            np.mean(grad) for grad in convert_to_numpy(postprocessed_gradients.values())
        ]
        ret = {
            "loss": loss_numpy,
            "mean_gradient": np.mean(mean_grads),
        }

        return ret

    @OverrideToImplementCustomLogic_CallToSuperRecommended
    def add_module(
        self,
        *,
        module_id: ModuleID,
        module_spec: SingleAgentRLModuleSpec,
        set_optimizer_fn: Optional[Callable[[RLModule], ParamOptimizerPairs]] = None,
        optimizer_cls: Optional[Type[Optimizer]] = None,
    ) -> None:
        """Add a module to the underlying MultiAgentRLModule and the Learner.

        Args:
            module_id: The id of the module to add.
            module_spec: The module spec of the module to add.
            set_optimizer_fn: A function that takes in the module and returns a list of
                (param, optimizer) pairs. Each element in the tuple describes a
                parameter group that share the same optimizer object, if None, the
                default optimizer_cls will be used with all the parameters from the
                module.
            optimizer_cls: The optimizer class to use. If None, the set_optimizer_fn
                should be provided.
        """
        self.__check_if_build_called()
        module = module_spec.build()

        # construct a default set_optimizer_fn if not provided
        if set_optimizer_fn is None:
            if optimizer_cls is None:
                raise ValueError(
                    "Either set_optimizer_fn or optimizer_cls must be provided."
                )

            def set_optimizer_fn(module):
                optimizer = self.get_optimizer_obj(module, optimizer_cls)
                parameters = self.get_parameters(module)
                return [(parameters, optimizer)]

        for param_seq, optimizer in set_optimizer_fn(module):
            self._optim_to_param[optimizer] = []
            for param in param_seq:
                param_ref = self.get_param_ref(param)
                self._optim_to_param[optimizer].append(param_ref)
                self._params[param_ref] = param
                self._param_to_optim[param_ref] = optimizer

        self._module.add_module(module_id, module)

    @OverrideToImplementCustomLogic_CallToSuperRecommended
    def remove_module(self, module_id: ModuleID) -> None:
        """Remove a module from the Learner.

        Args:
            module_id: The id of the module to remove.
        """
        self.__check_if_build_called()
        module = self._module[module_id]

        parameters = self.get_parameters(module)
        for param in parameters:
            param_ref = self.get_param_ref(param)
            if param_ref in self._params:
                del self._params[param_ref]
            if param_ref in self._param_to_optim:
                optimizer = self._param_to_optim[param_ref]
                if optimizer in self._optim_to_param:
                    del self._optim_to_param[optimizer]
                del self._param_to_optim[param_ref]

        self._module.remove_module(module_id)

    @OverrideToImplementCustomLogic_CallToSuperRecommended
    def build(self) -> None:
        """Builds the Learner.

        This method should be called before the learner is used. It is responsible for
        setting up the module and optimizers.
        """
        self._module = self._make_module()
        for param_seq, optimizer in self.configure_optimizers():
            self._optim_to_param[optimizer] = []
            for param in param_seq:
                param_ref = self.get_param_ref(param)
                self._optim_to_param[optimizer].append(param_ref)
                self._params[param_ref] = param
                self._param_to_optim[param_ref] = optimizer

    @OverrideToImplementCustomLogic
    def compute_loss(
        self,
        *,
        fwd_out: Union[MultiAgentBatch, NestedDict],
        batch: Union[MultiAgentBatch, NestedDict],
    ) -> Union[TensorType, Mapping[str, Any]]:
        """Computes the loss for the module being optimized.

        This method must be overridden multiagent-specific algorithm learners to
        specify the specific loss computation logic. If the algorithm is single agent
        `compute_loss_per_module()` should be overriden instead.
        The input "fwd_out" is the output "forward_train" method of the underlying
        MultiAgentRLModule. The input "batch" is the data that was used to compute
        "fwd_out". The returned dictionary must contain a key called "total_loss",
        which will be used to compute gradients. It is recommended to not compute any
        forward passes within this method, and to use the "forward_train" outputs to
        compute the required tensors for loss calculation.

        Args:
            fwd_out: Output from a call to `forward_train` on self._module during
                training.
            batch: The data that was used to compute fwd_out.

        Returns:
            A dictionary of losses. NOTE that the dictionary
            must contain one protected key "total_loss" which will be used for
            computing gradients through.
        """
        loss_total = None
        results_all_modules = {}
        for module_id in fwd_out:
            module_batch = batch[module_id]
            module_fwd_out = fwd_out[module_id]

            module_results = self.compute_loss_per_module(
                module_id, module_batch, module_fwd_out
            )
            results_all_modules[module_id] = module_results
            loss = module_results[self.TOTAL_LOSS_KEY]

            if loss_total is None:
                loss_total = loss
            else:
                loss_total += loss

        results_all_modules[self.TOTAL_LOSS_KEY] = loss_total

        return results_all_modules

    @OverrideToImplementCustomLogic
    def compute_loss_per_module(
        self, module_id: str, batch: SampleBatch, fwd_out: Mapping[str, TensorType]
    ) -> Mapping[str, Any]:
        """Computes the loss for a single module.

        Think of this as computing loss for a single agent. For multi-agent use-cases
        that require more complicated computation for loss, consider overriding the
        `compute_loss` method instead.

        Args:
            module_id: The id of the module.
            batch: The sample batch for this particular module.
            fwd_out: The output of the forward pass for this particular module.

        Returns:
            A dictionary of losses. NOTE that the dictionary
            must contain one protected key "total_loss" which will be used for
            computing gradients through.
        """
        raise NotImplementedError

    @OverrideToImplementCustomLogic
    def additional_update(self, *args, **kwargs) -> Mapping[str, Any]:
        """Apply additional non-gradient based updates to this Trainer.

        For example, this could be used to do a polyak averaging update
        of a target network in off policy algorithms like SAC or DQN.

        Example:

        .. code-block:: python

            class DQNLearner(TorchLearner):

                def additional_update_per_module(self, module_id: str, tau: float):
                    # perform polyak averaging update
                    main = self._module[module_id].main
                    target = self._module[module_id].target
                    for param, target_param in zip(
                        main.parameters(), target.parameters()
                    ):
                        target_param.data.copy_(
                            tau * param.data + (1.0 - tau) * target_param.data
                        )

        And inside a training loop:

        .. code-block:: python

            for _ in range(100):
                sample = ...
                self.learner.update(sample)
                if self.learner.global_step % 10 == 0:
                    self.learner.additional_update(tau=0.01)

        Args:
            *args: Arguments to use for the update.
            **kwargs: Keyword arguments to use for the additional update.

        Returns:
            A dictionary of results from the update
        """
        results_all_modules = {}
        for module_id in self._module.keys():
            module_results = self.additional_update_per_module(
                module_id, *args, **kwargs
            )
            results_all_modules[module_id] = module_results

        return results_all_modules

    @OverrideToImplementCustomLogic
    def additional_update_per_module(
        self, module_id: str, *args, **kwargs
    ) -> Mapping[str, Any]:
        """Apply additional non-gradient based updates for a single module.

        See `additional_update` for more details.

        Args:
            module_id: The id of the module to update.
            *args: Arguments to use for the update.
            **kwargs: Keyword arguments to use for the additional update.

        Returns:
            A dictionary of results from the update
        """
        raise NotImplementedError

    @OverrideToImplementCustomLogic
    def postprocess_gradients(
        self, gradients_dict: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Applies potential postprocessings to the gradients.

        In some algorithms, we may want to perform some postprocessing on the
        gradients before they are applied. This method is called after gradients
        have been computed, and modifies them before they are applied.

        Args:
            gradients_dict: A dictionary of gradients.

        Returns:
            A dictionary of updated gradients.
        """
        return gradients_dict

    def update(
        self,
        batch: MultiAgentBatch,
        *,
        minibatch_size: Optional[int] = None,
        num_iters: int = 1,
        reduce_fn: Callable[[ResultDict], ResultDict] = _reduce_mean_results,
    ) -> Mapping[str, Any]:
        """Do `num_iters` minibatch updates given the original batch.

        Given a batch of episodes you can use this method to take more
        than one backward pass on the batch. The same minibatch_size and num_iters
        will be used for all module ids in MultiAgentRLModule.

        Args:
            batch: A batch of data.
            minibatch_size: The size of the minibatch to use for each update.
            num_iters: The number of complete passes over all the sub-batches
                in the input multi-agent batch.
            reduce_fn: reduce_fn: A function to reduce the results from a list of
                minibatch updates. This can be any arbitrary function that takes a
                list of dictionaries and returns a single dictionary. For example you
                can either take an average (default) or concatenate the results (for
                example for metrics) or be more selective about you want to report back
                to the algorithm's training_step. If None is passed, the results will
                not get reduced.
        Returns:
            A dictionary of results, in numpy format.
        """
        self.__check_if_build_called()

        batch_iter = (
            MiniBatchCyclicIterator
            if minibatch_size is not None
            else MiniBatchDummyIterator
        )

        results = []
        for minibatch in batch_iter(batch, minibatch_size, num_iters):

            result = self._update(minibatch)
            results.append(result)

        # Reduce results across all minibatches, if necessary.
        if len(results) == 1:
            return results[0]
        else:
            if reduce_fn is None:
                return results
            return reduce_fn(results)

    def set_state(self, state: Mapping[str, Any]) -> None:
        """Set the state of the trainer.

        Args:
            state: The state of the optimizer and module. Can be obtained
                from `get_state`.

        """
        # TODO (Kourosh): We have both get(set)_state and get(set)_weights. I think
        # having both can become confusing. Can we simplify this API requirement?
        self.__check_if_build_called()
        # TODO: once we figure out the optimizer format, we can set/get the state
        self._module.set_state(state.get("module_state", {}))

    def get_state(self) -> Mapping[str, Any]:
        """Get the state of the trainer.

        Returns:
            The state of the optimizer and module.

        """
        self.__check_if_build_called()
        # TODO: once we figure out the optimizer format, we can set/get the state
        return {"module_state": self._module.get_state()}

    def _make_module(self) -> MultiAgentRLModule:
        """Construct the multi-agent RL module for the trainer.

        This method uses `self._module_specs` or `self._module_obj` to construct the
        module. If the module_class is a single agent RL module it will be wrapped to a
        multi-agent RL module. Override this method if there are other things than
        needs to happen for instantiation of the module.


        Returns:
            The constructed module.
        """
        if self._module_obj is not None:
            module = self._module_obj
        else:
            module = self._module_spec.build()
        module = module.as_multi_agent()
        return module

    @OverrideToImplementCustomLogic_CallToSuperRecommended
    def _update(
        self,
        batch: Union[MultiAgentBatch, NestedDict],
    ) -> Mapping[str, Any]:
        """Performs a single update given a batch of data."""

        # TODO (Kourosh): remove the MultiAgentBatch from the type, it should be
        # NestedDict from the base class.
        batch = self._convert_batch_type(batch)
        fwd_out = self._module.forward_train(batch)
        loss = self.compute_loss(fwd_out=fwd_out, batch=batch)
        gradients = self.compute_gradients(loss)
        postprocessed_gradients = self.postprocess_gradients(gradients)
        self.apply_gradients(postprocessed_gradients)
        result = self.compile_results(batch, fwd_out, loss, postprocessed_gradients)
        return convert_to_numpy(result)

    def __check_if_build_called(self):
        if self._module is None:
            raise ValueError(
                "RLTrainer.build() must be called after constructing a "
                "RLTrainer and before calling any methods on it."
            )

    def apply(self, func, *_args, **_kwargs):
        return func(self, *_args, **_kwargs)


@dataclass
class RLTrainerSpec:
    """The spec for constructing RLTrainer actors.

    Args:
        rl_trainer_class: The RLTrainer class to use.
        module_spec: The underlying (MA)RLModule spec to completely define the module.
        module: Alternatively the RLModule instance can be passed in directly. This
            only works if the RLTrainer is not an actor.
        backend_config: The backend config for properly distributing the RLModule.
        optimizer_config: The optimizer setting to apply during training.
        trainer_hyperparameters: The extra config for the loss/additional update. This
            should be a subclass of RLTrainerHPs. This is useful for passing in
            algorithm configs that contains the hyper-parameters for loss computation,
            change of training behaviors, etc. e.g lr, entropy_coeff.
    """

    rl_trainer_class: Type["RLTrainer"]
    module_spec: Union["SingleAgentRLModuleSpec", "MultiAgentRLModuleSpec"] = None
    module: Optional["RLModule"] = None
    trainer_scaling_config: TrainerScalingConfig = field(
        default_factory=TrainerScalingConfig
    )
    optimizer_config: Dict[str, Any] = field(default_factory=dict)
    trainer_hyperparameters: RLTrainerHPs = field(default_factory=RLTrainerHPs)
    framework_hyperparameters: FrameworkHPs = field(default_factory=FrameworkHPs)

    def get_params_dict(self) -> Dict[str, Any]:
        """Returns the parameters than be passed to the RLTrainer constructor."""
        return {
            "module": self.module,
            "module_spec": self.module_spec,
            "trainer_scaling_config": self.trainer_scaling_config,
            "optimizer_config": self.optimizer_config,
            "trainer_hyperparameters": self.trainer_hyperparameters,
            "framework_hyperparameters": self.framework_hyperparameters,
        }

    def build(self) -> "RLTrainer":
        """Builds the RLTrainer instance."""
        return self.rl_trainer_class(**self.get_params_dict())
