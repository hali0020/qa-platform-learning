# Persistent, single-node teaching mode. This is deliberately not `-dev`:
# data is encrypted on disk and the server starts sealed after every restart.
ui                 = false
disable_mlock      = false
disable_clustering = true
api_addr           = "http://vault-core:8200"
log_level          = "info"
default_lease_ttl  = "1h"
max_lease_ttl      = "24h"

storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address                  = "0.0.0.0:8200"
  tls_disable              = true
  redact_addresses         = true
  redact_cluster_name      = true
  redact_version           = true

  telemetry {
    unauthenticated_metrics_access = false
  }
}

# Do not retain or expose Prometheus telemetry in this isolated lesson.
telemetry {
  prometheus_retention_time = "0s"
  disable_hostname          = true
}
