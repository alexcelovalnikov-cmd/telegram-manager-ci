# Capture the actual V22 image ID on the server; pass it as BASE_IMAGE.
# This reuses its working dependency environment without replacing old SDK versions.
ARG BASE_IMAGE=telegram-manager:V22
FROM ${BASE_IMAGE}
USER root
WORKDIR /app
COPY requirements.lock requirements.v24.txt ./
RUN python -m pip install --no-cache-dir --constraint requirements.lock -r requirements.v24.txt && python -m pip check
COPY tm_api ./tm_api
COPY tm_calendar ./tm_calendar
COPY tm_reminders ./tm_reminders
USER 10001
CMD ["uvicorn","tm_api.app:create_app","--factory","--host","0.0.0.0","--port","8000","--no-access-log","--no-proxy-headers","--log-level","critical"]
