# syntax=docker/dockerfile:1.7

# The application-facing Vault gateway uses the same pinned, unprivileged
# upstream NGINX image as the other protocol gateways in this repository.
FROM ghcr.io/nginx/nginx-unprivileged:1.30.4-alpine3.24@sha256:93722936b82ec8a1178d48448e619226680d2de3706a1640800e186cd5fa7fd3

COPY infra/docker/vault-gateway.conf /etc/nginx/conf.d/default.conf

EXPOSE 8200
