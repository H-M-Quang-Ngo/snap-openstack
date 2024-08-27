# Copyright (c) 2023 Canonical Ltd.
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

"""Observability feature.

Feature to deploy and manage observability, powered by COS Lite.
This feature have options to deploy a COS Lite stack internally
or point to an external COS Lite.
"""

import asyncio
import enum
import logging
from pathlib import Path

import click
from packaging.version import Version
from rich.console import Console
from rich.status import Status

from sunbeam.clusterd.service import (
    ClusterServiceUnavailableException,
    ConfigItemNotFoundException,
)
from sunbeam.core.checks import (
    Check,
    JujuControllerRegistrationCheck,
    run_preflight_checks,
)
from sunbeam.core.common import (
    BaseStep,
    Result,
    ResultType,
    convert_proxy_to_model_configs,
    read_config,
    run_plan,
    update_config,
    update_status_background,
)
from sunbeam.core.deployment import Deployment
from sunbeam.core.juju import (
    JujuHelper,
    JujuStepHelper,
    JujuWaitException,
    TimeoutException,
    run_sync,
)
from sunbeam.core.k8s import K8SHelper
from sunbeam.core.manifest import (
    AddManifestStep,
    CharmManifest,
    Manifest,
    SoftwareConfig,
    TerraformManifest,
)
from sunbeam.core.openstack import OPENSTACK_MODEL
from sunbeam.core.steps import PatchLoadBalancerServicesStep
from sunbeam.core.terraform import (
    TerraformException,
    TerraformHelper,
    TerraformInitStep,
)
from sunbeam.features.interface.v1.base import FeatureRequirement
from sunbeam.features.interface.v1.openstack import (
    DisableOpenStackApplicationStep,
    EnableOpenStackApplicationStep,
    OpenStackControlPlaneFeature,
    TerraformPlanLocation,
)
from sunbeam.steps.k8s import CREDENTIAL_SUFFIX

LOG = logging.getLogger(__name__)
console = Console()

OBSERVABILITY_FEATURE_KEY = "ObservabilityProviderType"
OBSERVABILITY_MODEL = "observability"
OBSERVABILITY_DEPLOY_TIMEOUT = 1200  # 20 minutes
COS_TFPLAN = "cos-plan"
GRAFANA_AGENT_TFPLAN = "grafana-agent-plan"
COS_CONFIG_KEY = "TerraformVarsFeatureObservabilityPlanCos"
GRAFANA_AGENT_CONFIG_KEY = "TerraformVarsFeatureObservabilityPlanGrafanaAgent"

COS_CHANNEL = "latest/stable"
GRAFANA_AGENT_CHANNEL = "latest/stable"
GRAFANA_AGENT_K8S_CHANNEL = "latest/stable"
OBSERVABILITY_OFFER_INTERFACES = [
    "grafana_dashboard",
    "prometheus_remote_write",
    "loki_push_api",
]


class ProviderType(enum.Enum):
    EXTERNAL = 1
    EMBEDDED = 2


class DeployObservabilityStackStep(BaseStep, JujuStepHelper):
    """Deploy Observability Stack using Terraform."""

    _CONFIG = COS_CONFIG_KEY

    def __init__(
        self,
        feature: "ObservabilityFeature",
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
    ):
        super().__init__("Deploy Observability Stack", "Deploying Observability Stack")
        self.feature = feature
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = self.feature.manifest
        self.client = self.feature.deployment.get_client()
        self.model = OBSERVABILITY_MODEL
        self.cloud = K8SHelper.get_cloud(self.feature.deployment.name)

    def run(self, status: Status | None = None) -> Result:
        """Execute configuration using terraform."""
        proxy_settings = self.feature.deployment.get_proxy_settings()
        model_config = convert_proxy_to_model_configs(proxy_settings)
        model_config.update({"workload-storage": K8SHelper.get_default_storageclass()})
        extra_tfvars = {
            "model": self.model,
            "cloud": self.cloud,
            "credential": f"{self.cloud}{CREDENTIAL_SUFFIX}",
            "config": model_config,
        }

        try:
            self.update_status(status, "deploying services")
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            LOG.exception("Error deploying Observability Stack")
            return Result(ResultType.FAILED, str(e))

        apps = run_sync(self.jhelper.get_application_names(self.model))
        LOG.debug(f"Application monitored for readiness: {apps}")
        queue: asyncio.queues.Queue[str] = asyncio.queues.Queue(maxsize=len(apps))
        task = run_sync(update_status_background(self, apps, queue, status))
        try:
            run_sync(
                self.jhelper.wait_until_active(
                    self.model,
                    apps,
                    timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                    queue=queue,
                )
            )
        except (JujuWaitException, TimeoutException) as e:
            LOG.debug("Failed to deploy Observability Stack", exc_info=True)
            return Result(ResultType.FAILED, str(e))
        finally:
            if not task.done():
                task.cancel()

        return Result(ResultType.COMPLETED)


