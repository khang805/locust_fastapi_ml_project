# Image used for Locust master/workers AND for the offline mock API (mock_server/fake_api.py)
FROM python:3.11-slim

WORKDIR /mnt/locust
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd -m locust && chown -R locust:locust /mnt/locust
USER locust

EXPOSE 8089 5557 8000
ENTRYPOINT ["locust"]
