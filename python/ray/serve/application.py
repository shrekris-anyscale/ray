from typing import (
    Dict,
    Optional,
    List,
)

from ray.serve.deployment import (
    Deployment,
    schema_to_deployment,
    deployment_to_schema,
)
from ray.serve.schema import (
    ServeApplicationSchema,
)


class ImmutableDeploymentDict(dict):
    def __init__(self, deployments: Dict[str, Deployment]):
        super().__init__()
        self.update(deployments)

    def __setitem__(self, *args):
        """Not allowed. Modify deployment options using set_options instead."""
        raise RuntimeError(
            "Setting deployments in a built app is not allowed. Modify the "
            'options using app.deployments["deployment"].set_options instead.'
        )


class Application:
    """A static, pre-built Serve application.

    An application consists of a number of Serve deployments that can send
    requests to each other. One of the deployments acts as the "ingress,"
    meaning that it receives external traffic and is the entrypoint to the
    application.

    The ingress deployment can be accessed via app.ingress and a dictionary of
    all deployments can be accessed via app.deployments.

    The config options of each deployment can be modified using set_options:
    app.deployments["name"].set_options(...).

    This application object can be written to a config file and later deployed
    to production using the Serve CLI or REST API.
    """

    def __init__(self, deployments: List[Deployment]):
        deployment_dict = {}
        for d in deployments:
            if not isinstance(d, Deployment):
                raise TypeError(f"Got {type(d)}. Expected deployment.")
            elif d.name in deployment_dict:
                raise ValueError(f"App got multiple deployments named '{d.name}'.")

            deployment_dict[d.name] = d

        self._deployments = ImmutableDeploymentDict(deployment_dict)

    @property
    def deployments(self) -> ImmutableDeploymentDict:
        return self._deployments

    @property
    def ingress(self) -> Optional[Deployment]:
        """Gets the app's ingress, if one exists.

        The ingress is the single deployment with a non-None route prefix. If more
        or less than one deployment has a route prefix, no single ingress exists,
        so returns None.
        """

        ingress = None

        for deployment in self._deployments.values():
            if deployment.route_prefix is not None:
                if ingress is None:
                    ingress = deployment
                else:
                    return None

        return ingress

    def to_dict(self) -> Dict:
        """Returns this Application's deployments as a dictionary.

        This dictionary adheres to the Serve REST API schema. It can be deployed
        via the Serve REST API.

        Returns:
            Dict: The Application's deployments formatted in a dictionary.
        """

        return ServeApplicationSchema(
            deployments=[deployment_to_schema(d) for d in self._deployments.values()]
        ).dict()

    @classmethod
    def from_dict(cls, d: Dict) -> "Application":
        """Converts a dictionary of deployment data to an application.

        Takes in a dictionary matching the Serve REST API schema and converts
        it to an application containing those deployments.

        Args:
            d: A dictionary containing the deployments' data that matches
                the Serve REST API schema.

        Returns:
            Application: a new application object containing the deployments.
        """

        schema = ServeApplicationSchema.parse_obj(d)
        return cls([schema_to_deployment(s) for s in schema.deployments])
