# Front Door UI — stdlib-only, so there is NO dependency install step.
# Build with the front_door/ directory as the context:
#   docker build -t front-door front_door/
#   docker run -p 8000:8000 --env-file front_door/.env front-door
FROM python:3.11-slim

WORKDIR /app
COPY . /app/front_door

# Bind all interfaces in the container; honor the platform's $PORT (Cloud Run /
# Render / Fly / Heroku all inject it — config.py reads it).
ENV FRONT_DOOR_HOST=0.0.0.0 PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000')).read()" || exit 1

CMD ["python", "-m", "front_door.run"]
