# Pinned by digest so every build starts from the same reviewed image. To take Debian and Python fixes, refresh
# the digest and requirements-lock.txt together (app/README.md, "Update the base image and packages").
FROM python:3.14-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b

# Only the Easy Auth sidecar in the same replica (loopback) may set the forwarded scheme and client address.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FORWARDED_ALLOW_IPS=127.0.0.1,::1

# mssql-python bundles its ODBC driver but needs these system libraries on Debian.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Installs exactly the locked versions, verified by hash and from wheels only, then removes pip: the running app
# never installs packages, and pip's own bundled libraries would otherwise stay in the image.
COPY requirements-lock.txt .
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary :all: \
        -r requirements-lock.txt \
    && python -m pip check \
    && python -m pip uninstall --yes --disable-pip-version-check pip \
    && rm requirements-lock.txt
COPY passport ./passport

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin passport
USER 10001
EXPOSE 8000

CMD ["uvicorn", "passport.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
