# SPDX-FileCopyrightText: 2025 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import Mock, patch

import pytest

from sunbeam.features.maintenance.commands import enable


@pytest.fixture
def base_mocks():
    """Common mocks for the `enable` commands."""
    with (
        patch("sunbeam.features.maintenance.commands.JujuHelper") as mock_juju_helper,
        patch(
            "sunbeam.features.maintenance.commands.get_cluster_status"
        ) as mock_get_cluster_status,
        patch(
            "sunbeam.features.maintenance.commands.run_preflight_checks"
        ) as mock_run_preflight_checks,
        patch(
            "sunbeam.features.maintenance.commands.OperationViewer"
        ) as mock_operation_viewer,
        patch("sunbeam.features.maintenance.commands.run_plan") as mock_run_plan,
        patch(
            "sunbeam.features.maintenance.commands.CreateWatcherHostMaintenanceAuditStep"
        ) as mock_create_watcher_step,
    ):
        # Setup default mock behavior
        mock_get_cluster_status.return_value = {"test-node": "compute"}
        mock_run_preflight_checks.return_value = []
        mock_create_watcher_step.__name__ = "CreateWatcherHostMaintenanceAuditStep"

        mock_result = Mock()
        mock_result.message = {"actions": []}
        mock_run_plan.return_value = {
            "CreateWatcherHostMaintenanceAuditStep": mock_result
        }

        yield {
            "mock_juju_helper": mock_juju_helper,
            "mock_get_cluster_status": mock_get_cluster_status,
            "mock_run_preflight_checks": mock_run_preflight_checks,
            "mock_operation_viewer": mock_operation_viewer,
            "mock_run_plan": mock_run_plan,
            "mock_create_watcher_step": mock_create_watcher_step,
        }


class TestDisableMigrationCommand:
    """Test cases for the `disable_migration` option in maintenance command CLI."""

    def _call_enable_function(self, disable_migration):
        """Helper method to call enable function with given disable_migration param."""
        original_func = enable.callback
        mock_self = Mock()
        mock_deployment = Mock()

        with patch("click.get_current_context"):
            original_func(
                mock_self,
                deployment=mock_deployment,
                node="test-node",
                force=True,
                dry_run=True,
                enable_ceph_crush_rebalancing=False,
                stop_osds=False,
                disable_migration=disable_migration,
                show_hints=False,
            )

    @pytest.mark.parametrize(
        "disable_migration,expected_live,expected_cold",
        [
            (None, False, False),  # None case
            ("both", True, True),  # "both" case
            ("live", True, False),  # "live" case
            ("cold", False, True),  # "cold" case
        ],
    )
    def test_disable_migration_parameters(
        self, base_mocks, disable_migration, expected_live, expected_cold
    ):
        """Test disable-migration parameter mapping for all cases."""
        mock_create_watcher_step = base_mocks["mock_create_watcher_step"]

        with patch("sunbeam.features.maintenance.commands.EnableMaintenance") as mock_enable_maintenance:
            self._call_enable_function(disable_migration)

            # Check EnableMaintenance was created with correct migration parameters
            mock_enable_maintenance.assert_called_once()
            _, kwargs = mock_enable_maintenance.call_args
            assert kwargs["disable_live_migration"] == expected_live
            assert kwargs["disable_cold_migration"] == expected_cold
