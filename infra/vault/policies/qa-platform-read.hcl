# The web application may read exactly two KV-v2 documents. It cannot list
# metadata, discover other paths, create versions, delete values, or call sys/.
path "qa-platform/data/runtime" {
  capabilities = ["read"]
}

path "qa-platform/data/providers" {
  capabilities = ["read"]
}
