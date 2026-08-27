# syntax=docker/dockerfile:1.7

# Official Keycloak 26.7.2 OCI index from the project's Quay registry. Keep the
# release tag for readability and the complete index digest for immutability.
FROM quay.io/keycloak/keycloak:26.7.2@sha256:831330513f55695572286e521f94fcd3c7e285250ed5b848090265a33192f669 AS builder

ENV KC_DB=dev-file
ENV KC_CACHE=local
ENV KC_HEALTH_ENABLED=true
ENV KC_METRICS_ENABLED=false

RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:26.7.2@sha256:831330513f55695572286e521f94fcd3c7e285250ed5b848090265a33192f669

COPY --from=builder /opt/keycloak/ /opt/keycloak/

ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