class UpdateObservabilityModelConfigStep(BaseStep, JujuStepHelper):
    """Update Observability Model config  using Terraform."""

    _CONFIG = COS_CONFIG_KEY

    def __init__(
        self,
        feature: "ObservabilityFeature",
        tfhelper: TerraformHelper,
    ):
        super().__init__(
            "Update Observability Model Config",
            "Updating Observability proxy related model config",
        )
        self.feature = feature
        self.tfhelper = tfhelper
        self.manifest = self.feature.manifest
        self.client = self.feature.deployment.get_client()
        self.model = OBSERVABILITY_MODEL
        self.cloud = K8SHelper.get_cloud(self.feature.deployment.name)

    def run(self, status: Status | None = None) -> Result:
        """Execute configuration using terraform."""
        proxy_settings = self.feature.deployment.get_proxy_settings()
        model_config = convert_proxy_to_model_configs(proxy_settings)
        model_config.update({"workload-storage": K8SHelper.get_default_storageclass()})
        extra_tfvars = {
            "model": self.model,
            "cloud": self.cloud,
            "credential": f"{self.cloud}{CREDENTIAL_SUFFIX}",
            "config": model_config,
        }

        try:
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=extra_tfvars,
                tf_apply_extra_args=["-target=juju_model.cos"],
            )
        except TerraformException as e:
            LOG.exception("Error updating Observability Model config")
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class RemoveSaasApplicationsStep(BaseStep):
    """Removes SAAS offers from given model.

    This is a workaround around:
    https://github.com/juju/terraform-provider-juju/issues/473

    For CMR on same controller, offering_model should be sufficient.
    For CMR across controllers, offering_interfaces are required to
    determine the SAAS Applications.
    """

    def __init__(
        self,
        jhelper: JujuHelper,
        model: str,
        offering_model: str | None = None,
        offering_interfaces: list | None = None,
    ):
        super().__init__(
            f"Purge SAAS Offers: {model}", f"Purging SAAS Offers from {model}"
        )
        self.jhelper = jhelper
        self.model = model
        self.offering_model = offering_model
        self.offering_interfaces = offering_interfaces
        self._remote_app_to_delete: list[str] = []

    def _get_remote_apps_from_model(
        self, remote_applications: dict, offering_model: str
    ) -> list:
        """Get all remote apps connected to offering model."""
        remote_apps = []
        for name, remote_app in remote_applications.items():
            if not remote_app:
                continue

            offer = remote_app.get("offer-url")
            if not offer:
                continue

            LOG.debug("Processing offer: %s", offer)
            model_name = offer.split("/", 1)[1].split(".", 1)[0]
            if model_name == offering_model:
                remote_apps.append(name)

        return remote_apps

    def _get_remote_apps_from_interfaces(
        self, remote_applications: dict, offering_interfaces: list
    ) -> list:
        """Get all remote apps which has offered given interfaces."""
        remote_apps = []
        for name, remote_app in remote_applications.items():
            if not remote_app:
                continue

            LOG.debug(f"Processing remote app: {remote_app}")
            for endpoint in remote_app.get("endpoints", {}):
                if endpoint.get("interface") in offering_interfaces:
                    remote_apps.append(name)

        return remote_apps

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not."""
        super().is_skip

        model_status = run_sync(self.jhelper.get_model_status(self.model))
        remote_applications = model_status.get("remote-applications")
        if not remote_applications:
            return Result(ResultType.SKIPPED, "No remote applications found")

        LOG.debug(
            "Remote applications found: %s", ", ".join(remote_applications.keys())
        )
        if self.offering_model:
            self._remote_app_to_delete.extend(
                self._get_remote_apps_from_model(
                    remote_applications, self.offering_model
                )
            )

        if self.offering_interfaces:
            self._remote_app_to_delete.extend(
                self._get_remote_apps_from_interfaces(
                    remote_applications, self.offering_interfaces
                )
            )

        if len(self._remote_app_to_delete) == 0:
            return Result(ResultType.SKIPPED, "No remote applications to remove")

        return Result(ResultType.COMPLETED)

    def run(self, status: Status | None = None) -> Result:
        """Execute to remove SAAS apps."""
        if not self._remote_app_to_delete:
            return Result(ResultType.COMPLETED)

        model = run_sync(self.jhelper.get_model(self.model))

        for saas in self._remote_app_to_delete:
            LOG.debug("Removing remote application %s", saas)
            run_sync(model.remove_saas(saas))
        return Result(ResultType.COMPLETED)


class DeployGrafanaAgentStep(BaseStep, JujuStepHelper):
    """Deploy Grafana Agent using Terraform."""

    _CONFIG = GRAFANA_AGENT_CONFIG_KEY

    def __init__(
        self,
        feature: "ObservabilityFeature",
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        accepted_app_status: list[str] = ["active"],
    ):
        super().__init__("Deploy Grafana Agent", "Deploy Grafana Agent")
        self.feature = feature
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = self.feature.manifest
        self.accepted_app_status = accepted_app_status
        self.client = self.feature.deployment.get_client()
        self.model = self.feature.deployment.openstack_machines_model

    def run(self, status: Status | None = None) -> Result:
        """Execute configuration using terraform."""
        extra_tfvars = {
            "principal-application-model": self.model,
            "principal-application": "openstack-hypervisor",
        }
        # Offer URLs from COS are added from feature
        extra_tfvars.update(self.feature.set_tfvars_on_enable())

        try:
            self.update_status(status, "deploying services")
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            LOG.exception("Error deploying grafana agent")
            return Result(ResultType.FAILED, str(e))

        app = "grafana-agent"
        LOG.debug(f"Application monitored for readiness: {app}")
        try:
            run_sync(
                self.jhelper.wait_application_ready(
                    app,
                    self.model,
                    accepted_status=self.accepted_app_status,
                    timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                )
            )
        except (JujuWaitException, TimeoutException) as e:
            LOG.debug("Failed to deploy grafana agent", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class RemoveObservabilityStackStep(BaseStep, JujuStepHelper):
    """Remove Observability Stack using Terraform."""

    def __init__(
        self,
        feature: "ObservabilityFeature",
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
    ):
        super().__init__("Remove Observability Stack", "Removing Observability Stack")
        self.feature = feature
        self.tfhelper = tfhelper
        self.manifest = self.feature.manifest
        self.jhelper = jhelper
        self.model = OBSERVABILITY_MODEL
        self.cloud = K8SHelper.get_cloud(self.feature.deployment.name)

    def run(self, status: Status | None = None) -> Result:
        """Execute configuration using terraform."""
        try:
            self.tfhelper.destroy()
        except TerraformException as e:
            LOG.exception("Error destroying Observability Stack")
            return Result(ResultType.FAILED, str(e))

        try:
            run_sync(
                self.jhelper.wait_model_gone(
                    self.model,
                    timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                )
            )
        except TimeoutException as e:
            LOG.debug("Failed to destroy Observability Stack", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class RemoveGrafanaAgentStep(BaseStep, JujuStepHelper):
    """Remove Grafana Agent using Terraform."""

    _CONFIG = GRAFANA_AGENT_CONFIG_KEY

    def __init__(
        self,
        feature: "ObservabilityFeature",
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
    ):
        super().__init__("Remove Grafana Agent", "Removing Grafana Agent")
        self.feature = feature
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = self.feature.manifest
        self.client = self.feature.deployment.get_client()
        self.model = self.feature.deployment.openstack_machines_model

    def run(self, status: Status | None = None) -> Result:
        """Execute configuration using terraform."""
        try:
            self.tfhelper.destroy()
        except TerraformException as e:
            LOG.exception("Error destroying grafana agent")
            return Result(ResultType.FAILED, str(e))

        apps = ["grafana-agent"]
        try:
            run_sync(
                self.jhelper.wait_application_gone(
                    apps,
                    self.model,
                    timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                )
            )
        except TimeoutException as e:
            LOG.debug("Failed to destroy grafana agent", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        extra_tfvars = {
            "principal-application-model": self.model,
            "principal-application": "openstack-hypervisor",
        }
        # Offer URLs from COS are added from feature
        extra_tfvars.update(self.feature.set_tfvars_on_disable())
        update_config(self.client, self._CONFIG, extra_tfvars)

        return Result(ResultType.COMPLETED)


class PatchCosLoadBalancerStep(PatchLoadBalancerServicesStep):
    def services(self) -> list[str]:
        """List of services to patch."""
        return ["traefik"]

    def model(self) -> str:
        """Name of the model to use."""
        return OBSERVABILITY_MODEL


class IntegrateRemoteCosOffersStep(BaseStep, JujuStepHelper):
    """Integrate COS Offers across Juju controllers.

    This is a workaround for https://github.com/juju/terraform-provider-juju/issues/119
    """

    def __init__(
        self,
        feature: "ObservabilityFeature",
        jhelper: JujuHelper,
    ):
        super().__init__(
            "Integrate external Observability offers",
            "Integrating external Observability offers",
        )
        self.feature = feature
        self.jhelper = jhelper
        self.model = OPENSTACK_MODEL
        self.relations = [
            (
                "grafana-agent:grafana-dashboards-provider",
                self.feature.grafana_offer_url,
            ),
            ("grafana-agent:send-remote-write", self.feature.prometheus_offer_url),
            ("grafana-agent:logging-consumer", self.feature.loki_offer_url),
        ]

    def run(self, status: Status | None = None) -> Result:
        """Execute integrations using external offers."""
        for model in [
            OPENSTACK_MODEL,
            self.feature.deployment.openstack_machines_model,
        ]:
            for relation_pair in self.relations:
                if relation_pair[0] and relation_pair[1]:
                    self.integrate(
                        model,
                        relation_pair[0],
                        relation_pair[1],
                    )

        for model in [
            OPENSTACK_MODEL,
            self.feature.deployment.openstack_machines_model,
        ]:
            app = "grafana-agent"
            LOG.debug(f"Application monitored for readiness: {app}")
            try:
                run_sync(
                    self.jhelper.wait_application_ready(
                        app,
                        model,
                        timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                    )
                )
            except (JujuWaitException, TimeoutException) as e:
                LOG.debug("Failed to deploy grafana agent", exc_info=True)
                return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class RemoveRemoteCosOffersStep(BaseStep, JujuStepHelper):
    """Remove COS Offers across Juju controllers.

    This is a workaround for https://github.com/juju/terraform-provider-juju/issues/119
    """

    def __init__(
        self,
        feature: "ObservabilityFeature",
        jhelper: JujuHelper,
    ):
        super().__init__(
            "Remove external Observability offers",
            "Removing external Observability offers",
        )
        self.feature = feature
        self.jhelper = jhelper
        self.endpoints = [
            "grafana-agent:grafana-dashboards-provider",
            "grafana-agent:send-remote-write",
            "grafana-agent:logging-consumer",
        ]

    def _get_relations(self, model: str, endpoints: list[str]) -> list[tuple]:
        """Return model relations for the provided endpoints."""
        relations = []
        model_status = run_sync(self.jhelper.get_model_status(model))
        model_relations = [r.get("key") for r in model_status.get("relations", {})]
        for endpoint in endpoints:
            for relation in model_relations:
                if endpoint in relation:
                    relations.append(tuple(relation.split(" ")))
                    break

        return relations

    def run(self, status: Status | None = None) -> Result:
        """Execute integrations using external offers."""
        for model in [
            OPENSTACK_MODEL,
            self.feature.deployment.openstack_machines_model,
        ]:
            relations = self._get_relations(model, self.endpoints)
            LOG.debug(f"List of relations to remove in model {model}: {relations}")
            for relation_pair in relations:
                self.remove_relation(
                    model,
                    relation_pair[0],
                    relation_pair[1],
                )

        for model in [
            OPENSTACK_MODEL,
            self.feature.deployment.openstack_machines_model,
        ]:
            app = "grafana-agent"
            LOG.debug(f"Application monitored for readiness: {app}")
            try:
                run_sync(
                    self.jhelper.wait_application_ready(
                        app,
                        model,
                        accepted_status=["blocked"],
                        timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                    )
                )
            except (JujuWaitException, TimeoutException) as e:
                LOG.debug("Failed to deploy grafana agent", exc_info=True)
                return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class ObservabilityFeature(OpenStackControlPlaneFeature):
    version = Version("0.0.1")
    requires = {FeatureRequirement("telemetry")}

    def __init__(self, deployment: Deployment) -> None:
        super().__init__(
            "observability", deployment, TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO
        )
        self.tfplan_cos = COS_TFPLAN
        self.tfplan_cos_dir = "deploy-cos"
        self.tfplan_grafana_agent = GRAFANA_AGENT_TFPLAN
        self.tfplan_grafana_agent_dir = "deploy-grafana-agent"
        self.tfplan_grafana_agent_k8s_dir = "deploy-grafana-agent-k8s"

        self.external = False
        self.prometheus_offer_url = ""
        self.grafana_offer_url = ""
        self.loki_offer_url = ""

    @property
    def manifest(self) -> Manifest:
        """Return the manifest."""
        if self._manifest is not None:
            return self._manifest

        self._manifest: Manifest = self.deployment.get_manifest()
        return self._manifest

    def manifest_defaults(self) -> SoftwareConfig:
        """Feature software configuration."""
        return SoftwareConfig(
            charms={
                "cos-traefik-k8s": CharmManifest(channel=COS_CHANNEL),
                "alertmanager-k8s": CharmManifest(channel=COS_CHANNEL),
                "grafana-k8s": CharmManifest(channel=COS_CHANNEL),
                "catalogue-k8s": CharmManifest(channel=COS_CHANNEL),
                "prometheus-k8s": CharmManifest(channel=COS_CHANNEL),
                "loki-k8s": CharmManifest(channel=COS_CHANNEL),
                "grafana-agent": CharmManifest(channel=GRAFANA_AGENT_CHANNEL),
                "grafana-agent-k8s": CharmManifest(channel=GRAFANA_AGENT_K8S_CHANNEL),
            },
            terraform={
                self.tfplan_cos: TerraformManifest(
                    source=Path(__file__).parent / "etc" / self.tfplan_cos_dir
                ),
                self.tfplan_grafana_agent: TerraformManifest(
                    source=Path(__file__).parent
                    / "etc"  # noqa: W503
                    / self.tfplan_grafana_agent_dir  # noqa: W503
                ),
            },
        )

    def manifest_attributes_tfvar_map(self) -> dict:
        """Manifest attributes terraformvars map."""
        return {
            self.tfplan_cos: {
                "charms": {
                    "cos-traefik-k8s": {
                        "channel": "traefik-channel",
                        "revision": "traefik-revision",
                        "config": "traefik-config",
                    },
                    "alertmanager-k8s": {
                        "channel": "alertmanager-channel",
                        "revision": "alertmanager-revision",
                        "config": "alertmanager-config",
                    },
                    "grafana-k8s": {
                        "channel": "grafana-channel",
                        "revision": "grafana-revision",
                        "config": "grafana-config",
                    },
                    "catalogue-k8s": {
                        "channel": "catalogue-channel",
                        "revision": "catalogue-revision",
                        "config": "catalogue-config",
                    },
                    "prometheus-k8s": {
                        "channel": "prometheus-channel",
                        "revision": "prometheus-revision",
                        "config": "prometheus-config",
                    },
                    "loki-k8s": {
                        "channel": "loki-channel",
                        "revision": "loki-revision",
                        "config": "loki-config",
                    },
                }
            },
            self.tfplan_grafana_agent: {
                "charms": {
                    "grafana-agent": {
                        "channel": "grafana-agent-channel",
                        "revision": "grafana-agent-revision",
                        "config": "grafana-agent-config",
                    }
                }
            },
            self.tfplan: {
                "charms": {
                    "grafana-agent-k8s": {
                        "channel": "grafana-agent-channel",
                        "revision": "grafana-agent-revision",
                        "config": "grafana-agent-config",
                    }
                }
            },
        }

    def update_proxy_model_configs(self) -> None:
        """Update proxy model configs."""
        try:
            if not self.enabled:
                LOG.debug("Observability feature is not enabled, nothing to do")
                return

            provider = self.get_provider_type_from_cluster()
            if provider and provider == ProviderType.EXTERNAL:
                LOG.debug("Observability stack is remote, nothing to do")
                return
        except ClusterServiceUnavailableException:
            LOG.debug(
                "Failed to query for feature status, is cloud bootstrapped ?",
                exc_info=True,
            )
            return

        plan = [
            TerraformInitStep(self.deployment.get_tfhelper(self.tfplan_cos)),
            UpdateObservabilityModelConfigStep(
                self, self.deployment.get_tfhelper(self.tfplan_cos)
            ),
        ]
        run_plan(plan, console)

    def set_application_names(self) -> list:
        """Application names handled by the main terraform plan."""
        # main plan only handles grafana-agent-k8s, named grafana-agent
        return ["grafana-agent"]

    def get_cos_offer_urls(self) -> dict:
        """Return COS offer URLs."""
        if not self.external:
            tfhelper_cos = self.deployment.get_tfhelper(self.tfplan_cos)
            output = tfhelper_cos.output()
            return {
                "grafana-dashboard-offer-url": output["grafana-dashboard-offer-url"],
                "logging-offer-url": output["loki-logging-offer-url"],
                "receive-remote-write-offer-url": output[
                    "prometheus-receive-remote-write-offer-url"
                ],
            }

        # Returning empty dict as integrations are not handled in terraform plan
        # https://github.com/juju/terraform-provider-juju/issues/119
        # Should return URLs from user input when above bug is fixed
        return {}

    def set_tfvars_on_enable(self) -> dict:
        """Set terraform variables to enable the application."""
        tfvars = {
            "enable-observability": True,
        }
        tfvars.update(self.get_cos_offer_urls())
        return tfvars

    def set_tfvars_on_disable(self) -> dict:
        """Set terraform variables to disable the application."""
        return {
            "enable-observability": False,
            "grafana-dashboard-offer-url": None,
            "logging-offer-url": None,
            "receive-remote-write-offer-url": None,
        }

    def set_tfvars_on_resize(self) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    def get_provider_type(self) -> ProviderType:
        """Return provide type external or embedded."""
        return ProviderType.EXTERNAL if self.external else ProviderType.EMBEDDED

    def get_provider_type_from_cluster(self) -> str | None:
        """Return provider type from database.

        Return None if provider type is not set in database.
        """
        try:
            config = read_config(
                self.deployment.get_client(), OBSERVABILITY_FEATURE_KEY
            )
        except ConfigItemNotFoundException:
            config = {}

        return config.get("provider")

    def run_enable_plans_embedded(self):
        """Run the enablement plans for embedded."""
        jhelper = JujuHelper(self.deployment.get_connected_controller())

        tfhelper = self.deployment.get_tfhelper(self.tfplan)
        tfhelper_cos = self.deployment.get_tfhelper(self.tfplan_cos)
        tfhelper_grafana_agent = self.deployment.get_tfhelper(self.tfplan_grafana_agent)

        client = self.deployment.get_client()
        plan = []
        if self.user_manifest:
            plan.append(AddManifestStep(client, self.user_manifest))

        cos_plan = [
            TerraformInitStep(tfhelper_cos),
            DeployObservabilityStackStep(self, tfhelper_cos, jhelper),
            PatchCosLoadBalancerStep(client),
        ]

        grafana_agent_k8s_plan = [
            TerraformInitStep(tfhelper),
            EnableOpenStackApplicationStep(tfhelper, jhelper, self),
        ]

        grafana_agent_plan = [
            TerraformInitStep(tfhelper_grafana_agent),
            DeployGrafanaAgentStep(self, tfhelper_grafana_agent, jhelper),
        ]

        run_plan(plan, console)
        run_plan(cos_plan, console)
        run_plan(grafana_agent_k8s_plan, console)
        run_plan(grafana_agent_plan, console)

    def run_disable_plans_embedded(self):
        """Run the disablement plans for embedded."""
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        tfhelper = self.deployment.get_tfhelper(self.tfplan)
        tfhelper_cos = self.deployment.get_tfhelper(self.tfplan_cos)
        tfhelper_grafana_agent = self.deployment.get_tfhelper(self.tfplan_grafana_agent)

        agent_grafana_k8s_plan = [
            TerraformInitStep(tfhelper),
            DisableOpenStackApplicationStep(tfhelper, jhelper, self),
            RemoveSaasApplicationsStep(
                jhelper, OPENSTACK_MODEL, offering_model=OBSERVABILITY_MODEL
            ),
        ]

        grafana_agent_plan = [
            TerraformInitStep(tfhelper_grafana_agent),
            RemoveGrafanaAgentStep(self, tfhelper_grafana_agent, jhelper),
            RemoveSaasApplicationsStep(
                jhelper,
                self.deployment.openstack_machines_model,
                offering_model=OBSERVABILITY_MODEL,
            ),
        ]

        cos_plan = [
            TerraformInitStep(tfhelper_cos),
            RemoveObservabilityStackStep(self, tfhelper_cos, jhelper),
        ]

        run_plan(agent_grafana_k8s_plan, console)
        run_plan(grafana_agent_plan, console)
        run_plan(cos_plan, console)

    def run_enable_plans_external(self):
        """Run the enablement plans for external."""
        jhelper = JujuHelper(self.deployment.get_connected_controller())

        tfhelper = self.deployment.get_tfhelper(self.tfplan)
        tfhelper_grafana_agent = self.deployment.get_tfhelper(self.tfplan_grafana_agent)

        client = self.deployment.get_client()
        plan = []
        if self.user_manifest:
            plan.append(AddManifestStep(client, self.user_manifest))

        grafana_agent_k8s_plan = [
            TerraformInitStep(tfhelper),
            EnableOpenStackApplicationStep(
                tfhelper, jhelper, self, app_desired_status=["active", "blocked"]
            ),
        ]

        grafana_agent_plan = [
            TerraformInitStep(tfhelper_grafana_agent),
            DeployGrafanaAgentStep(
                self,
                tfhelper_grafana_agent,
                jhelper,
                accepted_app_status=["active", "blocked"],
            ),
        ]

        # Workaround as integrations are not handled in terraform plan
        # https://github.com/juju/terraform-provider-juju/issues/119
        grafana_integrations_plan = [IntegrateRemoteCosOffersStep(self, jhelper)]

        run_plan(plan, console)
        run_plan(grafana_agent_k8s_plan, console)
        run_plan(grafana_agent_plan, console)
        run_plan(grafana_integrations_plan, console)

    def run_disable_plans_external(self):
        """Run the disablement plans for external."""
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        tfhelper = self.deployment.get_tfhelper(self.tfplan)
        tfhelper_grafana_agent = self.deployment.get_tfhelper(self.tfplan_grafana_agent)

        # Workaround as integrations are not handled in terraform plan
        # https://github.com/juju/terraform-provider-juju/issues/119
        grafana_remove_offers_plan = [RemoveRemoteCosOffersStep(self, jhelper)]

        agent_grafana_k8s_plan = [
            TerraformInitStep(tfhelper),
            DisableOpenStackApplicationStep(tfhelper, jhelper, self),
            RemoveSaasApplicationsStep(
                jhelper,
                OPENSTACK_MODEL,
                offering_interfaces=OBSERVABILITY_OFFER_INTERFACES,
            ),
        ]

        grafana_agent_plan = [
            TerraformInitStep(tfhelper_grafana_agent),
            RemoveGrafanaAgentStep(self, tfhelper_grafana_agent, jhelper),
            RemoveSaasApplicationsStep(
                jhelper,
                self.deployment.openstack_machines_model,
                offering_interfaces=OBSERVABILITY_OFFER_INTERFACES,
            ),
        ]

        run_plan(grafana_remove_offers_plan, console)
        run_plan(agent_grafana_k8s_plan, console)
        run_plan(grafana_agent_plan, console)

    def run_enable_plans(self):
        """Run the enablement plans."""
        if self.external:
            self.run_enable_plans_external()
        else:
            self.run_enable_plans_embedded()

        click.echo("Observability enabled.")

    def run_disable_plans(self):
        """Run the disablement plans."""
        if self.external:
            self.run_disable_plans_external()
        else:
            self.run_disable_plans_embedded()

        click.echo("Observability disabled.")

    def pre_enable(self) -> None:
        """Handler to perform tasks before enabling the feature."""
        super().pre_enable()
        provider = self.get_provider_type_from_cluster()
        if provider and provider != self.get_provider_type().name:
            raise Exception(f"Observability provider already set to {provider!r}")

    def post_enable(self) -> None:
        """Handler to perform tasks after the feature is enabled."""
        super().post_enable()
        config = {
            "provider": self.get_provider_type().name,
        }
        update_config(self.deployment.get_client(), OBSERVABILITY_FEATURE_KEY, config)

    def pre_disable(self) -> None:
        """Handler to perform tasks before disabling the feature."""
        super().pre_enable()
        try:
            config = read_config(
                self.deployment.get_client(), OBSERVABILITY_FEATURE_KEY
            )
        except ConfigItemNotFoundException:
            config = {}

        provider = config.get("provider")
        if provider and provider != self.get_provider_type().name:
            raise Exception(f"Observability provider set to {provider!r}")

    def post_disable(self) -> None:
        """Handler to perform tasks after the feature is disabled."""
        super().post_disable()

        config: dict = {}
        update_config(self.deployment.get_client(), OBSERVABILITY_FEATURE_KEY, config)

    @click.command()
    def enable_embedded_feature(self) -> None:
        """Deploy Observability stack."""
        super().enable_feature()

    @click.command()
    def disable_embedded_feature(self) -> None:
        """Disable Observability stack."""
        super().disable_feature()

    @click.command()
    @click.argument(
        "controller",
        type=str,
    )
    @click.argument(
        "grafana-dashboard-offer-url",
        type=str,
    )
    @click.argument(
        "prometheus-receive-remote-write-offer-url",
        type=str,
    )
    @click.argument("loki-logging-offer-url", type=str)
    def enable_external_feature(
        self,
        controller: str,
        grafana_dashboard_offer_url: str,
        prometheus_receive_remote_write_offer_url: str,
        loki_logging_offer_url: str,
    ) -> None:
        """Connect to external Observability stack."""
        self.external = True
        self.prometheus_offer_url = (
            f"{controller}:{prometheus_receive_remote_write_offer_url}"
        )
        self.grafana_offer_url = f"{controller}:{grafana_dashboard_offer_url}"
        self.loki_offer_url = f"{controller}:{loki_logging_offer_url}"

        data_location = self.snap.paths.user_data
        preflight_checks: list[Check] = []
        preflight_checks.append(
            JujuControllerRegistrationCheck(controller, data_location)
        )
        run_preflight_checks(preflight_checks, console)

        super().enable_feature()

    @click.command()
    def disable_external_feature(self) -> None:
        """Disconnect from external Observability stack."""
        self.external = True
        super().disable_feature()

    @click.group(invoke_without_command=True)
    def enable_observability(self) -> None:
        """Enable Observability service."""
        ctx = click.get_current_context()
        if ctx.invoked_subcommand is None:
            click.echo(
                "WARNING: This command is deprecated. "
                "Use `sunbeam enable observability embedded` instead."
            )
            super().enable_feature()

    @click.group(invoke_without_command=True)
    def disable_observability(self) -> None:
        """Disable Observability service."""
        ctx = click.get_current_context()
        if ctx.invoked_subcommand is None:
            click.echo(
                "WARNING: This command is deprecated. "
                "Use `sunbeam disable observability embedded` instead."
            )
            super().disable_feature()

    @click.command()
    def enable_feature(self):
        """Enable Observaility feature."""

    @click.command()
    def disable_feature(self):
        """Disable Observaility feature."""

    @click.group()
    def observability_group(self):
        """Manage Observability."""

    @click.command()
    def dashboard_url(self) -> None:
        """Retrieve COS Dashboard URL."""
        jhelper = JujuHelper(self.deployment.get_connected_controller())

        with console.status("Retrieving dashboard URL from Grafana service ... "):
            # Retrieve config from juju actions
            model = OBSERVABILITY_MODEL
            app = "grafana"
            action_cmd = "get-admin-password"
            unit = run_sync(jhelper.get_leader_unit(app, model))
            if not unit:
                _message = f"Unable to get {app} leader"
                raise click.ClickException(_message)

            action_result = run_sync(jhelper.run_action(unit, model, action_cmd))

            if action_result.get("return-code", 0) > 1:
                _message = "Unable to retrieve URL from Grafana service"
                raise click.ClickException(_message)

            url = action_result.get("url")
            if url:
                console.print(url)
            else:
                _message = "No URL provided by Grafana service"
                raise click.ClickException(_message)

    def commands(self) -> dict:
        """Dict of clickgroup along with commands."""
        try:
            embedded_provider = False
            enabled = self.enabled
            provider = self.get_provider_type_from_cluster()
            if provider and provider == ProviderType.EMBEDDED.name:
                embedded_provider = True
        except ClusterServiceUnavailableException:
            LOG.debug(
                "Failed to query for feature status, is cloud bootstrapped ?",
                exc_info=True,
            )
            enabled = False

        commands = {
            "enable": [{"name": self.name, "command": self.enable_observability}],
            "disable": [{"name": self.name, "command": self.disable_observability}],
            f"enable.{self.name}": [
                {"name": "embedded", "command": self.enable_embedded_feature},
                {"name": "external", "command": self.enable_external_feature},
            ],
            f"disable.{self.name}": [
                {"name": "embedded", "command": self.disable_embedded_feature},
                {"name": "external", "command": self.disable_external_feature},
            ],
        }

        if enabled and embedded_provider:
            commands.update(
                {
                    "init": [
                        {"name": "observability", "command": self.observability_group}
                    ],
                    "init.observability": [
                        {"name": "dashboard-url", "command": self.dashboard_url}
                    ],
                }
            )
        return commands
