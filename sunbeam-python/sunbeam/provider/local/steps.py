# Copyright (c) 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import json
import logging
from functools import cache
from typing import Any

from rich.console import Console

import sunbeam.core.questions
from sunbeam import utils
from sunbeam.clusterd.client import Client
from sunbeam.commands.configure import (
    CLOUD_CONFIG_SECTION,
    SetHypervisorUnitsOptionsStep,
)
from sunbeam.core.common import SunbeamException
from sunbeam.core.juju import JujuHelper, run_sync
from sunbeam.core.manifest import Manifest
from sunbeam.steps import hypervisor
from sunbeam.steps.cluster_status import ClusterStatusStep

LOG = logging.getLogger(__name__)
console = Console()


def local_hypervisor_questions():
    return {
        "nic": sunbeam.core.questions.PromptQuestion(
            "External network's interface",
            description=(
                "Interface used by networking layer to allow remote access to cloud"
                " instances. This interface must be unconfigured"
                " (no IP address assigned) and connected to the external network."
            ),
        ),
    }


class LocalSetHypervisorUnitsOptionsStep(SetHypervisorUnitsOptionsStep):
    def __init__(
        self,
        client: Client,
        name: str,
        jhelper: JujuHelper,
        model: str,
        join_mode: bool = False,
        manifest: Manifest | None = None,
    ):
        super().__init__(
            client,
            [name],
            jhelper,
            model,
            manifest,
            "Apply local hypervisor settings",
            "Applying local hypervisor settings",
        )
        self.join_mode = join_mode

    def has_prompts(self) -> bool:
        """Returns true if the step has prompts that it can ask the user."""
        return True

    async def _fetch_nics(self) -> dict:
        """Fetch nics from hypervisor."""
        name = self.names[0]  # always only one name in local mode
        node = self.client.cluster.get_node_info(name)
        machine_id = str(node.get("machineid"))
        unit = await self.jhelper.get_unit_from_machine(
            "openstack-hypervisor", machine_id, self.model
        )

        action_result = await self.jhelper.run_action(
            unit.entity_id, self.model, "list-nics"
        )

        if action_result.get("return-code", 0) > 1:
            _message = f"Unable to fetch hypervisor {name!r} nics"
            raise SunbeamException(_message)
        return json.loads(action_result.get("result", "{}"))

    def prompt_for_nic(self, console: Console | None = None) -> str | None:
        """Prompt user for nic to use and do some validation."""
        if console:
            context: Any = console.status("Fetching candidate nics from hypervisor")
        else:
            context = contextlib.nullcontext()

        with context:
            nics = run_sync(self._fetch_nics())

        all_nics: list[dict] | None = nics.get("nics")
        candidate_nics: list[str] | None = nics.get("candidates")

        if not all_nics:
            # all_nics should contain every nics of the hypervisor
            # how did we get a response if there's no nics?
            raise SunbeamException("No nics found on hyperisor")

        if not candidate_nics:
            raise SunbeamException("No candidate nics found")

        local_hypervisor_bank = sunbeam.core.questions.QuestionBank(
            questions=local_hypervisor_questions(),
            console=console,
            accept_defaults=False,
        )
        nic = None
        while True:
            nic = local_hypervisor_bank.nic.ask(
                new_default=candidate_nics[0], new_choices=candidate_nics
            )
            if not nic:
                continue
            nic_state = None
            for interface in all_nics:
                if interface["name"] == nic:
                    nic_state = interface
                    break
            if not nic_state:
                continue
            LOG.debug("Selected nic %s, state: %r", nic, nic_state)
            if nic_state["configured"]:
                agree_nic_up = sunbeam.core.questions.ConfirmQuestion(
                    f"WARNING: Interface {nic} is configured. Any "
                    "configuration will be lost, are you sure you want to "
                    "continue?",
                ).ask()
                if not agree_nic_up:
                    continue
            if nic_state["up"] and not nic_state["connected"]:
                agree_nic_no_link = sunbeam.core.questions.ConfirmQuestion(
                    f"WARNING: Interface {nic} is not connected. Are "
                    "you sure you want to continue?",
                    description=(
                        "Interface is not detected as connected to any network. This"
                        " means it will most likely not work as expected."
                    ),
                ).ask()
                if not agree_nic_no_link:
                    continue
            break
        return nic

    def prompt(
        self,
        console: Console | None = None,
        show_hint: bool = False,
    ) -> None:
        """Determines if the step can take input from the user."""
        # If adding a node before configure step has run then answers will
        # not be populated yet.
        self.variables = sunbeam.core.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        remote_access_location = self.variables.get("user", {}).get(
            "remote_access_location"
        )
        # If adding new nodes to the cluster then local access makes no sense
        # so always prompt for the nic.
        preseed = {}
        if self.manifest and (
            ext_network := self.manifest.core.config.external_network
        ):
            preseed = ext_network.model_dump(by_alias=True)

        if self.join_mode or remote_access_location == utils.REMOTE_ACCESS:
            # If nic is in the preseed assume the user knows what they are doing and
            # bypass validation
            if nic := preseed.get("nic"):
                self.nics[self.names[0]] = nic
            else:
                self.nics[self.names[0]] = self.prompt_for_nic()


class LocalClusterStatusStep(ClusterStatusStep):
    def models(self) -> list[str]:
        """List of models to query status from."""
        return [self.deployment.openstack_machines_model]

    @cache
    def _has_storage(self) -> bool:
        """Check if deployment has storage."""
        return (
            len(self.deployment.get_client().cluster.list_nodes_by_role("storage")) > 0
        )

    def map_application_status(self, application: str, status: str) -> str:
        """Callback to map application status to a column.

        This callback is called for every unit status with the name of its application.
        """
        if application == hypervisor.APPLICATION:
            if status == "waiting" and not self._has_storage():
                return "active"
        return status

    def _update_microcluster_status(self, status: dict, microcluster_status: dict):
        """How to update microcluster status in the status dict."""
        for member, member_status in microcluster_status.items():
            for node_status in status[
                self.deployment.openstack_machines_model
            ].values():
                if node_status.get("name") != member:
                    continue
                node_status["clusterd-status"] = member_status
