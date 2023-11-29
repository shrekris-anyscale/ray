import logging
import math
from abc import ABCMeta, abstractmethod
from typing import List, Optional

from ray.serve._private.common import TargetCapacityDirection
from ray.serve._private.constants import CONTROL_LOOP_PERIOD_S, SERVE_LOGGER_NAME
from ray.serve._private.utils import get_capacity_adjusted_num_replicas
from ray.serve.config import AutoscalingConfig

logger = logging.getLogger(SERVE_LOGGER_NAME)


def calculate_desired_num_replicas(
    autoscaling_config: AutoscalingConfig,
    current_num_ongoing_requests: List[float],
    override_min_replicas: Optional[float] = None,
    override_max_replicas: Optional[float] = None,
) -> int:  # (desired replicas):
    """Returns the number of replicas to scale to based on the given metrics.

    Args:
        autoscaling_config: The autoscaling parameters to use for this
            calculation.
        current_num_ongoing_requests (List[float]): A list of the number of
            ongoing requests for each replica.  Assumes each entry has already
            been time-averaged over the desired lookback window.
        override_min_replicas: Overrides min_replicas from the config
            when calculating the final number of replicas.
        override_max_replicas: Overrides max_replicas from the config
            when calculating the final number of replicas.

    Returns:
        desired_num_replicas: The desired number of replicas to scale to, based
            on the input metrics and the current number of replicas.

    """
    current_num_replicas = len(current_num_ongoing_requests)
    if current_num_replicas == 0:
        raise ValueError("Number of replicas cannot be zero")

    # The number of ongoing requests per replica, averaged over all replicas.
    num_ongoing_requests_per_replica: float = sum(current_num_ongoing_requests) / len(
        current_num_ongoing_requests
    )

    # Example: if error_ratio == 2.0, we have two times too many ongoing
    # requests per replica, so we desire twice as many replicas.
    error_ratio: float = (
        num_ongoing_requests_per_replica
        / autoscaling_config.target_num_ongoing_requests_per_replica
    )

    # If error ratio >= 1, then the number of ongoing requests per
    # replica exceeds the target and we will make an upscale decision,
    # so we apply the upscale smoothing factor. Otherwise, the number of
    # ongoing requests per replica is lower than the target and we will
    # make a downscale decision, so we apply the downscale smoothing
    # factor.
    if error_ratio >= 1:
        smoothing_factor = autoscaling_config.get_upscale_smoothing_factor()
    else:
        smoothing_factor = autoscaling_config.get_downscale_smoothing_factor()

    # Multiply the distance to 1 by the smoothing ("gain") factor (default=1).
    smoothed_error_ratio = 1 + ((error_ratio - 1) * smoothing_factor)
    desired_num_replicas = math.ceil(current_num_replicas * smoothed_error_ratio)

    # If error_ratio = 0, meaning there is no more traffic, and desired
    # num replicas is stuck at a positive number due to the math.ceil
    # above, decrease desired_num_replicas by one so that the deployment
    # can eventually scale to 0.
    if (
        error_ratio == 0
        and desired_num_replicas == current_num_replicas
        and desired_num_replicas >= 1
    ):
        desired_num_replicas -= 1

    min_replicas = autoscaling_config.min_replicas
    max_replicas = autoscaling_config.max_replicas
    if override_min_replicas is not None:
        min_replicas = override_min_replicas
    if override_max_replicas is not None:
        max_replicas = override_max_replicas

    # Ensure scaled_min_replicas <= desired_num_replicas <= scaled_max_replicas.
    desired_num_replicas = max(min_replicas, min(max_replicas, desired_num_replicas))

    return desired_num_replicas


class AutoscalingPolicy:
    """Defines the interface for an autoscaling policy.

    To add a new autoscaling policy, a class should be defined that provides
    this interface. The class may be stateful, in which case it may also want
    to provide a non-default constructor. However, this state will be lost when
    the controller recovers from a failure.
    """

    __metaclass__ = ABCMeta

    def __init__(self, config: AutoscalingConfig):
        """Initialize the policy using the specified config dictionary."""
        self.config = config

    @abstractmethod
    def get_decision_num_replicas(
        self,
        curr_target_num_replicas: int,
        current_num_ongoing_requests: List[float],
        current_handle_queued_queries: float,
    ) -> int:
        """Make a decision to scale replicas.

        Arguments:
            current_num_ongoing_requests: List[float]: List of number of
                ongoing requests for each replica.
            curr_target_num_replicas: The number of replicas that the
                deployment is currently trying to scale to.
            current_handle_queued_queries : The number of handle queued queries,
                if there are multiple handles, the max number of queries at
                a single handle should be passed in

        Returns:
            int: The new number of replicas to scale to.
        """
        return curr_target_num_replicas


