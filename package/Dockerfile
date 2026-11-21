FROM python:3.13-slim AS builder

WORKDIR /src
COPY . .

RUN python tools/validate.py \
    && python tools/package.py > /package-info.json


FROM python:3.13-slim

ARG VERSION=0.2.3
ARG VCS_REF=local
ARG SOURCE_URL=https://github.com/kkswl/ttdxiaohao

LABEL org.opencontainers.image.title="TgtoDrive 115 Account Manager Plugin" \
      org.opencontainers.image.description="Installable TgtoDrive plugin bundle for managing 115 sub-accounts and Emby assignments." \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      io.github.kkswl.tgto.plugin="tgto-115-accounts"

ENV TGTO_PLUGIN_VERSION="${VERSION}"

RUN mkdir -p /package
COPY --from=builder /dist/tgto-115-accounts-${VERSION}-preview.zip /package/
COPY --from=builder /dist/tgto-115-accounts-${VERSION}-source.zip /package/
COPY --from=builder /package-info.json /package/package-info.json
COPY --from=builder /src/README.md /package/README.md

WORKDIR /package
CMD ["/bin/sh"]
