FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Debian security updates, applied before anything else.
#
# The base image is rebuilt on its own schedule, so a CVE patched upstream can
# sit in it for days — CVE-2026-53615 (integer overflow in util-linux, nine
# packages) arrived exactly that way and failed this repository's own Trivy gate
# without a line of our code changing. Upgrading here fixes it in the image we
# actually ship rather than waiting for someone else's rebuild.
#
# `upgrade`, not `dist-upgrade`: it takes the security fixes without allowing a
# package to be removed to satisfy a dependency, which in a container is a
# silent way to lose something the app needs at runtime.
RUN apt-get update     && apt-get upgrade -y --no-install-recommends     && apt-get clean     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# pip is a build-time tool. Left in the runtime image it is both extra attack
# surface and a standing source of CVE noise: its bundled `_vendor/vendor.txt`
# declares msgpack and setuptools, so the image gets reported as vulnerable to
# flaws in code the service never imports and cannot reach. Removing it after
# the install is what actually resolves those, rather than suppressing them.
# The app runs uvicorn and the healthcheck uses urllib; neither needs pip.
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m pip uninstall -y pip

COPY app ./app

# Run as an unprivileged user: a container process that does not need root
# should not have it, so a compromise in the app is not a compromise of the
# container.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Reports unhealthy once /health returns 503, which it does when Mongo is
# unreachable — so an orchestrator can replace the instance instead of leaving
# it in rotation serving errors.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# --proxy-headers so the client address behind a load balancer is the real one
# (see TRUST_PROXY_HEADERS, which gates whether rate limiting believes it).
# --workers 2 as a floor: password hashing is offloaded to a thread pool, but a
# single process still serialises everything else behind one event loop.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
