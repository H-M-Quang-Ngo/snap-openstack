# SPDX-FileCopyrightText: 2023 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

variable "k8s_channel" {
  description = "Operator channel for k8s deployment"
  type        = string
  default     = "latest/edge"
}

variable "k8s_revision" {
  description = "Operator channel revision for k8s deployment"
  type        = number
  default     = null
}

variable "k8s_config" {
  description = "Operator config for k8s deployment"
  type        = map(string)
  default     = {}
}

variable "machine_ids" {
  description = "List of machine ids to include"
  type        = list(string)
  default     = []
}

variable "machine_model" {
  description = "Model to deploy to"
  type        = string
}

variable "endpoint_bindings" {
  description = "Endpoint bindings for k8s"
  type        = set(map(string))
  default     = null
}
