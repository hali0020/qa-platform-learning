# syntax=docker/dockerfile:1.7

# Official unprivileged NGINX image, pinned to the immutable upstream index
# digest already verified for the phase-four S3-only gateway.
FROM ghcr.io/nginx/nginx-unprivileged:1.30.4-alpine3.24@sha256:93722936b82ec8a1178d48448e619226680d2de3706a1640800e186cd5fa7fd3

COPY infra/docker/keycloak-gateway.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
