#!/usr/bin/env python
import json
import yaml
import os
import requests
import time
import logging
import click

import ray
from ray.serve.api import Deployment, deploy_group
from ray.serve.config import DeploymentMode
from ray._private.utils import import_attr
from ray import serve
from ray.serve.constants import (
    DEFAULT_CHECKPOINT_PATH,
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
)

logger = logging.getLogger(__name__)


def log_failed_request(response: requests.models.Response):
    logger.error("Request failed. Got response status code "
                 f"{response.status_code} with the following message:"
                 f"\n{response.text}")


@click.group(help="[EXPERIMENTAL] CLI for managing Serve instances on a Ray cluster.")
@click.option(
    "--address",
    "-a",
    default=os.environ.get("RAY_ADDRESS", "auto"),
    required=False,
    type=str,
    help="Address of the running Ray cluster to connect to. " 'Defaults to "auto".',
)
@click.option(
    "--namespace",
    "-n",
    default="serve",
    required=False,
    type=str,
    help='Ray namespace to connect to. Defaults to "serve".',
)
@click.option(
    "--runtime-env-json",
    default=r"{}",
    required=False,
    type=str,
    help=(
        "Runtime environment dictionary to pass into ray.init. " "Defaults to empty."
    ),
)
def cli(address, namespace, runtime_env_json):
    ray.init(
        address=address,
        namespace=namespace,
        runtime_env=json.loads(runtime_env_json),
    )


@cli.command(help="Start a detached Serve instance on the Ray cluster.")
@click.option(
    "--http-host",
    default=DEFAULT_HTTP_HOST,
    required=False,
    type=str,
    help="Host for HTTP servers to listen on. " f"Defaults to {DEFAULT_HTTP_HOST}.",
)
@click.option(
    "--http-port",
    default=DEFAULT_HTTP_PORT,
    required=False,
    type=int,
    help="Port for HTTP servers to listen on. " f"Defaults to {DEFAULT_HTTP_PORT}.",
)
@click.option(
    "--http-location",
    default=DeploymentMode.HeadOnly,
    required=False,
    type=click.Choice(list(DeploymentMode)),
    help="Location of the HTTP servers. Defaults to HeadOnly.",
)
@click.option(
    "--checkpoint-path",
    default=DEFAULT_CHECKPOINT_PATH,
    required=False,
    type=str,
    hidden=True,
)
def start(http_host, http_port, http_location, checkpoint_path):
    serve.start(
        detached=True,
        http_options=dict(
            host=http_host,
            port=http_port,
            location=http_location,
        ),
        _checkpoint_path=checkpoint_path,
    )


@cli.command(help="Shutdown the running Serve instance on the Ray cluster.")
def shutdown():
    serve.api._connect()
    serve.shutdown()


@cli.command(
    help="""
[Experimental]
Create a deployment in running Serve instance. The required argument is the
import path for the deployment: ``my_module.sub_module.file.MyClass``. The
class may or may not be decorated with ``@serve.deployment``.
""",
    hidden=True,
)
@click.argument("deployment")
@click.option(
    "--options-json",
    default=r"{}",
    required=False,
    type=str,
    help="JSON string for the deployments options",
)
def create_deployment(deployment: str, options_json: str):
    deployment_cls = import_attr(deployment)
    if not isinstance(deployment_cls, Deployment):
        deployment_cls = serve.deployment(deployment_cls)
    options = json.loads(options_json)
    deployment_cls.options(**options).deploy()


@cli.command(
    help="""
    [Experimental] Deploy a YAML configuration file via REST API to
    your Serve cluster.
    """,
    hidden=True,
)
@click.argument("config_file_name")
@click.option(
    "--address",
    "-a",
    default=os.environ.get("RAY_ADDRESS", "http://localhost:8265"),
    required=False,
    type=str,
    help="Address of the Ray dashboard to query. For example, \"http://localhost:8265\".",
)
def deploy(config_file_name: str, address: str):
    full_address_path = f"{address}/api/serve/deployments/"

    with open(config_file_name, "r") as config_file:
        config = yaml.safe_load(config_file)

    response = requests.put(full_address_path, json=config)

    if response.status_code == 200:
        logger.info("Sent deployment request successfully!\n Use "
                    "`serve status` to check your deployments' statuses.\n "
                    "Use `serve info` to retrieve your running Serve "
                    "application's current configuration.")
    else:
        log_failed_request(response)


@cli.command(
    help="[Experimental] Run YAML configuration file via Serve's Python API.",
    hidden=True,
)
@click.argument("config_file_name")
def run(config_file_name: str):
    with open(config_file_name, "r") as config:
        deployment_data_list = yaml.safe_load(config)["deployments"]

    deployments = []
    for deployment_data in deployment_data_list:
        configurables = deployment_data["configurable"]
        del deployment_data["configurable"]
        deployment_data.update(configurables)

        import_path = deployment_data["import_path"]
        del deployment_data["import_path"]

        for key in list(deployment_data.keys()):
            val = deployment_data[key]
            if isinstance(val, str) and val.lower() == "none":
                del deployment_data[key]

        deployments.append(serve.deployment(**deployment_data)(import_path))

    deploy_group(deployments)
    print("Group deployed successfully!")

    while True:
        time.sleep(100)


@cli.command(
    help="[Experimental] Get info about your Serve application's config.",
    hidden=True,
)
@click.option(
    "--address",
    "-a",
    default=os.environ.get("RAY_ADDRESS", "http://localhost:8265"),
    required=False,
    type=str,
    help="Address of the Ray dashboard to query. For example, \"http://localhost:8265\".",
)
def info(address: str):
    full_address_path = f"{address}/api/serve/deployments/"
    response = requests.get(full_address_path)
    if response.status_code == 200:
        print(response.json())
    else:
        log_failed_request(response)
