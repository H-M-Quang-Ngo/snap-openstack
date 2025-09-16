# SPDX-FileCopyrightText: 2025 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import Any

from rich.status import Status

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import (
    ConfigItemNotFoundException,
)
from sunbeam.core.common import Result, ResultType, read_config
from sunbeam.core.deployment import Deployment, Networks
from sunbeam.core.juju import (
    JujuHelper,
)
from sunbeam.core.manifest import Manifest
from sunbeam.core.steps import (
    AddMachineUnitsStep,
    DeployMachineApplicationStep,
    DestroyMachineApplicationStep,
)
from sunbeam.core.terraform import TerraformException, TerraformHelper

LOG = logging.getLogger(__name__)
CONFIG_KEY = "TerraformVarsManilaDataPlan"
APPLICATION = "manila-data"
MANILA_DATA_APP_TIMEOUT = 600
MANILA_DATA_UNIT_TIMEOUT = 600


def get_mandatory_control_plane_offers(
    tfhelper: TerraformHelper,
) -> dict[str, str | None]:
    """Get mandatory control plane offers."""
    openstack_tf_output = tfhelper.output()

    tfvars = {
        "keystone-offer-url": openstack_tf_output.get("keystone-offer-url"),
        "database-offer-url": openstack_tf_output.get("manila-data-database-offer-url"),
        "amqp-offer-url": openstack_tf_output.get("rabbitmq-offer-url"),
    }
    return tfvars


class DeployManilaDataApplicationStep(DeployMachineApplicationStep):
    """Deploy Manila Data application using Terraform."""

    def __init__(
        self,
        deployment: Deployment,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
        refresh: bool = False,
    ):
        super().__init__(
            deployment,
            client,
            tfhelper,
            jhelper,
            manifest,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Deploy Manila Data",
            "Deploying Manila Data",
            refresh,
        )
        self._offers: dict[str, str | None] = {}

    def get_application_timeout(self) -> int:
        """Return application timeout in seconds."""
        return MANILA_DATA_APP_TIMEOUT

    def get_accepted_application_status(self) -> list[str]:
        """Return accepted application status."""
        accepted_status = super().get_accepted_application_status()
        offers = self._get_offers()
        if not offers or not all(offers.values()):
            accepted_status.append("blocked")
        return accepted_status

    def _get_offers(self):
        if not self._offers:
            self._offers = get_mandatory_control_plane_offers(
                self.deployment.get_tfhelper("openstack-plan")
            )
        return self._offers

    def extra_tfvars(self) -> dict:
        """Extra terraform vars to pass to terraform apply."""
        # storage_nodes = self.client.cluster.list_nodes_by_role("storage")
        tfvars: dict[str, Any] = {
            "endpoint_bindings": [
                {
                    "space": self.deployment.get_space(Networks.MANAGEMENT),
                },
                {
                    "endpoint": "amqp",
                    "space": self.deployment.get_space(Networks.INTERNAL),
                },
                {
                    "endpoint": "database",
                    "space": self.deployment.get_space(Networks.INTERNAL),
                },
                {
                    "endpoint": "identity-credentials",
                    "space": self.deployment.get_space(Networks.INTERNAL),
                },
            ],
            "charm-manila-data-config": {},
        }

        tfvars.update(self._get_offers())

        return tfvars


class AddManilaDataUnitsStep(AddMachineUnitsStep):
    """Add Manila Data Unit."""

    def __init__(
        self,
        client: Client,
        names: list[str] | str,
        jhelper: JujuHelper,
        model: str,
        openstack_tfhelper: TerraformHelper,
    ):
        super().__init__(
            client,
            names,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Add Manila Data unit",
            "Adding Manila Data unit to machine",
        )
        self.os_tfhelper = openstack_tfhelper

    def get_unit_timeout(self) -> int:
        """Return unit timeout in seconds."""
        return MANILA_DATA_UNIT_TIMEOUT

    def get_accepted_unit_status(self) -> dict[str, list[str]]:
        """Accepted status to pass wait_units_ready function."""
        offers = get_mandatory_control_plane_offers(self.os_tfhelper)

        allow_blocked = {"agent": ["idle"], "workload": ["active", "blocked"]}
        if not offers or not all(offers.values()):
            return allow_blocked

        try:
            config = read_config(self.client, CONFIG_KEY)
        except ConfigItemNotFoundException:
            config = {}

        # check if values in offers are the same in config
        for key, value in offers.items():
            if key not in config or config[key] != value:
                return allow_blocked

        return super().get_accepted_unit_status()


class DestroyManilaDataApplicationStep(DestroyMachineApplicationStep):
    """Destroy Manila Data application using Terraform."""

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        super().__init__(
            client,
            tfhelper,
            jhelper,
            manifest,
            CONFIG_KEY,
            [APPLICATION],
            model,
            "Destroy Manila Data",
            "Destroying Manila Data",
        )

    def get_application_timeout(self) -> int:
        """Return application timeout in seconds."""
        return MANILA_DATA_APP_TIMEOUT

    def run(self, status: Status | None = None) -> Result:
        """Destroy Manila Data application."""
        # note(gboutry):this is a workaround for
        # https://github.com/juju/terraform-provider-juju/issues/473
        try:
            resources = self.tfhelper.state_list()
        except TerraformException as e:
            LOG.debug(f"Failed to list terraform state: {str(e)}")
            return Result(ResultType.FAILED, "Failed to list terraform state")

        for resource in resources:
            if "integration" in resource:
                try:
                    self.tfhelper.state_rm(resource)
                except TerraformException as e:
                    LOG.debug(f"Failed to remove resource {resource}: {str(e)}")
                    return Result(
                        ResultType.FAILED,
                        f"Failed to remove resource {resource} from state",
                    )

        return super().run(status)