class BasicAutoscalingPolicy(AutoscalingPolicy):
    """The default autoscaling policy based on basic thresholds for scaling.
    There is a minimum threshold for the average queue length in the cluster
    to scale up and a maximum threshold to scale down. Each period, a 'scale
    up' or 'scale down' decision is made. This decision must be made for a
    specified number of periods in a row before the number of replicas is
    actually scaled. See config options for more details.  Assumes
    `get_decision_num_replicas` is called once every CONTROL_LOOP_PERIOD_S
    seconds.
    """

    def __init__(self, config: AutoscalingConfig):
        self.config = config
        # TODO(architkulkarni): Make configurable via AutoscalingConfig
        self.loop_period_s = CONTROL_LOOP_PERIOD_S
        self.scale_up_consecutive_periods = int(
            config.upscale_delay_s / self.loop_period_s
        )
        self.scale_down_consecutive_periods = int(
            config.downscale_delay_s / self.loop_period_s
        )

        # Keeps track of previous decisions. Each time the load is above
        # 'scale_up_threshold', the counter is incremented and each time it is
        # below 'scale_down_threshold', the counter is decremented. When the
        # load is between the thresholds or a scaling decision is made, the
        # counter is reset to 0.
        # TODO(architkulkarni): It may be too noisy to reset the counter each
        # time the direction changes, especially if we calculate frequently
        # (like every 0.1s).  A potentially less noisy option is to not reset
        # the counter, and instead only increment/decrement until we reach
        # scale_up_periods or scale_down_periods.
        self.decision_counter = 0

        # Scales the min_replicas and max_replicas when calculating the final
        # number of replicas.
        self.target_capacity: Optional[float] = None

        # Determines what lower bound to use when calculating num_replicas.
        # When the target_capacity_direction is UP, the lower bound defaults
        # to using initial_replicas and falls back to min_replicas if that value
        # is None. Otherwise, the lower bound is based on min_replicas.
        self.target_capacity_direction: Optional[TargetCapacityDirection] = None

    def get_decision_num_replicas(
        self,
        curr_target_num_replicas: int,
        current_num_ongoing_requests: List[float],
        current_handle_queued_queries: float,
    ) -> int:
        if len(current_num_ongoing_requests) == 0:
            # When 0 replicas and queries are queued, scale up the replicas
            if current_handle_queued_queries > 0:
                return max(
                    math.ceil(1 * self.config.get_upscale_smoothing_factor()),
                    curr_target_num_replicas,
                )
            return curr_target_num_replicas

        decision_num_replicas = curr_target_num_replicas

        desired_num_replicas = calculate_desired_num_replicas(
            self.config,
            current_num_ongoing_requests,
            self.get_curr_lower_bound(),
            self.get_capacity_adjusted_max_replicas(),
        )
        # Scale up.
        if desired_num_replicas > curr_target_num_replicas:
            # If the previous decision was to scale down (the counter was
            # negative), we reset it and then increment it (set to 1).
            # Otherwise, just increment.
            if self.decision_counter < 0:
                self.decision_counter = 0
            self.decision_counter += 1

            # Only actually scale the replicas if we've made this decision for
            # 'scale_up_consecutive_periods' in a row.
            if self.decision_counter > self.scale_up_consecutive_periods:
                self.decision_counter = 0
                decision_num_replicas = desired_num_replicas

        # Scale down.
        elif desired_num_replicas < curr_target_num_replicas:
            # If the previous decision was to scale up (the counter was
            # positive), reset it to zero before decrementing.
            if self.decision_counter > 0:
                self.decision_counter = 0
            self.decision_counter -= 1

            # Only actually scale the replicas if we've made this decision for
            # 'scale_down_consecutive_periods' in a row.
            if self.decision_counter < -self.scale_down_consecutive_periods:
                self.decision_counter = 0
                decision_num_replicas = desired_num_replicas

        # Do nothing.
        else:
            self.decision_counter = 0

        return decision_num_replicas

    def set_target_capacity(
        self,
        new_target_capacity: Optional[float],
        new_target_capacity_direction: Optional[TargetCapacityDirection],
    ):
        self.target_capacity = new_target_capacity
        self.target_capacity_direction = new_target_capacity_direction

    def get_capacity_adjusted_min_replicas(self) -> int:
        return get_capacity_adjusted_num_replicas(
            self.config.min_replicas, self.target_capacity
        )

    def get_capacity_adjusted_initial_replicas(self) -> Optional[int]:
        if self.config.initial_replicas is None:
            return None
        else:
            return get_capacity_adjusted_num_replicas(
                self.config.initial_replicas, self.target_capacity
            )

    def get_capacity_adjusted_max_replicas(self) -> int:
        return get_capacity_adjusted_num_replicas(
            self.config.max_replicas, self.target_capacity
        )

    def apply_initial_bounds(self, curr_target_num_replicas: int) -> int:
        """Gets a valid target_num_replicas using the current value and bounds."""

        initial_upper_bound = self.get_capacity_adjusted_max_replicas()

        if self.get_capacity_adjusted_initial_replicas() is not None and (
            self.target_capacity_direction is None
            or self.target_capacity_direction == TargetCapacityDirection.UP
        ):
            initial_lower_bound = self.get_capacity_adjusted_initial_replicas()
        else:
            initial_lower_bound = self.get_capacity_adjusted_min_replicas()

        return max(
            initial_lower_bound, min(initial_upper_bound, curr_target_num_replicas)
        )

    def get_curr_lower_bound(self) -> int:
        if self.get_capacity_adjusted_initial_replicas() is not None and (
            self.target_capacity_direction == TargetCapacityDirection.UP
        ):
            return self.get_capacity_adjusted_initial_replicas()
        else:
            return self.get_capacity_adjusted_min_replicas()
